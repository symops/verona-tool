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

QEMU = ["qemu-aarch64-static", "-L", "/aarch64-sysroot"]
VERONA = "/opt/tools/verona-tool"
FWTUTIL = "/opt/tools/fwtutil"
FWTABLECTL = "/opt/tools/fwtablectl"
GOLDEN_DUO = "/root/work/golden/duo-golden.img"
GOLDEN_MCH = "/root/work/golden/monarch-golden.img"
SCRATCH = "/root/work/scratch"
LOGFILE = "/root/work/autotest.log"
RESULTS = "/root/work/results.jsonl"
# Local-only transcript of every tool invocation run during the test, in
# order. Never committed to the repo -- this is purely a local debugging
# aid, not test output.
CMDLOG = "/root/work/commands.log"
DUO_TABLE_OFFSET = 6422528

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


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=False, timeout=60)
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


def fresh(platform):
    name = f"{platform}-{random.randint(0, 1 << 30)}.img"
    dst = os.path.join(SCRATCH, name)
    src = GOLDEN_DUO if platform == "duo" else GOLDEN_MCH
    if platform == "mch":
        subprocess.run(["cp", "--sparse=always", src, dst], check=True)
    else:
        subprocess.run(["cp", src, dst], check=True)
    return dst


def cleanup(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


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
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img_a, slot) if platform == "duo" else monarch_boundary(img_a, slot)
    if boundary < 16:
        cleanup(img_a, img_b)
        return
    size_choice = random.choice(["small", "near", "aligned512"])
    if size_choice == "small":
        size = random.randint(1, min(4096, boundary))
    elif size_choice == "near":
        size = max(1, boundary - random.randint(1, 16))
    else:
        size = (boundary // 512) * 512
        if size == 0 or size > boundary:
            size = 512
    src = os.path.join(SCRATCH, f"src-{random.randint(0,1<<30)}.bin")
    gen_random_file(src, size)

    rc, out, err = verona(["-p", platform, "-d", img_a, "-w", f"{slot}:{src}"])
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
        # Symlink /dev/mmcblk0 -> img_b for the real fwtutil.
        try:
            os.remove("/dev/mmcblk0")
        except OSError:
            pass
        os.symlink(img_b, "/dev/mmcblk0")
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
        table_file = os.path.join(SCRATCH, f"tbl-{random.randint(0,1<<30)}.img")
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
    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img_a, slot) if platform == "duo" else monarch_boundary(img_a, slot)
    if boundary < 16:
        cleanup(img_a)
        return
    rounds = random.choice([2, 3])
    srcs = []
    ok = True
    for r in range(rounds):
        size = random.randint(1, min(boundary, 1 << 20))
        src = os.path.join(SCRATCH, f"seq-{random.randint(0,1<<30)}.bin")
        gen_random_file(src, size)
        srcs.append((src, size))
        rc, out, err = verona(["-p", platform, "-d", img_a, "-w", f"{slot}:{src}"])
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
        rc2, out2, err2 = verona(["-p", platform, "-d", img_a, "-r", f"{slot}:{src}.readback"])
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
    size = boundary + random.randint(1, 1 << 16)
    src = os.path.join(SCRATCH, f"over-{random.randint(0,1<<30)}.bin")
    gen_random_file(src, size)
    before = sha256(img_a)
    rc, out, err = verona(["-p", platform, "-d", img_a, "-w", f"{slot}:{src}"])
    after = sha256(img_a)
    if rc == 3 and before == after:
        record("pass", f"refusal platform={platform} slot={slot} boundary={boundary} size={size}")
    else:
        record("genuine_failure", f"refusal platform={platform} slot={slot} boundary={boundary} size={size} rc={rc} hash_unchanged={before==after} err={err!r}")
    cleanup(img_a, src)


