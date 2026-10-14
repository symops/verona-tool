# verona-tool

A unified read/write tool for the VERONA firmware table shared by the
WD My Cloud Home (Monarch / MCH, single-bay, RTD1295) and WD My Cloud
Home Duo (Pelican / MCHD, dual-bay, RTD1296). Replaces the two
separate, platform-specific vendor tools (`fwtablectl` for Monarch,
`fwtutil` for Duo) with one tool that understands both: list the table,
read and write slots, back the table up and restore it, with the
checksum recalculated and every write read back and verified.

See `SPEC.md` for the full, line-by-line specification this
implementation follows. Run `verona-tool -h` for the CLI reference.

## Download

Ready-made binaries are attached to the
[releases](https://github.com/symops/verona-tool/releases): the file
`verona-tool` is a stripped **linux/arm64** executable, dynamically
linked against glibc only (`libc.so.6`). Copy it to the unit
(rescue/recovery environment or a full Debian) and `chmod +x` it. The
SHA-256 of each binary is in its release notes.

## Quick start

```sh
verona-tool -p mch -d /dev/sda -l                 # Monarch: list the table (-d required)
verona-tool -p duo -l                             # Duo: -d defaults to /dev/mmcblk0

verona-tool -p duo -r --dtb                       # dump both device-tree slots to ./6.img, ./7.img
verona-tool -p duo -r 3:backup-3.img -w 3:new.bin # back one slot up, then overwrite it

# Flash a kernel + its rescue copy, keeping a backup of the table
verona-tool -p mch -d /dev/sda -B table-backup.img -w -k=sata.uImage
verona-tool -p mch -d /dev/sda -l                 # check the result
```

- `-p` selects the platform (`mch`/`monarch`/`m`/`home`, or `duo`/`mchd`/`pelican`/`p`).
- Slots are addressed by index (`-l` shows them). The same 13 entries
  exist on both platforms: `0 GoldKernel`, `1 GoldRescueDeviceTree`,
  `2 GoldRescueRootfs`, `3 GoldAudio`, `4 uBoot`, `5 Kernel`,
  `6 RescueDeviceTree`, `7 KernelDeviceTree`, `8 RescueRootFS`,
  `9 KernelRootFS`, `10 Audio`, `11 RescueAudio`, `12 RescueKernel`.
- `-k` / `-t` / `-f` are shortcuts for the kernel / device-tree /
  rootfs *pair* (active + rescue copy). To write, attach the file:
  `--kernel=FILE`, `-k=FILE` or `-k:FILE`; the same file goes into
  both slots, and only if it fits both.
- `-n` prints what would be done (and which checks would pass) without
  writing; `-B [FILE]` saves the table first; `-R FILE` restores it.
- Exit codes tell failures apart (`3` file too large for the slot,
  `6` post-write verification mismatch, `7` device locked by another
  process, ...); the full table is in `SPEC.md`.

**Writing to a real device is irreversible and not interactively
confirmed.** Back up first, try the exact command with `-n` and against
a copy of the image, and double-check `-d` before writing to hardware.

## Command-line reference

```
verona-tool -p PLATFORM [-d DEVICE] [-r SLOT[:FILE]]... [-w SLOT:FILE]... [options]
verona-tool -p PLATFORM [-d DEVICE] -B [FILE] [-r ...] [-w ...]
verona-tool -p PLATFORM [-d DEVICE] -l
verona-tool -p PLATFORM [-d DEVICE] -R FILE
verona-tool -h | --help
verona-tool -v | --version
```

At least one of `-r`, `-w`, `-B`, `-l`, `-R` is required (unless `-h` or
`-v` is given). A value may be attached to its option with `=` for the
long options (`--device=/dev/sda`); a slot/file pair accepts `:` or `=`
between the two (`3:file.img` or `3=file.img`).

### Selecting the unit

| Option | Meaning |
|---|---|
| `-p`, `--platform VALUE` | **Required.** `m`, `monarch`, `mch`, `home` = WD My Cloud Home (single-bay, RTD1295); `p`, `pelican`, `mchd`, `duo` = My Cloud Home Duo (dual-bay, RTD1296). |
| `-d`, `--device PATH` | The block device **or a plain image file** that holds the VERONA table. **Required for Monarch** (pass the whole disk, e.g. `/dev/sda`: the table's partition is found through the GPT); optional for Duo (default `/dev/mmcblk0`, the table sits at a fixed offset). |

### Reading and writing slots

| Option | Meaning |
|---|---|
| `-r`, `--read SLOT[:FILE]` | Dump the current contents of table entry `SLOT` to `FILE`. `FILE` may be omitted and defaults to `./SLOT.img`. Repeatable. Reading an empty slot (length 0) silently produces an empty file. |
| `-w`, `--write SLOT:FILE` | Write `FILE` into table entry `SLOT` and recalculate its checksum. `FILE` is required. Exactly the file's own bytes are written, nothing beyond is touched or zero-filled. Refused (nothing written, exit 3) if `FILE` is larger than the slot's boundary. Repeatable. Only the entry's length, padding and checksum change in the table; its type, version and load address are never touched. |
| `-k`, `--kernel` | Shortcut for the kernel pair: slots `5` (Kernel) and `12` (RescueKernel). |
| `-t`, `--dtb` | Shortcut for the device-tree pair: slots `7` (KernelDeviceTree) and `6` (RescueDeviceTree). |
| `-f`, `--rootfs` | Shortcut for the rootfs pair: slots `9` (KernelRootFS) and `8` (RescueRootFS). |

Each shortcut must be **immediately preceded by its own bare `-r` or
`-w`**, which selects the mode (otherwise it is a usage error:
`-t/--dtb must be immediately preceded by a bare -r or -w`):

- **Read mode** (`-r -k`): no value; both slots are dumped to files named
  after their own slot numbers (`5.img`, `12.img`).
- **Write mode** (`-w -k=FILE`): the file is attached to the option as
  `--kernel=FILE`, `-k=FILE` or `-k:FILE` (same for `-t`/`--dtb` and
  `-f`/`--rootfs`). A space-separated value is not accepted. The same
  file is written into both slots, and only if it fits **both**
  boundaries; otherwise neither slot is touched. All other short options
  reject an attached `-x=VALUE`.

```sh
verona-tool -p duo -r -k -r -t                                # dump 5, 12, 7, 6
verona-tool -p duo -w -k=emmc.uImage -w -t:rescue.emmc.dtb    # flash two pairs
```

### Whole-table operations

| Option | Meaning |
|---|---|
| `-B`, `--backup [FILE]` | Save the whole table region to `FILE` before anything else in this run (default `verona-table-backup-<platform>-<YYYYMMDDHHMMSS>.img`). It always runs first, even if a later `-r`/`-w` of the same command fails. Combines with `-r`, `-w`, `-k`, `-t`, `-f`; not with `-l` or `-R`. |
| `-R`, `--restore FILE` | Validate `FILE` (signature, checksum, size, alignment, and that it fits the space available on `-d`) and, only if valid, write it back over the table. **Exclusive:** cannot be combined with anything else, `-B` included. |
| `-l`, `--list` | Print the full table (header, partitions, firmware entries) and exit. **Exclusive.** |

### Modifiers and information

| Option | Meaning |
|---|---|
| `-n`, `--dry-run` | Print what would be read or written and which checks would pass or fail, without writing anything (and without creating backup or output files). Combines with everything; on its own it is a usage error. Exits 0 whenever the simulation itself completed, even if it reports that the real run would fail. |
| `-D`, `--debug` | Print extra diagnostics (where the table was found, computed boundaries, lock state, byte offsets, checksums) next to the normal output. Changes nothing about what is done. |
| `-h`, `--help` | Show the built-in help and exit. Must be used alone. |
| `-v`, `--version` | Show the version and the author and exit. Must be used alone. |

### How a run is processed

- **Order** is fixed and independent of the order on the command line:
  `-B` first, then every `-r`, then every `-w`. The table checksum is
  recalculated **once**, after all writes. If there is no `-w`, the
  table is never rewritten.
- **Conflicts** (exit 5): the same slot written twice (also through the
  shortcuts), the same shortcut given twice in write mode, an explicit
  `-w N` whose slot `N` also belongs to a shortcut written in the same
  command. The same output path used by several `-r`/`-B` is only a
  warning.
- **Verification:** after every data write and every table write
  (`-R` included) the written range is read back and compared with the
  source; a mismatch is exit 6 and is not retried. Every write is
  `fsync`ed.
- **Locking:** the device is `flock`ed non-blockingly for the whole run,
  shared for read-only commands and exclusive when anything is written. A
  device that is already locked fails at once with exit 7.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | argument or usage error |
| 2 | bad signature, corrupt table, or invalid backup file |
| 3 | file too large for the destination slot: refused |
| 4 | slot index out of range |
| 5 | argument conflict |
| 6 | verification after writing did not match |
| 7 | device locked by another process |

## Build

The target runtime -- the rescue/recovery environment on both Monarch
and Duo units -- ships only a minimal util-linux-based userspace
library set (`ld-linux-aarch64.so.1`, `libblkid.so.1`, `libc.so.6`,
`libcap.so.2`, `libfdisk.so.1`, `libm.so.6`, `libmount.so.1`,
`libreadline.so.8`, `libresolv.so.2`, `libsmartcols.so.1`,
`libtinfow.so.6`, `libudev.so.1`, `libuuid.so.1`). verona-tool is a
normal **dynamically-linked glibc binary**, cross-compiled with the
aarch64 C toolchain -- in practice it only needs `libc.so.6` (plus
`ld-linux-aarch64.so.1` as the ELF interpreter), both already in that
set. GPT headers and partition entries are decoded as plain bytes in
pure Go (`pkg/verona/gpt.go`); nothing here links against
libfdisk/libblkid/libmount/libuuid or any other util-linux library,
even though they happen to be present on the target.

```sh
make build        # CGO_ENABLED=1 CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64, -> verona-tool-arm64
make verify-deps  # fails the build if it needs a library outside the target's set
make strip        # verify-deps, then strip: the binary that is released
make build-host   # for local testing against image files
make test         # Go unit tests
```

If a future change pulls in a new shared-library dependency,
`make verify-deps` will catch it if that library isn't in the target's
set -- fix the dependency rather than accepting the build.

From a clean Debian 13 (toolchain, dependency check, release procedure,
and how to flash a kernel build with this tool) see `BUILDING.md`.

## Testing

Unit tests (`make test`) cover the command-line rules and the GPT
boundary math. `test/autotest/autotest_driver.py` is a separate
harness, not part of the build, that drives the cross-compiled binary
under `qemu-aarch64-static` against full-size golden Monarch and Duo
images, cross-checking it against the real vendor tools; it is described
in `BUILDING.md`.

## Known limitation

On Duo, the "how big may a write be" boundary check is computed as the
gap to the next firmware-table entry by offset, because Duo has no GPT
(or any other independent structure) to cross-check against. If a
real unit has physical regions between two table entries that aren't
represented in the table at all, the boundary for the entry right
before such a region will be overestimated. This is a known, accepted
limitation, not a bug -- see SPEC.md.

## Author

Symo <symops@gmail.com>
