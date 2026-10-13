#!/usr/bin/env python3
"""Autotest driver for verona-tool, run INSIDE the isolated container.
Not part of the verona-tool binary or repo -- a standalone test harness."""
import json
import os
import random
import shlex
import struct
import subprocess
import sys
import time
import hashlib
import traceback
import signal

QEMU = ["qemu-aarch64-static", "-L", "/aarch64-sysroot"]
VERONA = "/opt/tools/verona-tool"
FWTUTIL = "/opt/tools/fwtutil"
FWTABLECTL = "/opt/tools/fwtablectl"
GOLDEN_DUO = "/root/work/golden/duo-golden.img"
GOLDEN_MCH = "/root/work/golden/monarch-golden.img"
GOLDEN_MCH_128PARTS = "/root/work/golden/monarch-golden-128parts.img"
SCRATCH = "/root/work/scratch"
LOGFILE = "/root/work/autotest.log"
RESULTS = "/root/work/results.jsonl"
# Local-only transcript of every tool invocation run during the test, in
# order. Never committed to the repo -- this is purely a local debugging
# aid, not test output.
CMDLOG = "/root/work/commands.log"
DUO_TABLE_OFFSET = 6422528

# --- test-data diversity helpers -------------------------------------------

PLATFORM_ALIASES = {
    "mch": ["m", "monarch", "mch", "home"],
    "duo": ["p", "pelican", "mchd", "duo"],
}


def platform_flag(platform):
    """A randomly chosen valid -p/--platform alias for this platform --
    not always the canonical 'duo'/'mch' form."""
    return random.choice(PLATFORM_ALIASES[platform])


def sep():
    """A randomly chosen SLOT/FILE separator: ':' or '='. Both are
    equally valid per SPEC.md; using only one everywhere under-exercises
    the other."""
    return random.choice([":", "="])


_NAME_STYLES = ["ascii", "cyrillic", "cjk", "mixed"]


def test_filename(prefix, ext=".bin"):
    """A random-looking basename, varying script/charset: plain ASCII,
    Cyrillic, CJK, or a mix -- exercises non-Latin filenames end to end
    (open/read/write/log), not just ASCII ones."""
    n = random.randint(0, 1 << 30)
    style = random.choice(_NAME_STYLES)
    if style == "ascii":
        return f"{prefix}-{n}{ext}"
    if style == "cyrillic":
        return f"тест-{prefix}-{n}{ext}"
    if style == "cjk":
        return f"测试-{prefix}-{n}{ext}"
    return f"{prefix}-тест-测试-{n}{ext}"


def test_path(prefix, ext=".bin"):
    """A full path for a scratch file, varying style: absolute (most
    common), a relative path through a subdirectory, or a bare filename
    with no directory component at all (resolved against the current
    working directory, which main() sets to SCRATCH)."""
    name = test_filename(prefix, ext)
    style = random.choice(["absolute", "relative_subdir", "bare"])
    if style == "absolute":
        return os.path.join(SCRATCH, name)
    if style == "relative_subdir":
        subdir = f"sub-{random.randint(0, 999)}"
        os.makedirs(os.path.join(SCRATCH, subdir), exist_ok=True)
        return os.path.join(".", subdir, name)
    return name  # bare, relative to CWD (== SCRATCH)


_DEVICE_NAMES = ["/dev/sdb", "/dev/sdc", "/dev/sdx", "/dev/mmcblk0", "/dev/mmcblk1"]
_DEVICE_PATH_PROBABILITY = 0.10
# Device-path names currently in use by a not-yet-cleaned-up image. Without
# this, two independent fresh() calls in the same scenario (e.g.
# scenario_single_write's img_a/img_b) could each roll the same device name
# and silently collide -- the second symlink would repoint over the first,
# making img_a and img_b the same file despite the scenario's assumption
# that they're independent copies.
_ACTIVE_DEVICE_PATHS = set()


def maybe_as_device_path(real_path):
    """~10% of the time, create a symlink at a randomly chosen real-
    device-looking path (/dev/sdX, /dev/mmcblkN) pointing at real_path,
    and return that symlink path instead -- exercising -d against
    something that looks like an actual block device, not only plain
    scratch files. verona-tool just opens whatever path it's given, so
    this is transparent to it; Python's own file operations (os.path.*,
    open()) also transparently follow the symlink, so callers can use
    the returned path uniformly for both the -d argument and any direct
    byte-level verification."""
    if random.random() >= _DEVICE_PATH_PROBABILITY:
        return real_path
    available = [n for n in _DEVICE_NAMES if n not in _ACTIVE_DEVICE_PATHS]
    if not available:
        return real_path
    name = random.choice(available)
    try:
        os.remove(name)
    except OSError:
        pass
    os.symlink(real_path, name)
    _ACTIVE_DEVICE_PATHS.add(name)
    return name

FW_TYPES = ["Reserved","Bootcode","Kernel","RescueDeviceTree","KernelDeviceTree",
    "RescueRootFS","KernelRootFS","Audio","AudioFile","VideoFile","Ext4","Ubifs",
    "Squash","Ext3","Odd","YAFFS2","ISO","Swap","NTFS","JFFS2","ImageFile",
    "ImageFile1","ImageFile2","AudioFile1","AudioFile2","VideoFile1","VideoFile2",
    "Video","Video2","eCPU","Tee","GoldKernel","GoldRescueDeviceTree",
    "GoldRescueRootfs","GoldAudio","GoldTee","Config","uBoot","BL31","Hypervisor",
    "GoldBL31","RSAKeyFW","RSAKeyTee","RescueKernel","RescueAudio","RescueConfig",
    "Unknown"]

counters = {"pass": 0, "expected_divergence": {}, "genuine_failure": 0, "total": 0}
failures = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def record(kind, detail):
    with open(RESULTS, "a") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, "detail": detail}) + "\n")
    counters["total"] += 1
    if kind == "pass":
        counters["pass"] += 1
    elif kind == "genuine_failure":
        counters["genuine_failure"] += 1
        failures.append(detail)
        log(f"GENUINE FAILURE: {detail}")
    else:
        counters["expected_divergence"][kind] = counters["expected_divergence"].get(kind, 0) + 1


def log_cmd(binary_name, args):
    with open(CMDLOG, "a") as f:
        f.write(binary_name + " " + shlex.join(args) + "\n")


# A slot's real boundary can legitimately be gigabytes (Duo's "last by
# offset" fallback is the whole device size -- an accepted, documented
# limitation, not a bug). Generating/writing/verifying a multi-GB random
# file under qemu emulation to fuzz such a slot is impractically slow and
# not what this randomized loop is for (the boundary math itself is
# covered by targeted unit tests and the earlier truncated-image runs);
# scenarios needing a "boundary" skip themselves when it exceeds this cap.
MAX_PRACTICAL_BOUNDARY = 64 * 1024 * 1024


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=False, timeout=180)
    return p.returncode, p.stdout, p.stderr


def verona(args):
    log_cmd("verona-tool", args)
    return run(QEMU + [VERONA] + args)


def fwtutil(args):
    log_cmd("fwtutil", args)
    return run(QEMU + [FWTUTIL] + args)


def fwtablectl(args):
    log_cmd("fwtablectl", args)
    return run(QEMU + [FWTABLECTL] + args)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def gen_random_file(path, size):
    with open(path, "wb") as f:
        f.write(os.urandom(size))


def read_header(path, offset=0):
    with open(path, "rb") as f:
        f.seek(offset)
        hdr = f.read(32)
    sig, cks, ver, res, paddings, plen, flen = struct.unpack("<8sIB7sIII", hdr)
    return dict(sig=sig, cks=cks, ver=ver, paddings=paddings, plen=plen, flen=flen)


