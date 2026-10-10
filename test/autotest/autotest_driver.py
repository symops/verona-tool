#!/usr/bin/env python3
"""Autotest driver for verona-tool, run INSIDE the isolated container.
Not part of the verona-tool binary or repo -- a standalone test harness."""
import json
import os
import random
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


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=False, timeout=60)
    return p.returncode, p.stdout, p.stderr


def verona(args):
    return run(QEMU + [VERONA] + args)


def fwtutil(args):
    return run(QEMU + [FWTUTIL] + args)


def fwtablectl(args):
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
        # verify readback after EACH round, not just the last.
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


SCENARIOS_DUO = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal, scenario_backup_restore_list]
SCENARIOS_MCH = [scenario_single_write, scenario_sequential_overwrites, scenario_refusal, scenario_backup_restore_list]


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    deadline = time.time() + duration
    os.chdir(SCRATCH)
    log(f"autotest starting, running for {duration} seconds")
    i = 0
    while time.time() < deadline:
        i += 1
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