def scenario_backup_restore_list(platform):
    img_a = fresh(platform)
    n = fwcount(img_a, platform)
    backup_file = os.path.join(SCRATCH, f"bak-{random.randint(0,1<<30)}.img")

    off, _, _ = (monarch_table_offset(img_a) if platform == "mch" else (DUO_TABLE_OFFSET, None, None))
    h = read_header(img_a, off)
    table_before = open(img_a, "rb").read()[off:off + h["paddings"]]

    # -b (explicit filename) combined with a harmless -r 0 (list/backup
    # alone with no -r/-w would still be valid per spec, but exercising
    # -b alongside an ordinary -r is also part of what we want covered).
    rc, out, err = verona(["-p", platform, "-d", img_a, "-b", backup_file, "-r", "0"])
    if rc != 0:
        record("genuine_failure", f"backup platform={platform} rc={rc} err={err!r}")
        cleanup(img_a, backup_file)
        return
    with open(backup_file, "rb") as f:
        backup_bytes = f.read()
    if backup_bytes != table_before:
        record("genuine_failure", f"backup platform={platform}: backup file does not match pre-backup table bytes")
        cleanup(img_a, backup_file, os.path.join(os.getcwd(), "0.img"))
        return

    slot = random.randint(0, n - 1)
    boundary = duo_boundary(img_a, slot) if platform == "duo" else monarch_boundary(img_a, slot)
    if boundary >= 16:
        src = os.path.join(SCRATCH, f"mut-{random.randint(0,1<<30)}.bin")
        gen_random_file(src, random.randint(1, min(boundary, 1 << 16)))
        rc, out, err = verona(["-p", platform, "-d", img_a, "-w", f"{slot}:{src}"])
        cleanup(src)
        if rc != 0:
            record("genuine_failure", f"backup_restore mutate platform={platform} rc={rc} err={err!r}")
            cleanup(img_a, backup_file)
            return

    rc, out, err = verona(["-p", platform, "-d", img_a, "-e", backup_file])
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
        os.remove(os.path.join(os.getcwd(), "0.img"))
    except OSError:
        pass


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

    rc, out, err = verona(["-p", platform, "-d", img, "-l"])
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
    flag = random.choice([f"--{name}", {"kernel": "-k", "dtb": "-t", "rootfs": "-o"}[name]])
    slots = SHORTCUT_SLOTS[name]

    rc, out, err = verona(["-p", platform, "-d", img, "-r", flag])
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
    flag_short = {"kernel": "-k", "dtb": "-t", "rootfs": "-o"}[name]
    slots = SHORTCUT_SLOTS[name]

    mode = random.choice(["normal", "oversized_half", "duplicate_conflict"])

    if mode == "duplicate_conflict":
        src = os.path.join(SCRATCH, f"dupe-{random.randint(0,1<<30)}.bin")
        gen_random_file(src, 64)
        before = sha256(img)
        # Write-mode shortcuts only accept the '=FILE' attachment on the
        # long option form (per SPEC.md); the short forms (-k/-t/-o) carry
        # no value in write mode. Using the long form twice is still a
        # valid way to exercise the duplicate-shortcut conflict check.
        rc, out, err = verona(["-p", platform, "-d", img, "-w",
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
    if min_b < 16:
        cleanup(img)
        return

    if mode == "oversized_half":
        size = min_b + random.randint(1, 1 << 16)
    else:
        size = random.randint(1, min(min_b, 1 << 16))
    src = os.path.join(SCRATCH, f"sw-{random.randint(0,1<<30)}.bin")
    gen_random_file(src, size)
    before = sha256(img)

    # Write-mode shortcuts only accept '=FILE' on the long option form.
    flag = flag_long
    rc, out, err = verona(["-p", platform, "-d", img, "-w", f"{flag}={src}"])
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


def scenario_auto_detect(platform):
    """-p auto must detect the right platform from -d's content alone."""
    img = fresh(platform)
    rc, out, err = verona(["-p", "auto", "-d", img, "-l"])
    text = out.decode("utf-8", "replace")
    want = "monarch" if platform == "mch" else "duo"
    if rc == 0 and f"auto-detected platform: {want}" in text:
        record("pass", f"auto_detect platform={platform}")
    else:
        record("genuine_failure", f"auto_detect platform={platform} rc={rc} out={text[:200]!r} err={err!r}")
    cleanup(img)


# Device-letter independence (Monarch only -- Duo has no device-letter
# concept, it's always a flat eMMC path): confirm the GPT/FW_TABLE lookup
# works through several different symlinked device-letter paths pointing at
# the SAME synthetic disk content, catching any accidental "sda"-style
# hardcoding regression.
_DEVICE_LETTERS = ["/dev/sdb", "/dev/sdc", "/dev/sdx"]


def scenario_device_letter_independence():
    img = fresh("mch")
    letter = random.choice(_DEVICE_LETTERS)
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
        try:
            os.remove(letter)
        except OSError:
            pass
    cleanup(img)


SCENARIOS_DUO = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal,
                 scenario_backup_restore_list, scenario_list_check, scenario_shortcut_read,
                 scenario_shortcut_write, scenario_auto_detect]
SCENARIOS_MCH = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal,
                 scenario_backup_restore_list, scenario_list_check, scenario_shortcut_read,
                 scenario_shortcut_write, scenario_auto_detect]


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    deadline = time.time() + duration
    os.chdir(SCRATCH)
    log(f"autotest starting, running for {duration} seconds")
    i = 0
    while time.time() < deadline:
        i += 1
        if random.random() < 0.05:
            # No-platform-arg scenario: device-letter independence for
            # Monarch (sdb/sdc/sdx), run on its own schedule.
            try:
                scenario_device_letter_independence()
            except Exception as e:
                record("genuine_failure", f"EXCEPTION in scenario_device_letter_independence: {e!r}")
                log(f"EXCEPTION: {e!r}")
            if i % 10 == 0:
                log(f"iteration {i}: {json.dumps(counters)}")
            continue
        platform = random.choice(["duo", "mch"])
        scenario = random.choice(SCENARIOS_DUO if platform == "duo" else SCENARIOS_MCH)
        try:
            scenario(platform)
        except Exception as e:
            record("genuine_failure", f"EXCEPTION in {scenario.__name__} platform={platform}: {e!r}")
            log(f"EXCEPTION: {e!r}")
        if i % 10 == 0:
            log(f"iteration {i}: {json.dumps(counters)}")
    log(f"autotest finished after {i} iterations: {json.dumps(counters)}")
    with open("/root/work/final_counters.json", "w") as f:
        json.dump({"counters": counters, "iterations": i, "failures": failures}, f, indent=2)


if __name__ == "__main__":
    main()