def read_fw_entries(path, offset, plen, flen):
    with open(path, "rb") as f:
        f.seek(offset + 32 + plen)
        raw = f.read(flen)
    out = []
    for i in range(flen // 32):
        e = raw[i * 32:(i + 1) * 32]
        typ, opt, ver, tgt, off, length, pad, cks = struct.unpack("<BBIIIIII", e[:26])
        out.append(dict(idx=i, type=typ, offset=off, length=length, paddings=pad, checksum=cks))
    return out


def read_part_entries(path, offset, plen):
    with open(path, "rb") as f:
        f.seek(offset + 32)
        raw = f.read(plen)
    out = []
    for i in range(plen // 48):
        out.append(raw[i * 48:(i + 1) * 48])
    return out


def read_gpt_partitions(path):
    with open(path, "rb") as f:
        f.seek(512)
        hdr = f.read(512)
        part_lba = struct.unpack_from("<Q", hdr, 72)[0]
        num = struct.unpack_from("<I", hdr, 80)[0]
        esize = struct.unpack_from("<I", hdr, 84)[0]
        alt_lba = struct.unpack_from("<Q", hdr, 32)[0]
        f.seek(part_lba * 512)
        arr = f.read(num * esize)
    parts = []
    for i in range(num):
        e = arr[i * esize:(i + 1) * esize]
        if e[0:16] == b"\x00" * 16:
            continue
        start = struct.unpack_from("<Q", e, 32)[0]
        parts.append(start)
    parts.sort()
    return parts, alt_lba + 1


def duo_boundary(img, slot):
    h = read_header(img, DUO_TABLE_OFFSET)
    fws = read_fw_entries(img, DUO_TABLE_OFFSET, h["plen"], h["flen"])
    offsets = sorted(e["offset"] for e in fws)
    off = fws[slot]["offset"]
    for i, o in enumerate(offsets):
        if o == off:
            if i + 1 < len(offsets):
                return offsets[i + 1] - off
            return os.path.getsize(img) - off
    raise RuntimeError("offset not found")


def monarch_table_offset(img):
    parts, total = read_gpt_partitions(img)
    # FW_TABLE is always the first partition by construction in our
    # synthetic image (and in the real sfdisk layout).
    return parts[0] * 512, parts, total


def monarch_boundary(img, slot):
    table_off, parts, total = monarch_table_offset(img)
    h = read_header(img, table_off)
    fws = read_fw_entries(img, table_off, h["plen"], h["flen"])
    off = fws[slot]["offset"]
    start_lba = off // 512
    idx = parts.index(start_lba)
    if idx + 1 < len(parts):
        return parts[idx + 1] * 512 - off
    return (total - 33) * 512 - off


def writable_slots(img, platform, n):
    """All slot indices whose write boundary falls in the practically
    testable range [16, MAX_PRACTICAL_BOUNDARY]. Scenarios that pick ONE
    random slot and then bail if its boundary disqualifies it waste that
    iteration and skew write coverage towards whichever slots happen to
    both get picked AND pass the filter -- picking from this list instead
    gives -w the same full-range coverage -r already gets (which has no
    such filter, since reading has no boundary to violate)."""
    result = []
    for slot in range(n):
        b = duo_boundary(img, slot) if platform == "duo" else monarch_boundary(img, slot)
        if 16 <= b <= MAX_PRACTICAL_BOUNDARY:
            result.append((slot, b))
    return result


def fresh(platform):
    """A fresh scratch copy of the platform's golden image. ~10% of the
    time, returns a /dev/sdX- or /dev/mmcblkN-style symlink pointing at
    the real copy instead of the plain scratch path itself, so the
    returned value can be used uniformly both as the -d argument and for
    direct Python-side byte verification (os.path.*/open() follow the
    symlink transparently either way)."""
    name = f"{platform}-{random.randint(0, 1 << 30)}.img"
    dst = os.path.join(SCRATCH, name)
    src = GOLDEN_DUO if platform == "duo" else GOLDEN_MCH
    if platform == "mch":
        subprocess.run(["cp", "--sparse=always", src, dst], check=True)
    else:
        subprocess.run(["cp", src, dst], check=True)
    return maybe_as_device_path(dst)


def cleanup(*paths):
    for p in paths:
        if p is None:
            continue
        try:
            if os.path.islink(p):
                target = os.path.realpath(p)
                os.remove(p)
                os.remove(target)
            else:
                os.remove(p)
        except OSError:
            pass
        _ACTIVE_DEVICE_PATHS.discard(p)


def fwcount(img, platform):
    if platform == "duo":
        h = read_header(img, DUO_TABLE_OFFSET)
    else:
        off, _, _ = monarch_table_offset(img)
        h = read_header(img, off)
    return h["flen"] // 32


def read_slot_payload(img, platform, slot):
    """Independently read a slot's raw payload bytes directly off the image
    at its real byte offset, entirely bypassing verona-tool (equivalent to
    dd if=img bs=1 skip=OFFSET count=LENGTH) -- confirms a write actually
    landed at the correct physical location, not just that the table's
    bookkeeping fields say so."""
    off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
    h = read_header(img, off)
    fw = read_fw_entries(img, off, h["plen"], h["flen"])[slot]
    with open(img, "rb") as f:
        f.seek(fw["offset"])
        return f.read(fw["length"]), fw["offset"], fw["length"]


def verify_payload(img, platform, slot, src_path, context):
    """Independent-read check: compare the slot's on-disk payload (read
    directly, bypassing verona-tool) against the source file that was
    supposedly written there. Returns True on match; records a
    genuine_failure and returns False on mismatch."""
    got, off, length = read_slot_payload(img, platform, slot)
    with open(src_path, "rb") as f:
        want = f.read()
    if got != want:
        record("genuine_failure", f"{context}: independent read-back at offset={off} length={length} does not match source file {src_path} (bypassing verona-tool)")
        return False
    return True


# ---------------------------------------------------------------- scenarios

def scenario_single_write(platform):
    img_a = fresh(platform)
    img_b = fresh(platform)
    n = fwcount(img_a, platform)
    candidates = writable_slots(img_a, platform, n)
    if not candidates:
        cleanup(img_a, img_b)
        return
    slot, boundary = random.choice(candidates)
    size_choice = random.choice(["small", "near", "aligned512"])
    if size_choice == "small":
        size = random.randint(1, min(4096, boundary))
    elif size_choice == "near":
        size = max(1, boundary - random.randint(1, 16))
    else:
        size = (boundary // 512) * 512
        if size == 0 or size > boundary:
            size = 512
    src = test_path("src", ".bin")
    gen_random_file(src, size)

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-w", f"{slot}{sep()}{src}"])
    if rc != 0:
        record("genuine_failure", f"single_write platform={platform} slot={slot} size={size} verona rc={rc} err={err!r}")
        cleanup(img_a, img_b, src)
        return

    # Independent read-back, bypassing verona-tool entirely: confirm the
    # payload actually landed at the slot's real byte offset on the image,
    # not just that the table's Length/Checksum bookkeeping say so.
    if not verify_payload(img_a, platform, slot, src, f"single_write platform={platform} slot={slot} size={size}"):
        cleanup(img_a, img_b, src)
        return

    if platform == "duo":
        # Symlink /dev/mmcblk0 -> img_b for the real fwtutil. img_b may
        # already BE "/dev/mmcblk0" (fresh()'s own ~10% device-path
        # coverage, item 7) -- in that case it already points at the
        # right file, and removing+re-symlinking it onto itself would
        # create a self-referential loop.
        if img_b != "/dev/mmcblk0":
            try:
                os.remove("/dev/mmcblk0")
            except OSError:
                pass
            os.symlink(img_b, "/dev/mmcblk0")
        # The real fwtutil's CLI is the opposite of what the flag letters
        # suggest: "-r"/"-R" replaces (writes) a slot FROM FILE, "-x"
        # extracts (reads) a slot TO FILE. Confirmed directly against the
        # real binary's own output ("Замена раздела..." for -r vs.
        # "Извлечение раздела..." for -x). We want a WRITE here: apply the
        # same src content to img_b via the real tool, so img_a (written
        # by verona-tool) and img_b (written by fwtutil) can be compared
        # whole-image and expected byte-identical below.
        rc2, out2, err2 = fwtutil(["-r", f"{slot}:{src}"])
        if rc2 != 0:
            record("genuine_failure", f"single_write duo slot={slot} size={size} fwtutil rc={rc2} err={err2!r}")
            cleanup(img_a, img_b, src)
            return
        h1, h2 = sha256(img_a), sha256(img_b)
        if h1 == h2:
            record("pass", f"single_write duo slot={slot} size={size}")
        else:
            record("genuine_failure", f"single_write duo slot={slot} size={size} whole-image hash mismatch (expected identical)")
    else:
        off_a, _, _ = monarch_table_offset(img_a)
        h_a = read_header(img_a, off_a)
        table_a = open(img_a, "rb").read()[off_a:off_a + 32 + h_a["plen"] + h_a["flen"]]

        off_b, _, _ = monarch_table_offset(img_b)
        h_b = read_header(img_b, off_b)
        table_file = test_path("tbl", ".img")
        with open(table_file, "wb") as f:
            f.write(open(img_b, "rb").read()[off_b:off_b + 32 + h_b["plen"] + h_b["flen"]])
        type_name = FW_TYPES[read_fw_entries(img_b, off_b, h_b["plen"], h_b["flen"])[slot]["type"]]
        rc2, out2, err2 = fwtablectl(["firmware", "update", table_file, type_name, src])
        if rc2 != 0:
            record("genuine_failure", f"single_write mch slot={slot} size={size} fwtablectl rc={rc2} err={err2!r}")
            cleanup(img_a, img_b, src, table_file)
            return
        table_b = open(table_file, "rb").read()

        diffs = diff_monarch_tables(table_a, table_b, h_a)
        classify_monarch_diffs(diffs, f"single_write slot={slot} size={size}", h_a["plen"], h_a["flen"])
        cleanup(table_file)
    cleanup(img_a, img_b, src)


def diff_monarch_tables(a, b, hdr):
    """Return list of (byte_offset, a_byte, b_byte) for every differing byte."""
    n = min(len(a), len(b))
    return [(i, a[i], b[i]) for i in range(n) if a[i] != b[i]]


def classify_monarch_diffs(diffs, context, plen, flen):
    if not diffs:
        record("pass", context)
        return
    part_list_start = 32
    fw_list_start = part_list_start + plen
    fw_list_end = fw_list_start + flen
    categories = set()
    unexplained = []
    for off, av, bv in diffs:
        if off < 12:
            # signature/checksum field: the table-level checksum differs
            # whenever anything else does -- expected, not interesting on
            # its own.
            continue
        if part_list_start <= off < fw_list_start:
            rel = off - part_list_start
            _, part_byte = divmod(rel, 48)
            if part_byte == 12:
                # EmmcPartID -- known wdmch-tools bug: always reads back
                # as 0 on the write side, so it gets zeroed for every
                # part entry after any fwtablectl write.
                categories.add("divergence_emmcpartid_zeroed")
                continue
            unexplained.append((off, av, bv))
        elif fw_list_start <= off < fw_list_end:
            rel = off - fw_list_start
            _, fw_byte = divmod(rel, 32)
            if 18 <= fw_byte <= 21:
                # Paddings field -- known wdmch-tools bug: always adds a
                # spare 512-byte sector even when the length is already
                # an exact multiple of 512.
                categories.add("divergence_padding_always_plus_512")
                continue
            unexplained.append((off, av, bv))
        else:
            unexplained.append((off, av, bv))
    if unexplained:
        record("genuine_failure", f"{context}: unexplained table diffs at {unexplained[:10]}")
    else:
        record("+".join(sorted(categories)) or "pass", context)


def scenario_sequential_overwrites(platform):
    img_a = fresh(platform)
    n = fwcount(img_a, platform)
    candidates = writable_slots(img_a, platform, n)
    if not candidates:
        cleanup(img_a)
        return
    slot, boundary = random.choice(candidates)
    rounds = random.choice([2, 3])
    srcs = []
    ok = True
    for r in range(rounds):
        size = random.randint(1, min(boundary, 1 << 20))
        src = test_path("seq", ".bin")
        gen_random_file(src, size)
        srcs.append((src, size))
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-w", f"{slot}{sep()}{src}"])
        if rc != 0:
            record("genuine_failure", f"sequential_overwrite platform={platform} slot={slot} round={r} rc={rc} err={err!r}")
            ok = False
            break
        # Independent read-back after EACH round, bypassing verona-tool,
        # confirming the payload is physically at the slot's real offset.
        if not verify_payload(img_a, platform, slot, src, f"sequential_overwrite platform={platform} slot={slot} round={r}"):
            ok = False
            break
        # Also verify via verona-tool's own -r, after EACH round.
        rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img_a, "-r", f"{slot}{sep()}{src}.readback"])
        if rc2 != 0:
            record("genuine_failure", f"sequential_overwrite readback platform={platform} slot={slot} round={r} rc={rc2}")
            ok = False
            break
        with open(src, "rb") as f1, open(src + ".readback", "rb") as f2:
            same = f1.read() == f2.read()
        cleanup(src + ".readback")
        if not same:
            record("genuine_failure", f"sequential_overwrite readback mismatch platform={platform} slot={slot} round={r}")
            ok = False
            break
    if ok:
        record("pass", f"sequential_overwrite platform={platform} slot={slot} rounds={rounds}")
    cleanup(img_a, *[s for s, _ in srcs])


def scenario_refusal(platform):
    img_a = fresh(platform)
    n = fwcount(img_a, platform)
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img_a, slot) if platform == "duo" else monarch_boundary(img_a, slot)
    if boundary > MAX_PRACTICAL_BOUNDARY:
        cleanup(img_a)
        return
    size = boundary + random.randint(1, 1 << 16)
    src = test_path("over", ".bin")
    gen_random_file(src, size)
    before = sha256(img_a)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-w", f"{slot}{sep()}{src}"])
    after = sha256(img_a)
    if rc == 3 and before == after:
        record("pass", f"refusal platform={platform} slot={slot} boundary={boundary} size={size}")
    else:
        record("genuine_failure", f"refusal platform={platform} slot={slot} boundary={boundary} size={size} rc={rc} hash_unchanged={before==after} err={err!r}")
    cleanup(img_a, src)


def scenario_backup_restore_list(platform):
    img_a = fresh(platform)
    n = fwcount(img_a, platform)
    backup_file = test_path("bak", ".img")

    off, _, _ = (monarch_table_offset(img_a) if platform == "mch" else (DUO_TABLE_OFFSET, None, None))
    h = read_header(img_a, off)
    table_before = open(img_a, "rb").read()[off:off + h["paddings"]]

    # -B (explicit filename) combined with a harmless -r SLOT (list/backup
    # alone with no -r/-w would still be valid per spec, but exercising
    # -B alongside an ordinary -r is also part of what we want covered).
    read_slot = random.randint(0, n - 1)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-B", backup_file, "-r", str(read_slot)])
    if rc != 0:
        record("genuine_failure", f"backup platform={platform} rc={rc} err={err!r}")
        cleanup(img_a, backup_file)
        return
    with open(backup_file, "rb") as f:
        backup_bytes = f.read()
    if backup_bytes != table_before:
        record("genuine_failure", f"backup platform={platform}: backup file does not match pre-backup table bytes")
        cleanup(img_a, backup_file, os.path.join(os.getcwd(), f"{read_slot}.img"))
        return

    mutate_candidates = writable_slots(img_a, platform, n)
    if mutate_candidates:
        slot, boundary = random.choice(mutate_candidates)
        src = test_path("mut", ".bin")
        gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-w", f"{slot}{sep()}{src}"])
        cleanup(src)
        if rc != 0:
            record("genuine_failure", f"backup_restore mutate platform={platform} rc={rc} err={err!r}")
            cleanup(img_a, backup_file)
            return

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-R", backup_file])
    if rc != 0:
        record("genuine_failure", f"restore platform={platform} rc={rc} err={err!r}")
        cleanup(img_a, backup_file)
        return

    table_after = open(img_a, "rb").read()[off:off + h["paddings"]]
    if table_after == table_before:
        record("pass", f"backup_restore_list platform={platform}")
    else:
        record("genuine_failure", f"backup_restore platform={platform}: table not byte-identical after restore")
    cleanup(img_a, backup_file)
    try:
        os.remove(os.path.join(os.getcwd(), f"{read_slot}.img"))
    except OSError:
        pass


def scenario_multi_read(platform):
    """Several -r SLOT:FILE in a single invocation -- exercises multi-flag
    parsing (not just one -r per call, which is all every other scenario
    does) and the 'all -r run before any -w' ordering guarantee, by also
    throwing in a -w for a slot NOT among the ones being read and
    confirming every -r output still matches its slot's real payload
    regardless of where the -w fell in the argument list."""
    img = fresh(platform)
    n = fwcount(img, platform)
    if n < 3:
        cleanup(img)
        return
    read_slots = random.sample(range(n), k=min(4, n))
    args = ["-p", platform_flag(platform), "-d", img]
    outs = {}
    for s in read_slots:
        outs[s] = test_path(f"mr{s}", ".img")
        args += ["-r", f"{s}{sep()}{outs[s]}"]

    write_slot = None
    write_src = None
    candidates = writable_slots(img, platform, n)
    candidates = [(s, b) for s, b in candidates if s not in read_slots]
    if candidates and random.random() < 0.5:
        write_slot, boundary = random.choice(candidates)
        write_src = test_path("mrw", ".bin")
        gen_random_file(write_src, random.randint(1, min(boundary, 1 << 16)))
        args += ["-w", f"{write_slot}{sep()}{write_src}"]

    rc, out, err = verona(args)
    if rc != 0:
        record("genuine_failure", f"multi_read platform={platform} slots={read_slots} write_slot={write_slot} rc={rc} err={err!r}")
        cleanup(img, write_src, *outs.values())
        return

    ok = True
    for s in read_slots:
        if not verify_payload(img, platform, s, outs[s], f"multi_read platform={platform} slots={read_slots} slot={s}"):
            ok = False
    if write_slot is not None and not verify_payload(img, platform, write_slot, write_src, f"multi_read platform={platform} write_slot={write_slot}"):
        ok = False
    if ok:
        record("pass", f"multi_read platform={platform} slots={read_slots} write_slot={write_slot}")
    cleanup(img, write_src, *outs.values())


