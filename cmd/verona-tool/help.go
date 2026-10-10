package main

const helpText = `verona-tool - unified read/write tool for the VERONA firmware table
(WD My Cloud Home / My Cloud Home Duo)

Usage:
  verona-tool -p PLATFORM [-d DEVICE] [-r SLOT[:FILE]]... [-w SLOT:FILE]... [options]
  verona-tool -p PLATFORM [-d DEVICE] -b [FILE] [-r ...] [-w ...]
  verona-tool -p PLATFORM [-d DEVICE] -l
  verona-tool -p PLATFORM [-d DEVICE] -e FILE
  verona-tool -h | --help
  verona-tool -v | --version

Platform (-p, --platform)  [required]
  m, monarch, mch, home      WD My Cloud Home (single-bay, Realtek RTD1295)
  p, pelican, mchd, duo      WD My Cloud Home Duo (dual-bay, Realtek RTD1296)
  a, auto                    auto-detect from -d's content (see below)

Device (-d, --disk)
  Target block device or raw image file holding the VERONA table.
  Required for mch. Optional for mchd (default: /dev/mmcblk0). Always
  required when -p is auto -- there is no default to fall back to
  before the platform is known.

Auto-detection (-p auto)
  Independently checks both candidate layouts on -d: a valid GPT (by
  signature, revision, and header CRC32) with a partition named
  FW_TABLE holding a valid VERONA table (Monarch), and a valid VERONA
  table at the fixed Duo offset (6,422,528 bytes), ignoring GPT
  entirely. Proceeds only if exactly one of the two validates fully;
  refuses with a clear error (exit 1) if both validate (ambiguous) or
  neither does.

Read (-r, --read SLOT[:FILE])
  Dump the current contents of table entry SLOT to FILE.
  FILE may be omitted; it then defaults to ./SLOT.img.
  Separator between SLOT and FILE may be ':' or '='.
  Repeatable. All -r entries run before any -w entries, regardless
  of their order on the command line. Reading an empty slot (length
  0) silently produces an empty file.

Write (-w, --write SLOT:FILE)
  Write FILE into table entry SLOT and recalculate its checksum.
  FILE is required. Separator may be ':' or '='.
  Refuses (no write, exits non-zero) if FILE is larger than the
  destination's known boundary. Exactly FILE's own bytes are
  written -- nothing beyond it is touched or zero-filled.
  Repeatable. The table checksum is recalculated once, after
  every -w has been applied, and the write is verified by
  reading it back.

Shortcuts for common slot pairs (active + rescue copy)
  -k, --kernel            Kernel / RescueKernel
  -t, --dtb               KernelDeviceTree / RescueDeviceTree
  -o, --rootfs            KernelRootFS / RescueRootFS
  Must be combined with an explicit bare -r or -w in the same
  invocation to select read or write mode. In read mode they take
  no value (two files, named after their own slot numbers, are
  produced automatically). In write mode they require '=FILE' (the
  same FILE is written into both slots of the pair, and only if it
  fits within both their boundaries -- otherwise neither slot is
  touched).

Backup (-b, --backup [FILE])
  Save the whole table to FILE before doing anything else in this
  invocation. FILE may be omitted; it then defaults to
  verona-table-backup-<platform>-<timestamp>.img. Always runs
  first, unconditionally. Compatible with -r/-w/-k/-t/-o; not
  compatible with -l or -e.

Restore (-e, --restore FILE)
  Validate FILE (signature, checksum, size) and, if valid, write
  it back over the table. Exclusive: not compatible with anything
  else, including -b.

List (-l, --list)
  Print the full table (header, partitions, firmware entries) and
  exit. Exclusive: not compatible with anything else.

-y, --dry-run    Print what would be read/written and which checks
                 would pass or fail, without opening the device
                 for writing.
-h, --help       Show this text and exit. Must be used alone.
-v, --version    Show version and author, then exit. Must be used
                 alone.

Examples:
  Dump the two device-tree slots of a Duo unit to their default names:
    verona-tool -p duo -r --dtb

  Flash a new kernel and its rescue copy on a Monarch unit, then
  check the result:
    verona-tool -p mch -d /dev/sda -w --kernel=/root/sata.uImage
    verona-tool -p mch -d /dev/sda -l

  Back up table entry 3 under an explicit name before overwriting it:
    verona-tool -p duo -r 3:backup-3.img -w 3:new-rootfs.bin
`
