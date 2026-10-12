# Building verona-tool

This describes building `verona-tool` from scratch on a fresh Debian 13 ("trixie") machine and deploying it against a real WD My Cloud Home (MCH) or My Cloud Home Duo (MCHD) unit. See `README.md` for what the tool does and `SPEC.md` for the full CLI/behavior specification.

## 1. Install the toolchain and build dependencies

```sh
sudo apt update
sudo apt install -y \
    git golang-go \
    gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu
```

`gcc-aarch64-linux-gnu` is required even though this is a Go project with no C source of its own: `make build` cross-compiles with `CGO_ENABLED=1` specifically to force a **dynamically-linked** binary (see step 3) — `cgo` needs a working C cross-compiler on the host to do that, even with zero actual cgo code in the repo.

Go 1.25 or newer is required (see `go.mod`); Debian 13's `golang-go` package satisfies this. `binutils-aarch64-linux-gnu` provides `aarch64-linux-gnu-strip` and `aarch64-linux-gnu-readelf`, used by `make strip`/`make verify-deps`.

## 2. Clone the repository

```sh
git clone git@github.com:symops/verona-tool.git
cd verona-tool
```

## 3. Build the dynamically-linked ARM64 binary

```sh
make build
```

This runs:

```sh
CGO_ENABLED=1 CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64 \
    go build -ldflags '-linkmode=external' -o verona-tool-arm64 ./cmd/verona-tool
```

`-linkmode=external` is explicit and required — `CGO_ENABLED=1` by itself is **not** sufficient to force dynamic linking when the package contains no actual cgo code (Go's linker still defaults to fully static in that case). The target runtime (the rescue/recovery environment on both Monarch and Duo units) ships a normal glibc userspace and has no particular need for a static binary; dynamic linking was chosen deliberately, confirmed against the real device's available shared libraries (see `make verify-deps` below), not defaulted into by accident.

In practice the resulting binary needs only `libc.so.6` (plus `ld-linux-aarch64.so.1` as the ELF interpreter) — both are present in the target's library set. GPT and partition-table parsing is pure-Go raw struct decoding (`pkg/verona/gpt.go`); nothing here links against `libfdisk`/`libblkid`/`libmount`/`libuuid` or any other util-linux library, even though those happen to also be present on the target.

## 4. Verify the dependency set hasn't drifted

```sh
make verify-deps
```

This re-runs the build, then checks `verona-tool-arm64`'s actual `NEEDED` entries (via `readelf -d`) against the exact set of shared libraries the target runtime is known to ship (hardcoded in the Makefile's `ALLOWED_LIBS`). If a future code change pulls in a new shared-library dependency not in that set, this target fails the build — fix the dependency (or update `ALLOWED_LIBS` if the target's own library set has genuinely changed), don't ignore the failure.

## 5. Strip and finalize

```sh
make strip
```

Runs `verify-deps` first, then `aarch64-linux-gnu-strip`. The repo's own CI/release process should produce the binary this way, not via a bare `go build` — skipping `verify-deps` risks shipping a binary that silently needs a library the real rescue environment doesn't have.

## 6. Build for the host (for local testing against image files)

```sh
make build-host
```

Produces a plain `CGO_ENABLED=0` binary for your own machine's architecture, named `verona-tool` (no `-arm64` suffix). This is what you want for iterating against a raw disk image or a copy of a real table dump on your own machine — there's no reason to cross-compile and run under emulation just to test against a file.

```sh
go test ./...
```

runs the Go unit tests (`pkg/verona/*_test.go`) — these cover the handful of edge cases (e.g. GPT last-partition boundary math) that are impractical to exercise through the full CLI.

## 7. Running against a real device vs. a test image

```sh
# Against a real device (needs root; -d takes a block device OR a plain file):
sudo ./verona-tool-arm64 -p mch -d /dev/sda -l
sudo ./verona-tool-arm64 -p duo -l                 # -d defaults to /dev/mmcblk0 on duo

# Against a raw image file copied off a device, or a synthetic test image:
./verona-tool -p mch -d ./mch-dump.img -l
```

`-d` is interchangeable between a real block device and a plain file — `verona-tool` just opens whatever path it's given and never assumes a device node. This is what makes the autotest harness (step 8) able to test the exact same binary against disk images with no special-casing. As with the kernel-port repos' own flashing tools, **writing (`-w`) to a real device is irreversible and not interactively confirmed** — always prove a write against a copy of the real image first (`cp` the device to a file, test against the file), and double-check `-d`'s target before running anything with `-w`/`-R`/`-B` against real hardware.

`-p auto` (alias `-p a`) can detect the platform from `-d`'s content instead of specifying `mch`/`duo` explicitly — see `SPEC.md`'s "Auto-detection" section for exactly how, and its two-way-ambiguity failure mode.

## 8. The autotest harness

`test/autotest/autotest_driver.py` is a standalone Python harness, **not part of the Go build** (it's never imported by or compiled into `cmd/verona-tool`) and not something `make` runs. It drives the real cross-compiled `verona-tool-arm64` under `qemu-aarch64-static`, alongside the real vendor tools (`fwtablectl`, `fwtutil`) for cross-validation, against full-size golden disk images, inside an isolated container — see the comments at the top of the file for the exact directory layout (`/opt/tools/`, `/root/work/golden/`, etc.) it expects when run. It is a correctness/regression tool for this repo's own development, not something an end user deploying `verona-tool` needs to run.