def scenario_duo_fwtutil_crosscheck(platform):
    """Dedicated, frequent cross-validation against the real fwtutil for
    Duo, covering BOTH directions: write via verona-tool then
    independently read it back via fwtutil's own -x (extract) and compare
    bytes; then separately write via fwtutil's own -r (replace) and
    independently read it back via verona-tool's -r, also compared.
    scenario_single_write already does one fwtutil write-comparison per
    run at most; this scenario exists purely to make fwtutil exercise a
    meaningfully larger fraction of Duo iterations, across both of its
    directions, not just a single incidental use."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    size = random.randint(1, min(boundary, 1 << 16))

    if img != "/dev/mmcblk0":
        try:
            os.remove("/dev/mmcblk0")
        except OSError:
            pass
        os.symlink(img, "/dev/mmcblk0")

    direction = random.choice(["verona_write_fwtutil_read", "fwtutil_write_verona_read"])
    src = test_path("fwx", ".bin")
    gen_random_file(src, size)
    if direction == "verona_write_fwtutil_read":
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
        if rc != 0:
            record("genuine_failure", f"duo_fwtutil_crosscheck write(verona) platform={platform} slot={slot} rc={rc} err={err!r}")
            cleanup(img, src)
            return
        readback = test_path("fwxr", ".bin")
        rc2, out2, err2 = fwtutil(["-x", f"{slot}:{readback}"])
        if rc2 != 0:
            record("genuine_failure", f"duo_fwtutil_crosscheck read(fwtutil) platform={platform} slot={slot} rc={rc2} err={err2!r}")
            cleanup(img, src, readback)
            return
        with open(src, "rb") as f1, open(readback, "rb") as f2:
            same = f1.read() == f2.read()
        cleanup(readback)
    else:
        rc, out, err = fwtutil(["-r", f"{slot}:{src}"])
        if rc != 0:
            record("genuine_failure", f"duo_fwtutil_crosscheck write(fwtutil) platform={platform} slot={slot} rc={rc} err={err!r}")
            cleanup(img, src)
            return
        readback = test_path("fwxr", ".bin")
        rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img, "-r", f"{slot}{sep()}{readback}"])
        if rc2 != 0:
            record("genuine_failure", f"duo_fwtutil_crosscheck read(verona) platform={platform} slot={slot} rc={rc2} err={err2!r}")
            cleanup(img, src, readback)
            return
        with open(src, "rb") as f1, open(readback, "rb") as f2:
            same = f1.read() == f2.read()
        cleanup(readback)

    if same:
        record("pass", f"duo_fwtutil_crosscheck platform={platform} slot={slot} direction={direction}")
    else:
        record("genuine_failure", f"duo_fwtutil_crosscheck mismatch platform={platform} slot={slot} direction={direction}")
    cleanup(img, src)


def scenario_duo_fwtutil_multiflag(platform):
    """Multiple -r/-x flags in a SINGLE real fwtutil invocation, including
    a mix of both in one call (e.g. "-x A -r B -x C") -- mirrors the
    multi-flag coverage scenario_multi_read already gives verona-tool's
    own -r/-w. The real fwtutil processes flags in literal command-line
    order (confirmed directly against its own output: an -x between two
    other flags prints its "Извлечение" line between their output lines,
    not batched before/after like verona-tool's "all -r before any -w"),
    but since the read slots and write slots chosen here are always
    disjoint, that ordering difference can't affect the final byte
    state -- only the two tools' internal processing order, which this
    scenario doesn't care about. Each read is cross-checked against the
    equivalent verona-tool multi-flag -r result for the same slots (a
    single verona-tool invocation with multiple -r, the same structure
    scenario_multi_read already exercises for verona-tool alone), and
    each write is cross-checked via an independent byte-level read."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if len(candidates) < 3:
        cleanup(img)
        return

    pool = candidates[:]
    random.shuffle(pool)
    read_slots = [s for s, _ in pool[:2]]
    write_pool = pool[2:4] if len(pool) >= 4 else pool[2:3]
    write_slots = [(s, b) for s, b in write_pool]

    if img != "/dev/mmcblk0":
        try:
            os.remove("/dev/mmcblk0")
        except OSError:
            pass
        os.symlink(img, "/dev/mmcblk0")

    fwtutil_args = []
    read_outs = {}
    write_srcs = {}
    # Interleave -x/-r in a fixed, readable order: alternate starting
    # with whichever kind has more entries, so a 2-read/2-write call
    # comes out "-x -r -x -r" (and a 2-read/1-write call "-x -r -x").
    reads_left = list(read_slots)
    writes_left = list(write_slots)
    while reads_left or writes_left:
        if reads_left:
            s = reads_left.pop(0)
            out = test_path(f"fwm{s}", ".img")
            read_outs[s] = out
            fwtutil_args += ["-x", f"{s}:{out}"]
        if writes_left:
            s, b = writes_left.pop(0)
            src = test_path(f"fwmw{s}", ".bin")
            gen_random_file(src, random.randint(1, min(b, 1 << 16)))
            write_srcs[s] = src
            fwtutil_args += ["-r", f"{s}:{src}"]

    rc, out, err = fwtutil(fwtutil_args)
    if rc != 0:
        record("genuine_failure", f"duo_fwtutil_multiflag platform={platform} reads={read_slots} writes={[s for s,_ in write_slots]} rc={rc} err={err!r}")
        cleanup(img, *read_outs.values(), *write_srcs.values())
        return

    # Cross-check the reads against a single verona-tool multi-flag -r
    # invocation for the exact same slots.
    vargs = ["-p", platform_flag(platform), "-d", img]
    verona_outs = {}
    for s in read_slots:
        verona_outs[s] = test_path(f"fwmv{s}", ".img")
        vargs += ["-r", f"{s}{sep()}{verona_outs[s]}"]
    rc2, out2, err2 = verona(vargs)
    ok = True
    if rc2 != 0:
        record("genuine_failure", f"duo_fwtutil_multiflag verona-crosscheck platform={platform} reads={read_slots} rc={rc2} err={err2!r}")
        ok = False
    else:
        for s in read_slots:
            with open(read_outs[s], "rb") as f1, open(verona_outs[s], "rb") as f2:
                if f1.read() != f2.read():
                    record("genuine_failure", f"duo_fwtutil_multiflag platform={platform} slot={s}: fwtutil -x result differs from verona-tool -r result")
                    ok = False

    # Cross-check the writes via an independent byte-level read.
    for s, src in write_srcs.items():
        if not verify_payload(img, platform, s, src, f"duo_fwtutil_multiflag platform={platform} slot={s}"):
            ok = False

    if ok:
        record("pass", f"duo_fwtutil_multiflag platform={platform} reads={read_slots} writes={list(write_srcs)}")
    cleanup(img, *read_outs.values(), *write_srcs.values(), *verona_outs.values())


def scenario_mch_fwtablectl_crosscheck(platform):
    """Dedicated, frequent cross-validation against the real fwtablectl
    for Monarch: write via verona-tool on img_a, apply the identical
    update via fwtablectl's own 'firmware update' on an independently
    extracted copy of img_b's table region, and compare the two
    resulting table regions byte-for-byte. fwtablectl has no read/extract
    command of its own (checked directly against the real binary's
    usage text), so unlike the Duo/fwtutil check this is write-only --
    scenario_single_write already does this once per run at most; this
    scenario exists purely to make fwtablectl exercise a meaningfully
    larger fraction of Monarch iterations instead of being nearly absent
    from any given run."""
    img_a = fresh(platform)
    img_b = fresh(platform)
    n = fwcount(img_a, platform)
    candidates = writable_slots(img_a, platform, n)
    if not candidates:
        cleanup(img_a, img_b)
        return
    slot, boundary = random.choice(candidates)
    size = random.randint(1, min(boundary, 1 << 16))
    src = test_path("ftc", ".bin")
    gen_random_file(src, size)

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img_a, "-w", f"{slot}{sep()}{src}"])
    if rc != 0:
        record("genuine_failure", f"mch_fwtablectl_crosscheck write(verona) platform={platform} slot={slot} rc={rc} err={err!r}")
        cleanup(img_a, img_b, src)
        return

    off_a, _, _ = monarch_table_offset(img_a)
    h_a = read_header(img_a, off_a)
    table_a = open(img_a, "rb").read()[off_a:off_a + 32 + h_a["plen"] + h_a["flen"]]

    off_b, _, _ = monarch_table_offset(img_b)
    h_b = read_header(img_b, off_b)
    table_file = test_path("ftctbl", ".img")
    with open(table_file, "wb") as f:
        f.write(open(img_b, "rb").read()[off_b:off_b + 32 + h_b["plen"] + h_b["flen"]])
    type_name = FW_TYPES[read_fw_entries(img_b, off_b, h_b["plen"], h_b["flen"])[slot]["type"]]
    rc2, out2, err2 = fwtablectl(["firmware", "update", table_file, type_name, src])
    if rc2 != 0:
        record("genuine_failure", f"mch_fwtablectl_crosscheck write(fwtablectl) platform={platform} slot={slot} rc={rc2} err={err2!r}")
        cleanup(img_a, img_b, src, table_file)
        return
    table_b = open(table_file, "rb").read()

    diffs = diff_monarch_tables(table_a, table_b, h_a)
    classify_monarch_diffs(diffs, f"mch_fwtablectl_crosscheck slot={slot} size={size}", h_a["plen"], h_a["flen"])
    cleanup(img_a, img_b, src, table_file)


import re

_HDR_RE = re.compile(r"Signature:\s*(\S+)\s+Checksum:\s*0x([0-9a-fA-F]+)\s+Version:\s*(\d+)\s+Size:\s*(\d+)")
_FW_RE = re.compile(
    r"^\s*(\d+):\s*(\S+)\s+ro=(\w+)\s+lzma=(\w+)\s+ver=(\d+)\s+target=0x([0-9a-fA-F]+)\s+"
    r"offset=0x([0-9a-fA-F]+)\s+length=(\d+)\s+paddings=(\d+)\s+checksum=0x([0-9a-fA-F]+)",
    re.MULTILINE,
)


def scenario_list_check(platform):
    """Dedicated, standalone -l check: run verona-tool -l and validate the
    printed header + every firmware entry against the ground truth read
    directly from the image bytes, not just "did it exit 0"."""
    img = fresh(platform)
    if platform == "duo":
        off = DUO_TABLE_OFFSET
    else:
        off, _, _ = monarch_table_offset(img)
    h = read_header(img, off)
    fws = read_fw_entries(img, off, h["plen"], h["flen"])

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-l"])
    if rc != 0:
        record("genuine_failure", f"list_check platform={platform} rc={rc} err={err!r}")
        cleanup(img)
        return
    text = out.decode("utf-8", "replace")

    hm = _HDR_RE.search(text)
    if not hm:
        record("genuine_failure", f"list_check platform={platform}: could not find header line in -l output")
        cleanup(img)
        return
    if hm.group(1) != "VERONA__" or int(hm.group(2), 16) != h["cks"] or int(hm.group(3)) != h["ver"] or int(hm.group(4)) != h["paddings"]:
        record("genuine_failure", f"list_check platform={platform}: header mismatch, -l printed {hm.groups()}, expected sig/cks/ver/paddings={h['cks']:#x}/{h['ver']}/{h['paddings']}")
        cleanup(img)
        return

    seen = {}
    for m in _FW_RE.finditer(text):
        idx = int(m.group(1))
        seen[idx] = dict(
            type_name=m.group(2),
            ro=m.group(3),
            lzma=m.group(4),
            ver=int(m.group(5)),
            target=int(m.group(6), 16),
            offset=int(m.group(7), 16),
            length=int(m.group(8)),
            paddings=int(m.group(9)),
            checksum=int(m.group(10), 16),
        )

    mismatches = []
    for e in fws:
        s = seen.get(e["idx"])
        if s is None:
            mismatches.append(f"entry {e['idx']} missing from -l output")
            continue
        want_type = FW_TYPES[e["type"]] if e["type"] < len(FW_TYPES) else f"Invalid({e['type']})"
        if (s["type_name"] != want_type or s["offset"] != e["offset"] or
                s["length"] != e["length"] or s["paddings"] != e["paddings"] or
                s["checksum"] != e["checksum"]):
            mismatches.append(f"entry {e['idx']}: -l printed {s}, expected type={want_type} offset={e['offset']} length={e['length']} paddings={e['paddings']} checksum={e['checksum']}")
    if len(seen) != len(fws):
        mismatches.append(f"entry count mismatch: -l printed {len(seen)}, table has {len(fws)}")

    if mismatches:
        record("genuine_failure", f"list_check platform={platform}: {mismatches[:5]}")
    else:
        record("pass", f"list_check platform={platform} entries_verified={len(fws)}")
    cleanup(img)


SHORTCUT_SLOTS = {
    "kernel": (5, 12),
    "dtb": (7, 6),
    "rootfs": (9, 8),
}


def scenario_shortcut_read(platform):
    """Exercise -r --kernel / --dtb / --rootfs (and their -k/-t/-o short
    forms): verify both files in the pair are produced and match a direct
    read of each slot."""
    img = fresh(platform)
    name = random.choice(list(SHORTCUT_SLOTS))
    flag = random.choice([f"--{name}", {"kernel": "-k", "dtb": "-t", "rootfs": "-f"}[name]])
    slots = SHORTCUT_SLOTS[name]

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", flag])
    if rc != 0:
        record("genuine_failure", f"shortcut_read platform={platform} name={name} rc={rc} err={err!r}")
        cleanup(img)
        return

    ok = True
    for slot in slots:
        want_file = os.path.join(SCRATCH, f"{slot}.img")
        if not os.path.exists(want_file):
            record("genuine_failure", f"shortcut_read platform={platform} name={name}: {want_file} not produced")
            ok = False
            continue
        off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
        h = read_header(img, off)
        fw = read_fw_entries(img, off, h["plen"], h["flen"])[slot]
        with open(img, "rb") as f:
            f.seek(fw["offset"])
            want = f.read(fw["length"])
        with open(want_file, "rb") as f:
            got = f.read()
        if got != want:
            record("genuine_failure", f"shortcut_read platform={platform} name={name} slot={slot}: content mismatch")
            ok = False
        cleanup(want_file)
    if ok:
        record("pass", f"shortcut_read platform={platform} name={name} flag={flag}")
    cleanup(img)


