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
