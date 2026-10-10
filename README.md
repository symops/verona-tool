# verona-tool

A unified read/write tool for the VERONA firmware table shared by the
WD My Cloud Home (Monarch / MCH, single-bay, RTD1295) and WD My Cloud
Home Duo (Pelican / MCHD, dual-bay, RTD1296). Replaces the two
separate, platform-specific vendor tools (`fwtablectl` for Monarch,
`fwtutil` for Duo) with one tool that understands both.

See `SPEC.md` for the full, line-by-line specification this
implementation follows. Run `verona-tool -h` for the CLI reference.

`-p auto` (alias `-p a`) auto-detects the platform from `-d`'s content
instead of requiring Monarch/Duo to be specified explicitly -- see
SPEC.md's "Auto-detection" section for exactly how.

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
make build-host   # for local testing against image files
```

If a future change pulls in a new shared-library dependency,
`make verify-deps` will catch it if that library isn't in the target's
set -- fix the dependency rather than accepting the build.

## Known limitation

On Duo, the "how big may a write be" boundary check is computed as the
gap to the next firmware-table entry by offset, because Duo has no GPT
(or any other independent structure) to cross-check against. If a
real unit has physical regions between two table entries that aren't
represented in the table at all, the boundary for the entry right
before such a region will be overestimated. This is a known, accepted
limitation, not a bug -- see SPEC.md.