def scenario_shortcut_write(platform):
    """Exercise -w --kernel=file / --dtb=file / --rootfs=file (and -k/-t/-o):
    normal case (both slots written identically), the atomic-pair boundary
    check (deliberately oversize one slot's own boundary, confirm NEITHER
    slot gets written), and the duplicate-shortcut conflict check."""
    img = fresh(platform)
    name = random.choice(list(SHORTCUT_SLOTS))
    flag_long = f"--{name}"
    flag_short = {"kernel": "-k", "dtb": "-t", "rootfs": "-f"}[name]
    slots = SHORTCUT_SLOTS[name]

    mode = random.choice(["normal", "oversized_half", "duplicate_conflict"])

    if mode == "duplicate_conflict":
        src = test_path("dupe", ".bin")
        gen_random_file(src, 64)
        before = sha256(img)
        # Write-mode shortcuts only accept the '=FILE' attachment on the
        # long option form (per SPEC.md); the short forms (-k/-t/-o) carry
        # no value in write mode. Using the long form twice is still a
        # valid way to exercise the duplicate-shortcut conflict check.
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w",
                                f"{flag_long}={src}", "-w", f"{flag_long}={src}"])
        after = sha256(img)
        if rc == 5 and before == after:
            record("pass", f"shortcut_write duplicate_conflict platform={platform} name={name}")
        else:
            record("genuine_failure", f"shortcut_write duplicate_conflict platform={platform} name={name} rc={rc} hash_unchanged={before==after}")
        cleanup(img, src)
        return

    b0 = duo_boundary(img, slots[0]) if platform == "duo" else monarch_boundary(img, slots[0])
    b1 = duo_boundary(img, slots[1]) if platform == "duo" else monarch_boundary(img, slots[1])
    min_b = min(b0, b1)
    if min_b < 16 or min_b > MAX_PRACTICAL_BOUNDARY:
        cleanup(img)
        return

    if mode == "oversized_half":
        size = min_b + random.randint(1, 1 << 16)
    else:
        size = random.randint(1, min(min_b, 1 << 16))
    src = test_path("sw", ".bin")
    gen_random_file(src, size)
    before = sha256(img)

    # Write-mode shortcuts only accept '=FILE' on the long option form.
    flag = flag_long
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{flag}={src}"])
    after = sha256(img)

    if mode == "oversized_half":
        if rc == 3 and before == after:
            record("pass", f"shortcut_write oversized_half platform={platform} name={name} size={size} min_boundary={min_b}")
        else:
            record("genuine_failure", f"shortcut_write oversized_half platform={platform} name={name} size={size} min_boundary={min_b} rc={rc} hash_unchanged={before==after}")
        cleanup(img, src)
        return

    # normal case
    if rc != 0:
        record("genuine_failure", f"shortcut_write normal platform={platform} name={name} size={size} rc={rc} err={err!r}")
        cleanup(img, src)
        return
    off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
    h = read_header(img, off)
    fws = read_fw_entries(img, off, h["plen"], h["flen"])
    ok = True
    with open(src, "rb") as f:
        want = f.read()
    for slot in slots:
        fw = fws[slot]
        with open(img, "rb") as f:
            f.seek(fw["offset"])
            got = f.read(fw["length"])
        if got != want or fw["length"] != size:
            record("genuine_failure", f"shortcut_write normal platform={platform} name={name} slot={slot}: written content/length mismatch")
            ok = False
    if ok:
        record("pass", f"shortcut_write normal platform={platform} name={name} flag={flag} size={size}")
    cleanup(img, src)


# Device-letter independence (Monarch only -- Duo has no device-letter
# concept, it's always a flat eMMC path): confirm the GPT/FW_TABLE lookup
# works through several different symlinked device-letter paths pointing at
# the SAME synthetic disk content, catching any accidental "sda"-style
# hardcoding regression.
_DEVICE_LETTERS = ["/dev/sdb", "/dev/sdc", "/dev/sdx"]


def scenario_device_letter_independence():
    img = fresh("mch")
    letter = random.choice(_DEVICE_LETTERS)
    # img may already BE this exact letter (fresh()'s own ~10%
    # device-path coverage, item 7) -- in that case it already points at
    # the right file, and removing+re-symlinking it onto itself would
    # create a self-referential loop.
    already_there = img == letter
    if not already_there:
        try:
            os.remove(letter)
        except OSError:
            pass
        os.symlink(img, letter)
    try:
        rc, out, err = verona(["-p", "mch", "-d", letter, "-l"])
        text = out.decode("utf-8", "replace")
        if rc == 0 and "Signature: VERONA__" in text and "Firmware entries (13):" in text:
            record("pass", f"device_letter_independence letter={letter}")
        else:
            record("genuine_failure", f"device_letter_independence letter={letter} rc={rc} out={text[:200]!r} err={err!r}")
    finally:
        if not already_there:
            try:
                os.remove(letter)
            except OSError:
                pass
    cleanup(img)


# =============================================================== edge cases
# Deterministic, mostly-fast edge/conflict/refusal checks, distinct from the
# randomized fuzzing scenarios above. See the coordinator's 16-point list.

import fcntl


def write_table_header(img, off, **fields):
    """Patch specific header fields in place (by name, matching read_header's
    dict keys) and recompute+write a correct checksum for the result, so the
    signature/checksum validation itself passes and whatever OTHER
    corruption was introduced is what actually gets exercised."""
    h = read_header(img, off)
    h.update(fields)
    with open(img, "r+b") as f:
        f.seek(off)
        body = f.read(h["paddings"])
    # body[0:32] is the old header; rebuild header bytes with new fields.
    hdr = struct.pack("<8sIB7sIII", b"VERONA__", 0, h["ver"], b"\0" * 7, h["paddings"], h["plen"], h["flen"])
    new_body = hdr + body[32:32 + h["plen"] + h["flen"]]
    cks = sum(new_body[12:]) & 0xffffffff
    hdr = struct.pack("<8sIB7sIII", b"VERONA__", cks, h["ver"], b"\0" * 7, h["paddings"], h["plen"], h["flen"])
    new_body = hdr + body[32:32 + h["plen"] + h["flen"]]
    with open(img, "r+b") as f:
        f.seek(off)
        f.write(new_body)


def edge_duplicate_write_direct(platform):
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    f1 = test_path("d1", ".bin")
    f2 = test_path("d2", ".bin")
    gen_random_file(f1, 32)
    gen_random_file(f2, 32)
    before = sha256(img)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{f1}", "-w", f"{slot}{sep()}{f2}"])
    after = sha256(img)
    if rc == 5 and before == after:
        record("pass", f"edge_duplicate_write_direct platform={platform} slot={slot}")
    else:
        record("genuine_failure", f"edge_duplicate_write_direct platform={platform} slot={slot} rc={rc} hash_unchanged={before==after}")
    cleanup(img, f1, f2)


def edge_duplicate_write_shortcut_collision(platform):
    img = fresh(platform)
    name = random.choice(list(SHORTCUT_SLOTS))
    colliding_slot = SHORTCUT_SLOTS[name][0]
    f1 = test_path("c1", ".bin")
    f2 = test_path("c2", ".bin")
    gen_random_file(f1, 32)
    gen_random_file(f2, 32)
    before = sha256(img)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"--{name}={f1}", "-w", f"{colliding_slot}{sep()}{f2}"])
    after = sha256(img)
    if rc == 5 and before == after:
        record("pass", f"edge_duplicate_write_shortcut_collision platform={platform} name={name}")
    else:
        record("genuine_failure", f"edge_duplicate_write_shortcut_collision platform={platform} name={name} rc={rc} hash_unchanged={before==after}")
    cleanup(img, f1, f2)


def edge_dry_run_overflow(platform):
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img, slot) if platform == "duo" else monarch_boundary(img, slot)
    if boundary > MAX_PRACTICAL_BOUNDARY:
        cleanup(img)
        return
    src = test_path("ov", ".bin")
    gen_random_file(src, boundary + 1024)
    before = sha256(img)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-n", "-w", f"{slot}{sep()}{src}"])
    after = sha256(img)
    text = err.decode("utf-8", "replace")
    if rc == 0 and before == after and "would fail" in text:
        record("pass", f"edge_dry_run_overflow platform={platform} slot={slot}")
    else:
        record("genuine_failure", f"edge_dry_run_overflow platform={platform} slot={slot} rc={rc} hash_unchanged={before==after} stderr={text[:200]!r}")
    cleanup(img, src)


def edge_dry_run_conflict(platform):
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    src = test_path("dc", ".bin")
    gen_random_file(src, 32)
    before = sha256(img)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-n", "-w", f"{slot}{sep()}{src}", "-w", f"{slot}{sep()}{src}"])
    after = sha256(img)
    text = err.decode("utf-8", "replace")
    if rc == 0 and before == after and "would fail" in text:
        record("pass", f"edge_dry_run_conflict platform={platform} slot={slot}")
    else:
        record("genuine_failure", f"edge_dry_run_conflict platform={platform} slot={slot} rc={rc} hash_unchanged={before==after} stderr={text[:200]!r}")
    cleanup(img, src)


def edge_restore_corrupted(platform):
    img = fresh(platform)
    off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
    h = read_header(img, off)
    backup = test_path("rc", ".img")
    with open(img, "rb") as f:
        f.seek(off)
        good = f.read(h["paddings"])
    # Corrupted: truncated to half.
    with open(backup, "wb") as f:
        f.write(good[: len(good) // 2])
    before = open_table_bytes(img, off, h["paddings"])
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-R", backup])
    after = open_table_bytes(img, off, h["paddings"])
    if rc == 2 and before == after:
        record("pass", f"edge_restore_corrupted platform={platform}")
    else:
        record("genuine_failure", f"edge_restore_corrupted platform={platform} rc={rc} unchanged={before==after} err={err!r}")
    cleanup(img, backup)


def open_table_bytes(img, off, length):
    with open(img, "rb") as f:
        f.seek(off)
        return f.read(length)


def table_region_boundary(img, platform):
    """Mirror of Device.TableRegionBoundary() in Go: how large a
    replacement table may be without overflowing into real data. Needed
    because the table's own region size differs a lot between platforms
    (Monarch's FW_TABLE GPT partition is ~1MB; Duo's is bounded by its
    nearest firmware entry's offset, often much smaller) -- a fixed
    multiplier that's oversized on one platform is not guaranteed to be
    oversized on the other."""
    if platform == "duo":
        off = DUO_TABLE_OFFSET
        h = read_header(img, off)
        fws = read_fw_entries(img, off, h["plen"], h["flen"])
        min_off = min(f["offset"] for f in fws)
        return min_off - off
    off, parts, _ = monarch_table_offset(img)
    idx = parts.index(off // 512)
    return parts[idx + 1] * 512 - off


def edge_restore_oversized_paddings(platform):
    img = fresh(platform)
    off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
    h = read_header(img, off)
    backup = test_path("ro", ".img")
    with open(img, "rb") as f:
        f.seek(off)
        good = f.read(h["paddings"])
    with open(backup, "wb") as f:
        f.write(good)
    # Patch the backup's own Paddings field to something guaranteed to
    # exceed the REAL available table-region space on this platform (not
    # a fixed multiplier -- Monarch's FW_TABLE partition is ~1MB, far
    # bigger than a fixed small multiple of the original ~1KB table size
    # would be "oversized" relative to), with a correctly-recomputed
    # checksum so it passes signature/checksum validation and only fails
    # the size-vs-available-space check.
    boundary = table_region_boundary(img, platform)
    write_table_header(backup, 0, paddings=boundary + 4096)
    before = open_table_bytes(img, off, h["paddings"])
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-R", backup])
    after = open_table_bytes(img, off, h["paddings"])
    if rc == 2 and before == after:
        record("pass", f"edge_restore_oversized_paddings platform={platform}")
    else:
        record("genuine_failure", f"edge_restore_oversized_paddings platform={platform} rc={rc} unchanged={before==after} err={err!r}")
    cleanup(img, backup)


def edge_help_version_combined():
    for flag in ["-h", "-v"]:
        rc, out, err = verona(["-p", "mch", flag])
        text = out.decode("utf-8", "replace")
        if rc == 1 and "verona-tool" not in text.split("\n")[0]:
            record("pass", f"edge_help_version_combined flag={flag}")
        else:
            record("genuine_failure", f"edge_help_version_combined flag={flag} rc={rc} out={text[:100]!r}")


def edge_garbage_slots(platform):
    img = fresh(platform)
    cases = [("-1", {1, 4}), ("abc", {1}), ("99999999", {4})]
    ok = True
    for val, acceptable_rc in cases:
        rc, out, err = run(QEMU + [VERONA, "-p", platform_flag(platform), "-d", img, "-r", val])
        if rc not in acceptable_rc:
            record("genuine_failure", f"edge_garbage_slots platform={platform} val={val} rc={rc} not in {acceptable_rc}")
            ok = False
    if ok:
        record("pass", f"edge_garbage_slots platform={platform}")
    cleanup(img)


def edge_out_of_range_slot(platform):
    """A numerically well-formed, plausible-looking slot number that is
    simply past the end of the real table (fwcount + a small offset) must
    be refused with the specific 'slot index out of range' error (exit 4)
    -- distinct from edge_garbage_slots's negative/non-numeric/absurd
    inputs, which are rejected earlier as plain exit-1 argument errors.
    Also confirms the refused -w left the device completely untouched
    (whole-image hash unchanged), not just that it returned the right
    exit code. Registered in both the dedicated edge-scenario list AND
    the regular per-platform rotation, so out-of-range attempts recur
    throughout a run at a noticeable but not overwhelming frequency,
    rather than only in a single one-off edge pass."""
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = n + random.randint(1, 500)
    src = test_path("oor", ".bin")
    gen_random_file(src, 32)
    ok = True
    before = sha256(img)
    rc_r, out_r, err_r = verona(["-p", platform_flag(platform), "-d", img, "-r", str(slot)])
    if rc_r != 4:
        record("genuine_failure", f"edge_out_of_range_slot platform={platform} slot={slot} -r rc={rc_r} err={err_r!r}")
        ok = False
    rc_w, out_w, err_w = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
    if rc_w != 4:
        record("genuine_failure", f"edge_out_of_range_slot platform={platform} slot={slot} -w rc={rc_w} err={err_w!r}")
        ok = False
    after = sha256(img)
    if before != after:
        record("genuine_failure", f"edge_out_of_range_slot platform={platform} slot={slot}: device changed despite refused out-of-range write")
        ok = False
    if ok:
        record("pass", f"edge_out_of_range_slot platform={platform} slot={slot} (fwcount={n})")
    cleanup(img, src, os.path.join(SCRATCH, f"{slot}.img"))


def edge_debug_flag(platform):
    """-D/--debug must produce visibly more output than the same operation
    without it, and must not change the resulting file/table state at all."""
    img_plain = fresh(platform)
    img_debug = fresh(platform)
    n = fwcount(img_plain, platform)
    slot = random.randint(0, n - 1)
    out_plain = test_path("dbgp", ".img")
    out_debug = test_path("dbgd", ".img")
    debug_flag = random.choice(["-D", "--debug"])

    rc1, stdout1b, err1 = verona(["-p", platform_flag(platform), "-d", img_plain, "-r", f"{slot}{sep()}{out_plain}"])
    rc2, stdout2b, err2 = verona(["-p", platform_flag(platform), "-d", img_debug, debug_flag, "-r", f"{slot}{sep()}{out_debug}"])
    stdout1 = stdout1b.decode("utf-8", "replace")
    stdout2 = stdout2b.decode("utf-8", "replace")

    ok = True
    if rc1 != 0 or rc2 != 0:
        record("genuine_failure", f"edge_debug_flag platform={platform} slot={slot} rc1={rc1} rc2={rc2} err1={err1!r} err2={err2!r}")
        ok = False
    if "[debug]" in stdout1:
        record("genuine_failure", f"edge_debug_flag platform={platform}: plain run unexpectedly contains [debug] output")
        ok = False
    if "[debug]" not in stdout2 or len(stdout2.splitlines()) <= len(stdout1.splitlines()):
        record("genuine_failure", f"edge_debug_flag platform={platform}: -u run not visibly more verbose (plain={len(stdout1.splitlines())} lines, debug={len(stdout2.splitlines())} lines)")
        ok = False
    if sha256(img_plain) != sha256(img_debug):
        record("genuine_failure", f"edge_debug_flag platform={platform}: resulting device state differs between plain and -u runs")
        ok = False
    if os.path.exists(out_plain) and os.path.exists(out_debug) and open(out_plain, "rb").read() != open(out_debug, "rb").read():
        record("genuine_failure", f"edge_debug_flag platform={platform}: read-out file contents differ between plain and -u runs")
        ok = False
    if ok:
        record("pass", f"edge_debug_flag platform={platform} slot={slot} flag={debug_flag}")
    cleanup(img_plain, img_debug, out_plain, out_debug)


def edge_dry_run_alone(platform):
    rc, out, err = verona(["-p", platform_flag(platform), "-n"])
    if rc == 1:
        record("pass", f"edge_dry_run_alone platform={platform}")
    else:
        record("genuine_failure", f"edge_dry_run_alone platform={platform} rc={rc}")


def edge_concurrent_write_lock(platform):
    """Simulate a second process already holding the write lock on -d by
    taking it ourselves (fcntl, non-blocking LOCK_EX) before invoking
    verona-tool -- it must fail immediately with exit 7, no blocking."""
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    src = test_path("lk", ".bin")
    gen_random_file(src, 32)
    fd = os.open(img, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        t0 = time.time()
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
        elapsed = time.time() - t0
        if rc == 7 and elapsed < 5:
            record("pass", f"edge_concurrent_write_lock platform={platform}")
        else:
            record("genuine_failure", f"edge_concurrent_write_lock platform={platform} rc={rc} elapsed={elapsed:.1f}s")
    finally:
        os.close(fd)
    cleanup(img, src)


def edge_concurrent_read_shared(platform):
    """A shared (read) lock held externally must NOT block a verona-tool
    read-only invocation (-r) against the same -d."""
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    fd = os.open(img, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", str(slot)])
        if rc == 0:
            record("pass", f"edge_concurrent_read_shared platform={platform}")
        else:
            record("genuine_failure", f"edge_concurrent_read_shared platform={platform} rc={rc} err={err!r}")
    finally:
        os.close(fd)
    cleanup(img, os.path.join(SCRATCH, f"{slot}.img"))


def edge_corrupted_table(platform):
    """Three kinds of table corruption, each checked against every
    operation that must refuse it with exit 2."""
    off_base = None
    kinds = ["bad_sig", "misaligned_len", "absurd_count"]
    kind = random.choice(kinds)
    ops = random.sample(["-l", "-r", "-w", "-B", "-R"], 2)
    for op in ops:
        img = fresh(platform)
        n = fwcount(img, platform)
        slot = random.randint(0, n - 1)
        off = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img)[0]
        with open(img, "r+b") as f:
            f.seek(off)
            if kind == "bad_sig":
                f.write(b"NOTAREAL")
            elif kind == "misaligned_len":
                f.seek(off + 24)
                f.write(struct.pack("<I", 47))  # not a multiple of 48
            else:
                f.seek(off + 28)
                f.write(struct.pack("<I", 20000 * 32))  # fwcount > 10000 cap
        src = test_path("ct", ".bin")
        gen_random_file(src, 32)
        if op == "-l":
            rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-l"])
        elif op == "-r":
            rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", str(slot)])
        elif op == "-w":
            rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
        elif op == "-B":
            rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-B", test_path("ctb", ".img"), "-r", str(slot)])
        else:  # -R
            dummy = test_path("ctd", ".img")
            gen_random_file(dummy, 1024)
            rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-R", dummy])
            cleanup(dummy)
        if rc == 2:
            record("pass", f"edge_corrupted_table platform={platform} kind={kind} op={op}")
        else:
            record("genuine_failure", f"edge_corrupted_table platform={platform} kind={kind} op={op} rc={rc} err={err!r}")
        cleanup(img, src, os.path.join(SCRATCH, f"{slot}.img"))


def edge_gpt_no_fw_table():
    img = test_path("nofw", ".img")
    subprocess.run(["truncate", "-s", "20000000", img], check=True)
    subprocess.run(["sgdisk", "-Z", img], check=True, capture_output=True)
    subprocess.run(["sgdisk", "-a", "1", "-n", "1:2048:20000", "-c", "1:NOT_FW_TABLE", img], check=True, capture_output=True)
    rc, out, err = verona(["-p", "mch", "-d", img, "-l"])
    if rc == 2:
        record("pass", "edge_gpt_no_fw_table")
    else:
        record("genuine_failure", f"edge_gpt_no_fw_table rc={rc} err={err!r}")
    cleanup(img)


def edge_fw_table_no_signature():
    img = test_path("nosig", ".img")
    subprocess.run(["truncate", "-s", "20000000", img], check=True)
    subprocess.run(["sgdisk", "-Z", img], check=True, capture_output=True)
    subprocess.run(["sgdisk", "-a", "1", "-n", "1:2048:20000", "-c", "1:FW_TABLE", img], check=True, capture_output=True)
    # Leave the partition's content as zeros -- no VERONA signature there.
    rc, out, err = verona(["-p", "mch", "-d", img, "-l"])
    if rc == 2:
        record("pass", "edge_fw_table_no_signature")
    else:
        record("genuine_failure", f"edge_fw_table_no_signature rc={rc} err={err!r}")
    cleanup(img)


def edge_mch_deleted_partition_boundary():
    """A Monarch firmware slot's table-recorded Offset must correspond to
    a real GPT partition's own StartingLBA. If that backing partition
    was deleted (or the table otherwise disagrees with the live GPT),
    verona-tool must refuse (exit 2) rather than silently falling
    through to whatever partition happens to come next on the disk --
    which could produce a much larger, wrong write boundary. Deletes one
    partition entry directly in the GPT array (zeroing its type GUID,
    exactly what marks an entry as unused/deleted) while leaving the
    VERONA table's own Offset for that slot completely untouched."""
    img = fresh("mch")
    n = fwcount(img, "mch")
    candidates = writable_slots(img, "mch", n)
    if not candidates:
        cleanup(img)
        return
    slot, _ = random.choice(candidates)
    table_off, _, _ = monarch_table_offset(img)
    h = read_header(img, table_off)
    fws = read_fw_entries(img, table_off, h["plen"], h["flen"])
    start_lba = fws[slot]["offset"] // 512

    with open(img, "rb") as f:
        f.seek(512)
        hdr = f.read(512)
        part_lba = struct.unpack_from("<Q", hdr, 72)[0]
        num = struct.unpack_from("<I", hdr, 80)[0]
        esize = struct.unpack_from("<I", hdr, 84)[0]

    deleted = False
    with open(img, "r+b") as f:
        for i in range(num):
            entry_off = part_lba * 512 + i * esize
            f.seek(entry_off)
            e = f.read(esize)
            if e[0:16] == b"\x00" * 16:
                continue
            s = struct.unpack_from("<Q", e, 32)[0]
            if s == start_lba:
                f.seek(entry_off)
                f.write(b"\x00" * 16)  # zero the type GUID -> "deleted"
                deleted = True
                break

    if not deleted:
        record("genuine_failure", f"edge_mch_deleted_partition_boundary: could not find/delete the GPT partition backing slot {slot} (start_lba={start_lba})")
        cleanup(img)
        return

    src = test_path("delpart", ".bin")
    gen_random_file(src, 32)
    before = sha256(img)
    rc, out, err = verona(["-p", "mch", "-d", img, "-w", f"{slot}:{src}"])
    after = sha256(img)
    errtext = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
    if rc == 2 and "no gpt partition" in errtext.lower() and before == after:
        record("pass", f"edge_mch_deleted_partition_boundary slot={slot}")
    else:
        record("genuine_failure", f"edge_mch_deleted_partition_boundary slot={slot} rc={rc} unchanged={before==after} err={errtext!r}")
    cleanup(img, src)


def edge_backup_before_failed_write(platform):
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img, slot) if platform == "duo" else monarch_boundary(img, slot)
    if boundary > MAX_PRACTICAL_BOUNDARY:
        cleanup(img)
        return
    big = test_path("bf", ".bin")
    gen_random_file(big, boundary + 4096)
    backup = test_path("bfb", ".img")
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-B", backup, "-w", f"{slot}{sep()}{big}"])
    backup_exists = os.path.exists(backup) and os.path.getsize(backup) > 0
    if rc == 3 and backup_exists:
        record("pass", f"edge_backup_before_failed_write platform={platform}")
    else:
        record("genuine_failure", f"edge_backup_before_failed_write platform={platform} rc={rc} backup_exists={backup_exists}")
    cleanup(img, big, backup)


def edge_separator_consistency(platform):
    img_colon = fresh(platform)
    img_eq = fresh(platform)
    n = fwcount(img_colon, platform)
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img_colon, slot) if platform == "duo" else monarch_boundary(img_colon, slot)
    src = test_path("sep", ".bin")
    gen_random_file(src, min(4096, max(1, boundary)))
    rc1, _, err1 = verona(["-p", platform_flag(platform), "-d", img_colon, "-w", f"{slot}{sep()}{src}"])
    rc2, _, err2 = verona(["-p", platform_flag(platform), "-d", img_eq, "-w", f"{slot}={src}"])
    same = sha256(img_colon) == sha256(img_eq)
    if rc1 == 0 and rc2 == 0 and same:
        record("pass", f"edge_separator_consistency platform={platform} slot={slot}")
    else:
        record("genuine_failure", f"edge_separator_consistency platform={platform} slot={slot} rc1={rc1} rc2={rc2} same={same}")
    cleanup(img_colon, img_eq, src)


def edge_backup_twice_same_second(platform):
    img = fresh(platform)
    n = fwcount(img, platform)
    slot = random.randint(0, n - 1)
    rc1, out1, err1 = verona(["-p", platform_flag(platform), "-d", img, "-B", "-r", str(slot)])
    rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img, "-B", "-r", str(slot)])
    names = sorted(f for f in os.listdir(SCRATCH) if f.startswith(f"verona-table-backup-{platform}-"))
    detail = f"edge_backup_twice_same_second platform={platform} rc1={rc1} rc2={rc2} produced={names}"
    if rc1 == 0 and rc2 == 0:
        if len(names) >= 2:
            record("pass", detail + " (two distinct backups)")
        else:
            record("pass", detail + " (NOTE: same-second default filenames collide -- second backup silently overwrote the first; no data loss observed here since content was identical, but flagging as a real behavior to be aware of)")
    else:
        record("genuine_failure", detail)
    cleanup(img, os.path.join(SCRATCH, f"{slot}.img"), *[os.path.join(SCRATCH, bn) for bn in names])


SCENARIOS_DUO = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal,
                 scenario_backup_restore_list, scenario_list_check, scenario_shortcut_read,
                 scenario_shortcut_write, scenario_multi_read,
                 scenario_duo_fwtutil_crosscheck, scenario_duo_fwtutil_crosscheck,
                 scenario_duo_fwtutil_multiflag, scenario_duo_fwtutil_multiflag,
                 edge_out_of_range_slot]
SCENARIOS_MCH = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal,
                 scenario_backup_restore_list, scenario_list_check, scenario_shortcut_read,
                 scenario_shortcut_write, scenario_multi_read,
                 scenario_mch_fwtablectl_crosscheck, scenario_mch_fwtablectl_crosscheck,
                 edge_out_of_range_slot]


def edge_arg_fuzzing(platform):
    """Malformed-input robustness: an absurdly huge slot number, an
    empty value where one is required, a near-NAME_MAX-length file
    path, a flag repeated many times in one command, and an unknown
    flag -- all must be rejected cleanly (exit 1), never hang or
    crash."""
    img = fresh(platform)
    ok = True

    def check(label, args):
        nonlocal ok
        rc, out, err = verona(args)
        if rc != 1:
            record("genuine_failure", f"edge_arg_fuzzing[{label}] platform={platform} rc={rc} err={err!r}")
            ok = False

    check("huge_slot", ["-p", platform_flag(platform), "-d", img, "-r", "999999999999999999999999"])
    check("empty_value", ["-p", platform_flag(platform), "-d", img, "-w", "5:"])
    long_name = "x" * 4000 + ".bin"
    check("long_path", ["-p", platform_flag(platform), "-d", img, "-r", f"0:{os.path.join(SCRATCH, long_name)}"])
    check("repeated_flag", ["-p", platform_flag(platform), "-d", img] + ["-l"] * 50 + ["-r", "0"])
    check("unknown_flag", ["-p", platform_flag(platform), "-d", img, "--bogus-flag"])

    if ok:
        record("pass", f"edge_arg_fuzzing platform={platform}")
    cleanup(img, os.path.join(os.getcwd(), "0.img"))


def edge_flag_empty_or_missing_values(platform):
    """Every flag that takes a value, tried with that value missing or
    empty (not just -w, already covered by edge_arg_fuzzing): -p, -d
    (mch's required case), -r, -R, -B (via --backup= specifically),
    and the -k/-t/-f shortcuts both alone (no -r/-w to select a mode)
    and in write mode with no/empty file. All must cleanly refuse
    (exit 1), never crash."""
    img = fresh(platform)
    ok = True

    def check(label, args):
        nonlocal ok
        rc, out, err = verona(args)
        if rc != 1:
            record("genuine_failure", f"edge_flag_empty_or_missing_values[{label}] platform={platform} rc={rc} err={err!r}")
            ok = False

    check("p_empty", ["-p", "", "-d", img, "-l"])
    check("p_missing", ["-p"])
    check("d_empty_mch", ["-p", "mch", "-d", "", "-l"])
    check("r_empty", ["-p", platform_flag(platform), "-d", img, "-r", ""])
    check("R_missing", ["-p", platform_flag(platform), "-d", img, "-R"])
    check("B_empty_filename", ["-p", platform_flag(platform), "-d", img, "--backup=", "-r", "0"])
    check("kernel_alone_no_mode", ["-p", platform_flag(platform), "-d", img, "-k"])
    check("dtb_alone_no_mode", ["-p", platform_flag(platform), "-d", img, "-t"])
    check("rootfs_alone_no_mode", ["-p", platform_flag(platform), "-d", img, "-f"])
    check("kernel_write_no_file", ["-p", platform_flag(platform), "-d", img, "-w", "--kernel"])
    check("kernel_write_empty_file", ["-p", platform_flag(platform), "-d", img, "-w", "--kernel="])

    if ok:
        record("pass", f"edge_flag_empty_or_missing_values platform={platform}")
    cleanup(img, os.path.join(os.getcwd(), "0.img"))


def edge_repeated_nonrepeatable_flags(platform):
    """Flags that are NOT meant to be repeated -- unlike -r/-w and the
    shortcuts (which legitimately repeat; a shortcut given twice in
    write mode is covered separately as a slot conflict, exit 5, by
    edge_duplicate_write_shortcut_collision) -- must cleanly refuse a
    second occurrence (exit 1) rather than silently keeping the last
    (or first) value or crashing. Non-repeatable: -p, -d, -l, -R, -B,
    -n, -D. (-h/-v are already covered by main.go's "must be used
    alone" check against argv's total length, exercised by
    edge_help_version_combined.)"""
    img = fresh(platform)
    ok = True

    def check(label, args):
        nonlocal ok
        rc, out, err = verona(args)
        if rc != 1:
            record("genuine_failure", f"edge_repeated_nonrepeatable_flags[{label}] platform={platform} rc={rc} err={err!r}")
            ok = False

    check("p_twice", ["-p", "duo", "-p", "mch", "-d", img, "-l"])
    check("d_twice", ["-p", platform_flag(platform), "-d", img, "-d", img, "-l"])
    check("l_twice", ["-p", platform_flag(platform), "-d", img, "-l", "-l"])
    check("n_twice", ["-p", platform_flag(platform), "-d", img, "-n", "-n", "-r", "0"])
    check("D_twice", ["-p", platform_flag(platform), "-d", img, "-D", "-D", "-r", "0"])
    check("B_twice", ["-p", platform_flag(platform), "-d", img, "-B", "-B", "-r", "0"])
    check("R_twice", ["-p", platform_flag(platform), "-d", img, "-R", "/tmp/a.img", "-R", "/tmp/b.img"])

    if ok:
        record("pass", f"edge_repeated_nonrepeatable_flags platform={platform}")
    cleanup(img, os.path.join(os.getcwd(), "0.img"))


def edge_missing_required_params(platform):
    """Commands missing a required parameter must cleanly refuse (exit
    1) rather than crash: no -p at all, no args at all, -w/-R with no
    value, -d omitted when required for mch, and -p/-d given but no
    action flag (-r/-w/-l/-R/-B) at all."""
    img = fresh(platform)
    ok = True

    def check(label, args):
        nonlocal ok
        rc, out, err = verona(args)
        if rc != 1:
            record("genuine_failure", f"edge_missing_required_params[{label}] platform={platform} rc={rc} err={err!r}")
            ok = False

    check("no_p_at_all", ["-d", img, "-l"])
    check("no_args_at_all", [])
    check("w_no_value", ["-p", platform_flag(platform), "-d", img, "-w"])
    check("R_no_value", ["-p", platform_flag(platform), "-d", img, "-R"])
    check("mch_no_d", ["-p", "mch", "-l"])
    check("no_action_flag", ["-p", platform_flag(platform), "-d", img])

    if ok:
        record("pass", f"edge_missing_required_params platform={platform}")
    cleanup(img)


def edge_bare_flag_without_shortcut(platform):
    """Real bug found by the user, fixed in two rounds:

    Round 1: a bare -r/-w (no attached SLOT[:FILE|=FILE], no following
    value token) interspersed among OTHER valid valued -r/-w
    occurrences used to pass argument parsing silently -- the bare
    occurrence just got dropped, because the pre-existing "-r/-w given
    but no slot" check only looks at the aggregate reads/writes list,
    which wasn't empty thanks to the other valid occurrences.

    Round 2: the round-1 fix only checked "is at least one shortcut
    present ANYWHERE" as a global existence check, so it never caught
    EXTRA bare -r/-w occurrences beyond the first meaningful one (e.g.
    "-r --kernel -r -r --kernel -r" has a real shortcut present, so it
    still passed). The user corrected an initial overcorrection on this
    round too: the real rule is not "at most one bare -r in the whole
    invocation" -- it's "every bare -r/-w must be immediately followed
    by a -k/-t/-f shortcut, which gives THAT occurrence meaning." A
    bare -r directly followed by a shortcut is satisfied, no matter how
    many such (bare, shortcut) pairs appear; a bare -r/-w NOT
    immediately followed by a shortcut (followed by another bare -r/
    -w, by -w/-r with no shortcut, or by nothing at the end of argv) is
    dangling and a usage error. This does NOT apply to VALUED -r/-w
    (e.g. "-r 5:file"), which remains legitimately repeatable
    throughout, including appearing more than once.

    Covers the user's exact example commands from both rounds (plus
    the user's own correction -- "-r --dtb -r --dtb" is legitimate,
    NOT an error, since each bare -r there is immediately followed by
    its own shortcut), the single-bare-plus-multiple-shortcuts case,
    and confirms valued -r remains repeatable."""
    img = fresh(platform)
    ok = True

    def check(label, args, expect=1):
        nonlocal ok
        rc, out, err = verona(args)
        if rc != expect:
            record("genuine_failure", f"edge_bare_flag_without_shortcut[{label}] platform={platform} rc={rc} (expected {expect}) err={err!r}")
            ok = False

    # Round 1's exact two example commands (device doesn't need to
    # exist -- the bug is that these used to pass parsing and reach the
    # open() call at all; now they must be rejected before that).
    check("r1_example1_bare_w_no_shortcut", ["-p", platform_flag(platform), "-d", "/dev/sdb-does-not-exist",
                                             "-w", "-w", "1=1.img", "-w", "-w"])
    check("r1_example2_bare_r_no_shortcut", ["-p", platform_flag(platform), "-d", "/dev/sdb-does-not-exist",
                                             "-r", "0", "-r", "1", "-r", "2", "-r", "-r", "-r", "4"])

    # Round 2's exact two example commands: a real shortcut IS present,
    # but at least one bare -r is NOT immediately followed by a
    # shortcut (followed by another bare -r, or by -w) -- must be
    # rejected despite a legitimate shortcut appearing elsewhere.
    check("r2_example1_dangling_bare_r_with_shortcut", ["-p", platform_flag(platform), "-d", "/dev/sdb-does-not-exist",
                                                        "-r", "--kernel", "-r", "-r", "--kernel", "-r"])
    check("r2_example2_dangling_bare_r_before_valued_w", ["-p", platform_flag(platform), "-d", "/dev/sdb-does-not-exist",
                                                           "-r", "--kernel", "-r", "-w", "1:1.img"])

    # Bare -r/-w alone, no shortcut at all -- dangling.
    check("bare_r_r_alone", ["-p", platform_flag(platform), "-d", img, "-r", "-r"])
    check("bare_w_w_alone", ["-p", platform_flag(platform), "-d", img, "-w", "-w"])

    # Bare write immediately followed by nothing (end of argv), with
    # only a READ-mode shortcut present earlier -- the earlier shortcut
    # doesn't retroactively satisfy this dangling bare -w.
    check("bare_w_with_only_read_shortcut", ["-p", platform_flag(platform), "-d", img, "-r", "--dtb", "-w"])
    cleanup(os.path.join(os.getcwd(), "6.img"), os.path.join(os.getcwd(), "7.img"))

    # User's own correction: a bare -r repeated is fine as long as EACH
    # occurrence is immediately followed by its own shortcut -- this is
    # NOT a dangling bare -r and must succeed (duplicate reads of the
    # same slots are explicitly allowed elsewhere in the spec, just a
    # warning).
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", "--dtb", "-r", "--dtb"])
    if rc != 0:
        record("genuine_failure", f"edge_bare_flag_without_shortcut[bare_r_repeated_each_with_own_shortcut] platform={platform} rc={rc} err={err!r}")
        ok = False
    cleanup(os.path.join(os.getcwd(), "6.img"), os.path.join(os.getcwd(), "7.img"))

    # Legitimate: exactly ONE bare -r immediately followed by multiple
    # shortcuts must still succeed.
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", "--kernel", "--dtb"])
    if rc != 0:
        record("genuine_failure", f"edge_bare_flag_without_shortcut[legit_one_bare_r_multi_shortcut] platform={platform} rc={rc} err={err!r}")
        ok = False
    cleanup(os.path.join(os.getcwd(), "5.img"), os.path.join(os.getcwd(), "12.img"),
            os.path.join(os.getcwd(), "6.img"), os.path.join(os.getcwd(), "7.img"))

    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if candidates:
        kfile = test_path("bareshortcut", ".bin")
        gen_random_file(kfile, 1024)
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"--kernel={kfile}"])
        if rc != 0:
            record("genuine_failure", f"edge_bare_flag_without_shortcut[legit_bare_w_shortcut] platform={platform} rc={rc} err={err!r}")
            ok = False
        cleanup(kfile)

    # Valued -r remains legitimately repeatable throughout both rounds.
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", "0", "-r", "1"])
    if rc != 0:
        record("genuine_failure", f"edge_bare_flag_without_shortcut[valued_r_still_repeatable] platform={platform} rc={rc} err={err!r}")
        ok = False
    cleanup(os.path.join(os.getcwd(), "0.img"), os.path.join(os.getcwd(), "1.img"))

    if ok:
        record("pass", f"edge_bare_flag_without_shortcut platform={platform}")
    cleanup(img)


def edge_kill_mid_write_lock_release(platform):
    """Kill verona-tool (SIGKILL or SIGTERM) partway through a -w and
    confirm the flock on the device is released when the process dies,
    so a subsequent invocation does not hang waiting on a stale lock.
    flock is tied to the process's file descriptor table and is always
    released on process death regardless of how it died, so this is
    really confirming verona-tool uses real flock() and not some
    separate lockfile scheme that could outlive the process."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    size = min(boundary, 16 * 1024 * 1024)
    src = test_path("kill", ".bin")
    gen_random_file(src, size)

    args = ["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"]
    log_cmd("verona-tool", args + ["# killed mid-write"])
    proc = subprocess.Popen(QEMU + [VERONA] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(random.uniform(0.05, 0.2))
    sig = random.choice([signal.SIGKILL, signal.SIGTERM])
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass  # it finished before we could signal it -- nothing to test this time
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)

    t0 = time.time()
    rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img, "-l"])
    elapsed = time.time() - t0
    if rc2 == 0 and elapsed < 10:
        record("pass", f"edge_kill_mid_write_lock_release platform={platform} slot={slot} sig={sig.name} elapsed={elapsed:.2f}s")
    else:
        record("genuine_failure", f"edge_kill_mid_write_lock_release platform={platform} slot={slot} sig={sig.name} rc2={rc2} elapsed={elapsed:.2f}s err={err2!r}")
    cleanup(img, src)


def edge_debug_and_dryrun_combined(platform):
    """-D and -n together in one invocation: debug output must describe
    the dry-run plan correctly, with no crash or malformed output, and
    the device must end up completely unchanged (it's still a dry run)."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    src = test_path("dn", ".bin")
    gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
    before = sha256(img)
    flag_d = random.choice(["-D", "--debug"])
    flag_n = random.choice(["-n", "--dry-run"])
    rc, outb, err = verona(["-p", platform_flag(platform), "-d", img, flag_d, flag_n, "-w", f"{slot}{sep()}{src}"])
    out = outb.decode("utf-8", "replace")
    after = sha256(img)
    ok = True
    if rc != 0:
        record("genuine_failure", f"edge_debug_and_dryrun_combined platform={platform} slot={slot} rc={rc} err={err!r}")
        ok = False
    if "[debug]" not in out:
        record("genuine_failure", f"edge_debug_and_dryrun_combined platform={platform} slot={slot}: no [debug] output with -D -n combined")
        ok = False
    if "would write" not in out:
        record("genuine_failure", f"edge_debug_and_dryrun_combined platform={platform} slot={slot}: no dry-run plan ('would write') in output")
        ok = False
    if before != after:
        record("genuine_failure", f"edge_debug_and_dryrun_combined platform={platform} slot={slot}: device changed despite -n")
        ok = False
    if ok:
        record("pass", f"edge_debug_and_dryrun_combined platform={platform} slot={slot}")
    cleanup(img, src)


def edge_idempotent_rewrite(platform):
    """Writing the exact same content to the same slot twice in a row
    must succeed cleanly both times (checksum recompute is idempotent,
    no spurious 'already written'/stale-state error), and a subsequent
    read must match."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    src = test_path("idem", ".bin")
    gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
    rc1, out1, err1 = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
    rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
    if rc1 != 0 or rc2 != 0:
        record("genuine_failure", f"edge_idempotent_rewrite platform={platform} slot={slot} rc1={rc1} rc2={rc2} err1={err1!r} err2={err2!r}")
        cleanup(img, src)
        return
    if not verify_payload(img, platform, slot, src, f"edge_idempotent_rewrite platform={platform} slot={slot}"):
        cleanup(img, src)
        return
    readback = test_path("idemr", ".bin")
    rc3, out3, err3 = verona(["-p", platform_flag(platform), "-d", img, "-r", f"{slot}{sep()}{readback}"])
    ok = True
    if rc3 != 0:
        record("genuine_failure", f"edge_idempotent_rewrite readback platform={platform} slot={slot} rc3={rc3} err={err3!r}")
        ok = False
    else:
        with open(src, "rb") as f1, open(readback, "rb") as f2:
            same = f1.read() == f2.read()
        if not same:
            record("genuine_failure", f"edge_idempotent_rewrite platform={platform} slot={slot}: readback mismatch after double write")
            ok = False
    if ok:
        record("pass", f"edge_idempotent_rewrite platform={platform} slot={slot}")
    cleanup(img, src, readback)


def edge_list_empty_entries(platform):
    """-l on a table with a mix of empty (length=0, the real uBoot-style
    empty-slot pattern) and populated entries must format every line
    correctly -- no crash, no garbled numbers -- for both kinds in the
    same listing."""
    img = fresh(platform)
    rc, outb, err = verona(["-p", platform_flag(platform), "-d", img, "-l"])
    out = outb.decode("utf-8", "replace")
    if rc != 0:
        record("genuine_failure", f"edge_list_empty_entries platform={platform} rc={rc} err={err!r}")
        cleanup(img)
        return
    matches = _FW_RE.findall(out)
    empty = [m for m in matches if m[7] == "0"]
    populated = [m for m in matches if m[7] != "0"]
    if empty and populated:
        record("pass", f"edge_list_empty_entries platform={platform} empty={len(empty)} populated={len(populated)}")
    elif not empty:
        record("expected_divergence", f"edge_list_empty_entries platform={platform}: golden image has no empty (length=0) entries to exercise -- not a verona-tool issue")
    else:
        record("genuine_failure", f"edge_list_empty_entries platform={platform}: no populated entries parsed alongside the empty one(s)")
    cleanup(img)


def edge_same_path_read_then_write(platform):
    """The same file path used as a -r output in one invocation and then
    as a -w source LATER IN THE SAME invocation (-r 5:data.bin -w
    7:data.bin) -- confirms the read fully completes and closes before
    the write reopens that same path as its source, with the correct
    resulting content in the write's target slot."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    read_slot = write_slot = None
    for rs, _ in candidates:
        _, _, rlen = read_slot_payload(img, platform, rs)
        for ws, wb in candidates:
            if ws != rs and rlen <= wb:
                read_slot, write_slot = rs, ws
                break
        if read_slot is not None:
            break
    if read_slot is None:
        cleanup(img)
        return
    shared = test_path("shared", ".bin")

    rc, out, err = verona(["-p", platform_flag(platform), "-d", img,
                            "-r", f"{read_slot}{sep()}{shared}",
                            "-w", f"{write_slot}{sep()}{shared}"])
    if rc != 0:
        record("genuine_failure", f"edge_same_path_read_then_write platform={platform} read_slot={read_slot} write_slot={write_slot} rc={rc} err={err!r}")
        cleanup(img, shared)
        return

    read_payload, _, _ = read_slot_payload(img, platform, read_slot)
    if not verify_payload(img, platform, write_slot, shared, f"edge_same_path_read_then_write platform={platform} write_slot={write_slot}"):
        cleanup(img, shared)
        return
    with open(shared, "rb") as f:
        shared_bytes = f.read()
    if read_payload == shared_bytes:
        record("pass", f"edge_same_path_read_then_write platform={platform} read_slot={read_slot} write_slot={write_slot}")
    else:
        record("genuine_failure", f"edge_same_path_read_then_write platform={platform} read_slot={read_slot} write_slot={write_slot}: shared file does not match read_slot's original payload")
    cleanup(img, shared)


_PLATFORM_ALIASES_DOC = {
    "mch": ["m", "monarch", "mch", "home"],
    "duo": ["p", "pelican", "mchd", "duo"],
}


def edge_doc_platform_aliases():
    """Every platform alias documented in --help's text must actually be
    accepted by the real binary -- not just the canonical duo/mch forms,
    and not just whatever platform_flag() happens to roll elsewhere.
    Guards doc/behavior drift."""
    ok = True
    for canonical, aliases in _PLATFORM_ALIASES_DOC.items():
        img = fresh(canonical)
        for alias in aliases:
            rc, out, err = verona(["-p", alias, "-d", img, "-l"])
            if rc != 0:
                record("genuine_failure", f"edge_doc_platform_aliases alias={alias!r} canonical={canonical} rc={rc} err={err!r}")
                ok = False
        cleanup(img)
    if ok:
        record("pass", "edge_doc_platform_aliases")


def edge_doc_exit_codes():
    """Every exit code documented in help.go/SPEC.md must actually be
    produced by its corresponding real scenario -- guards doc/behavior
    drift. Exit 6 (post-write verification mismatch) is intentionally
    not included: it can only occur from an internal race between
    verona-tool's own write and its own immediate readback, which can't
    be triggered from outside the process without instrumentation."""
    ok = True

    def check(label, expect, rc, err=None):
        nonlocal ok
        if rc != expect:
            record("genuine_failure", f"edge_doc_exit_codes[{label}] expected exit {expect}, got {rc} err={err!r}")
            ok = False

    platform = random.choice(["duo", "mch"])
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)

    rc, _, _ = verona(["-p", platform_flag(platform), "-d", img, "-l"])
    check("0_success", 0, rc)

    rc, _, err = verona(["-p", platform_flag(platform), "-d", img, "--bogus-flag"])
    check("1_usage_error", 1, rc, err)

    img2 = fresh(platform)
    off2 = DUO_TABLE_OFFSET if platform == "duo" else monarch_table_offset(img2)[0]
    with open(img2, "r+b") as f:
        f.seek(off2)
        f.write(b"NOTAREAL")
    rc, _, err = verona(["-p", platform_flag(platform), "-d", img2, "-l"])
    check("2_corrupted_table", 2, rc, err)
    cleanup(img2)

    src3 = None
    if candidates:
        slot3, boundary3 = random.choice(candidates)
        src3 = test_path("doc3", ".bin")
        gen_random_file(src3, boundary3 + 4096)
        rc, _, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot3}{sep()}{src3}"])
        check("3_overflow_refused", 3, rc, err)

    rc, _, err = verona(["-p", platform_flag(platform), "-d", img, "-r", str(n + 50)])
    check("4_slot_out_of_range", 4, rc, err)

    src5 = None
    if candidates:
        slot5 = candidates[0][0]
        src5 = test_path("doc5", ".bin")
        gen_random_file(src5, 16)
        rc, _, err = verona(["-p", platform_flag(platform), "-d", img,
                              "-w", f"{slot5}{sep()}{src5}", "-w", f"{slot5}{sep()}{src5}"])
        check("5_conflict", 5, rc, err)

    src7 = None
    fd = os.open(img, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if candidates:
            slot7 = candidates[0][0]
            src7 = test_path("doc7", ".bin")
            gen_random_file(src7, 16)
            rc, _, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot7}{sep()}{src7}"])
            check("7_locked", 7, rc, err)
    finally:
        os.close(fd)

    if ok:
        record("pass", f"edge_doc_exit_codes platform={platform}")
    cleanup(img, src3, src5, src7)


_TMPFS_DIR = "/mnt/verona-tinyfs"


def edge_disk_full_read_output():
    """-r's output path on a deliberately tiny, size-limited filesystem
    (too small for the slot being read) must fail cleanly with a clear
    I/O error -- no crash, no corrupt partial file left behind. Skips
    itself (expected_divergence, not a verona-tool issue) if this
    container can't mount tmpfs at all (missing CAP_SYS_ADMIN)."""
    platform = random.choice(["duo", "mch"])
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = [s for s, _ in writable_slots(img, platform, n)]
    biggest, biggest_len = None, -1
    for s in candidates:
        _, _, length = read_slot_payload(img, platform, s)
        if length > biggest_len:
            biggest, biggest_len = s, length
    if biggest is None or biggest_len < 8192:
        cleanup(img)
        return

    os.makedirs(_TMPFS_DIR, exist_ok=True)
    mounted = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=4k", "tmpfs", _TMPFS_DIR],
                              capture_output=True)
    if mounted.returncode != 0:
        record("expected_divergence",
               f"edge_disk_full_read_output: tmpfs mount unavailable in this container "
               f"({mounted.stderr.decode('utf-8', 'replace').strip()}) -- skipping, not a verona-tool issue")
        cleanup(img)
        return
    try:
        out_path = os.path.join(_TMPFS_DIR, "full.img")
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-r", f"{biggest}{sep()}{out_path}"])
        if rc == 0:
            record("genuine_failure", f"edge_disk_full_read_output platform={platform} slot={biggest}: succeeded despite insufficient space on the destination filesystem")
        elif rc < 0 or rc in (124, 134, 136, 137, 139):
            record("genuine_failure", f"edge_disk_full_read_output platform={platform} slot={biggest}: crashed/hung instead of a clean error (rc={rc})")
        else:
            partial = os.path.exists(out_path) and os.path.getsize(out_path) not in (0,)
            if partial:
                record("genuine_failure", f"edge_disk_full_read_output platform={platform} slot={biggest}: left a nonzero partial file behind (rc={rc})")
            else:
                record("pass", f"edge_disk_full_read_output platform={platform} slot={biggest} rc={rc} err={err!r}")
    finally:
        subprocess.run(["umount", _TMPFS_DIR], capture_output=True)
    cleanup(img)


def edge_exact_boundary_write(platform):
    """A write whose size is EXACTLY equal to the computed boundary (not
    one byte under, not one byte over) must succeed cleanly -- the
    classic >= vs > off-by-one at the boundary comparison. Paired here
    with scenario_refusal's existing one-byte-over coverage so both
    sides of the exact boundary are explicitly exercised."""
    img = fresh(platform)
    n = fwcount(img, platform)
    candidates = writable_slots(img, platform, n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    src = test_path("exact", ".bin")
    gen_random_file(src, boundary)
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-w", f"{slot}{sep()}{src}"])
    ok = True
    if rc != 0:
        record("genuine_failure", f"edge_exact_boundary_write platform={platform} slot={slot} boundary={boundary} rc={rc} err={err!r}")
        ok = False
    elif not verify_payload(img, platform, slot, src, f"edge_exact_boundary_write platform={platform} slot={slot}"):
        ok = False
    if ok:
        record("pass", f"edge_exact_boundary_write platform={platform} slot={slot} boundary={boundary}")
    cleanup(img, src)


def edge_cross_device_lock_independence(platform):
    """flock must be scoped to the actual device/file, not some shared
    or global lock object: holding an exclusive lock on device A must
    not block a write against a completely different device B. Unlike
    edge_concurrent_write_lock (same device, correctly blocks), this
    must proceed immediately."""
    img_a = fresh(platform)
    img_b = fresh(platform)
    n = fwcount(img_b, platform)
    candidates = writable_slots(img_b, platform, n)
    if not candidates:
        cleanup(img_a, img_b)
        return
    slot, boundary = random.choice(candidates)
    src = test_path("xdev", ".bin")
    gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))

    fd_a = os.open(img_a, os.O_RDWR)
    try:
        fcntl.flock(fd_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        t0 = time.time()
        rc, out, err = verona(["-p", platform_flag(platform), "-d", img_b, "-w", f"{slot}{sep()}{src}"])
        elapsed = time.time() - t0
        if rc == 0 and elapsed < 5:
            record("pass", f"edge_cross_device_lock_independence platform={platform} slot={slot} elapsed={elapsed:.3f}s")
        else:
            record("genuine_failure", f"edge_cross_device_lock_independence platform={platform} slot={slot} rc={rc} elapsed={elapsed:.3f}s err={err!r}")
    finally:
        os.close(fd_a)
    cleanup(img_a, img_b, src)


def edge_readonly_device(platform):
    """-d pointed at a read-only file: -w must fail cleanly (a
    permission/read-only-filesystem style error, not a crash or
    confusing message); -r and -l against the same read-only -d must
    still succeed. Plain chmod doesn't work here since this harness
    runs as root (which bypasses permission bits) -- a bind-mount
    remounted read-only is a real filesystem-level restriction even
    root can't write through."""
    img = fresh(platform)
    mount_point = os.path.join(SCRATCH, f"ro-mount-{random.randint(0, 1 << 30)}")
    os.makedirs(mount_point, exist_ok=True)
    ro_path = os.path.join(mount_point, "ro.img")
    with open(ro_path, "wb"):
        pass
    r1 = subprocess.run(["mount", "--bind", img, ro_path], capture_output=True)
    if r1.returncode != 0:
        record("expected_divergence", f"edge_readonly_device: bind mount unavailable in this container ({r1.stderr.decode('utf-8','replace').strip()}) -- skipping, not a verona-tool issue")
        cleanup(img)
        try:
            os.rmdir(mount_point)
        except OSError:
            pass
        return
    subprocess.run(["mount", "-o", "remount,ro,bind", ro_path], capture_output=True, check=True)
    try:
        n = fwcount(ro_path, platform)
        candidates = writable_slots(ro_path, platform, n)
        ok = True
        if candidates:
            slot, boundary = random.choice(candidates)
            src = test_path("rosrc", ".bin")
            gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
            rc_w, out_w, err_w = verona(["-p", platform_flag(platform), "-d", ro_path, "-w", f"{slot}{sep()}{src}"])
            if rc_w == 0:
                record("genuine_failure", f"edge_readonly_device platform={platform}: -w succeeded against a read-only device")
                ok = False
            cleanup(src)
        read_slot = random.randint(0, n - 1)
        rc_r, out_r, err_r = verona(["-p", platform_flag(platform), "-d", ro_path, "-r", str(read_slot)])
        rc_l, out_l, err_l = verona(["-p", platform_flag(platform), "-d", ro_path, "-l"])
        if rc_r != 0 or rc_l != 0:
            record("genuine_failure", f"edge_readonly_device platform={platform}: -r/-l failed against a read-only device (rc_r={rc_r} rc_l={rc_l})")
            ok = False
        if ok:
            record("pass", f"edge_readonly_device platform={platform}")
        cleanup(os.path.join(os.getcwd(), f"{read_slot}.img"))
    finally:
        subprocess.run(["umount", ro_path], capture_output=True)
        cleanup(img)
        try:
            os.remove(ro_path)
            os.rmdir(mount_point)
        except OSError:
            pass


def edge_backup_bitflip_corruption(platform):
    """A well-formed backup file (correct size, correct PartListLen/
    FwListLen alignment) with a bit flipped in the middle of its DATA
    (inside the part/fw entries, which are checksummed -- not the
    header, not the trailing zero padding out to Paddings, which
    isn't) must be refused with the same checksum-mismatch error as
    other corruption cases. Distinct from the already-covered
    truncated (edge_restore_corrupted) and oversized-Paddings
    (edge_restore_oversized_paddings) cases."""
    img = fresh(platform)
    backup = test_path("flip", ".img")
    rc, out, err = verona(["-p", platform_flag(platform), "-d", img, "-B", backup, "-r", "0"])
    if rc != 0:
        record("genuine_failure", f"edge_backup_bitflip_corruption platform={platform} backup rc={rc} err={err!r}")
        cleanup(img, backup, os.path.join(os.getcwd(), "0.img"))
        return
    with open(backup, "r+b") as f:
        data = bytearray(f.read())
        mid = len(data) // 2  # inside the checksummed part/fw-entry region
        data[mid] ^= 0xFF
        f.seek(0)
        f.write(data)
    rc2, out2, err2 = verona(["-p", platform_flag(platform), "-d", img, "-R", backup])
    if rc2 == 2:
        record("pass", f"edge_backup_bitflip_corruption platform={platform}")
    else:
        record("genuine_failure", f"edge_backup_bitflip_corruption platform={platform} rc={rc2} err={err2!r}")
    cleanup(img, backup, os.path.join(os.getcwd(), "0.img"))


def edge_cross_platform_restore_pinned():
    """KNOWN, INTENTIONAL LIMITATION -- pinned here so a future change
    doesn't silently alter this in either direction without someone
    noticing: a backup taken with -p duo and restored onto a -p mch
    target (or vice versa) currently SUCCEEDS. The VERONA table format
    is byte-identical between platforms and nothing enforces platform-
    matching (a deliberate design decision not to add a wrapper
    format around it) -- restoring a Duo table onto a Monarch device
    silently makes it look like a Duo table (and vice versa). If this
    scenario starts failing, that means platform-matching enforcement
    was added somewhere and this pin needs to be updated deliberately,
    not treated as an accidental regression."""
    img_duo = fresh("duo")
    img_mch = fresh("mch")
    backup = test_path("xplat", ".img")
    rc1, out1, err1 = verona(["-p", platform_flag("duo"), "-d", img_duo, "-B", backup, "-r", "0"])
    if rc1 != 0:
        record("genuine_failure", f"edge_cross_platform_restore_pinned: duo backup rc={rc1} err={err1!r}")
        cleanup(img_duo, img_mch, backup, os.path.join(os.getcwd(), "0.img"))
        return
    rc2, out2, err2 = verona(["-p", platform_flag("mch"), "-d", img_mch, "-R", backup])
    if rc2 == 0:
        record("pass", "edge_cross_platform_restore_pinned: duo backup onto mch target succeeds (intentional, pinned)")
    else:
        record("genuine_failure", f"edge_cross_platform_restore_pinned: duo-onto-mch restore now rc={rc2} (was 0 -- behavior changed, update the pin deliberately if this is intended) err={err2!r}")
    cleanup(img_duo, img_mch, backup, os.path.join(os.getcwd(), "0.img"))


def edge_max_gpt_partition_count():
    """A synthetic Monarch disk with the GPT partition array filled to
    its practical maximum (128 entries, the standard default array
    size) -- confirms the real on-disk GPT parser and boundary-
    computation logic still work correctly at that scale: no off-by-
    one at the array edge, correct boundary found for entries near the
    end of a fully-populated table. (The array-edge math itself is
    also covered directly, independent of any real fw-entry, by
    TestNextBoundaryBytes_128EntryArray in pkg/verona/gpt_test.go.)"""
    if not os.path.exists(GOLDEN_MCH_128PARTS):
        record("expected_divergence", "edge_max_gpt_partition_count: GOLDEN_MCH_128PARTS image not present in this container -- skipping, not a verona-tool issue")
        return
    name = f"mch128-{random.randint(0, 1 << 30)}.img"
    img = os.path.join(SCRATCH, name)
    subprocess.run(["cp", "--sparse=always", GOLDEN_MCH_128PARTS, img], check=True)
    n = fwcount(img, "mch")
    candidates = writable_slots(img, "mch", n)
    if not candidates:
        cleanup(img)
        return
    slot, boundary = random.choice(candidates)
    src = test_path("maxgpt", ".bin")
    gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
    rc, out, err = verona(["-p", platform_flag("mch"), "-d", img, "-w", f"{slot}{sep()}{src}"])
    ok = True
    if rc != 0:
        record("genuine_failure", f"edge_max_gpt_partition_count slot={slot} rc={rc} err={err!r}")
        ok = False
    elif not verify_payload(img, "mch", slot, src, f"edge_max_gpt_partition_count slot={slot}"):
        ok = False
    if ok:
        record("pass", f"edge_max_gpt_partition_count slot={slot}")
    cleanup(img, src)


EDGE_SCENARIOS_PLATFORMED = [
    edge_duplicate_write_direct, edge_duplicate_write_shortcut_collision,
    edge_dry_run_overflow, edge_dry_run_conflict, edge_restore_corrupted,
    edge_restore_oversized_paddings, edge_garbage_slots, edge_out_of_range_slot,
    edge_dry_run_alone,
    edge_concurrent_write_lock, edge_concurrent_read_shared,
    edge_corrupted_table, edge_backup_before_failed_write,
    edge_separator_consistency, edge_backup_twice_same_second,
    edge_debug_flag,
    edge_arg_fuzzing, edge_kill_mid_write_lock_release, edge_debug_and_dryrun_combined,
    edge_idempotent_rewrite, edge_list_empty_entries, edge_same_path_read_then_write,
    edge_flag_empty_or_missing_values, edge_repeated_nonrepeatable_flags,
    edge_missing_required_params, edge_bare_flag_without_shortcut,
    edge_exact_boundary_write, edge_cross_device_lock_independence,
    edge_readonly_device, edge_backup_bitflip_corruption,
]
EDGE_SCENARIOS_NOARG = [edge_help_version_combined, edge_gpt_no_fw_table, edge_fw_table_no_signature,
                         edge_doc_platform_aliases, edge_doc_exit_codes, edge_disk_full_read_output,
                         edge_mch_deleted_partition_boundary,
                         edge_cross_platform_restore_pinned, edge_max_gpt_partition_count]


def run_one_edge_iteration():
    if random.random() < 0.15:
        random.choice(EDGE_SCENARIOS_NOARG)()
    else:
        platform = random.choice(["duo", "mch"])
        random.choice(EDGE_SCENARIOS_PLATFORMED)(platform)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    # mode: "classic" (original randomized scenarios only, the default,
    # for backward compatibility with runs #1-3), "edge" (only the 16
    # deterministic edge-case scenarios), "combined" (both mixed together
    # in the same loop).
    mode = sys.argv[2] if len(sys.argv) > 2 else "classic"
    deadline = time.time() + duration
    os.chdir(SCRATCH)
    log(f"autotest starting, running for {duration} seconds, mode={mode}")
    i = 0
    while time.time() < deadline:
        i += 1
        try:
            if mode == "edge":
                run_one_edge_iteration()
            elif mode == "combined" and random.random() < 0.5:
                run_one_edge_iteration()
            elif random.random() < 0.05:
                # No-platform-arg scenario: device-letter independence for
                # Monarch (sdb/sdc/sdx), run on its own schedule.
                scenario_device_letter_independence()
            else:
                platform = random.choice(["duo", "mch"])
                scenario = random.choice(SCENARIOS_DUO if platform == "duo" else SCENARIOS_MCH)
                scenario(platform)
        except Exception as e:
            tb = traceback.format_exc()
            record("genuine_failure", f"EXCEPTION on iteration {i} (mode={mode}): {e!r}")
            log(f"EXCEPTION: {e!r}\n{tb}")
        if i % 10 == 0:
            log(f"iteration {i}: {json.dumps(counters)}")
    log(f"autotest finished after {i} iterations: {json.dumps(counters)}")
    with open("/root/work/final_counters.json", "w") as f:
        json.dump({"counters": counters, "iterations": i, "failures": failures}, f, indent=2)


if __name__ == "__main__":
    main()
