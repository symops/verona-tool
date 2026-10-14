# Building verona-tool from a clean Debian 13

This describes building **verona-tool** (the unified VERONA firmware-table tool for the WD My Cloud Home "Monarch" / MCH and the My Cloud Home Duo "Pelican" / MCHD) from scratch on a fresh Debian 13 ("trixie") machine, running its tests, and using it on a unit. It is the companion of the kernel build guides in [`symops/monarch-6.18`](https://github.com/symops/monarch-6.18) and [`symops/pelican-6.18`](https://github.com/symops/pelican-6.18) (their section "Flashing for a normal boot" is what this tool replaces). See `README.md` for what the tool does and `SPEC.md` for the full behaviour specification.

If you only want to run it, you do not need any of this: download the ready-made `verona-tool` from the [latest release](https://github.com/symops/verona-tool/releases/latest).

## 1. Install the toolchain and build dependencies

```sh
sudo apt update
sudo apt install -y \
    git make golang-go \
    gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu
```

Go 1.25 or newer is required (`go.mod`); Debian 13's `golang-go` satisfies it. Check with `go version`.

`gcc-aarch64-linux-gnu` is needed even though this is a Go project with no C source of its own: `make build` cross-compiles with `CGO_ENABLED=1` specifically to get a **dynamically linked** binary (see step 3), and `cgo` needs a working C cross-compiler on the host for that. `binutils-aarch64-linux-gnu` provides `aarch64-linux-gnu-strip` and `aarch64-linux-gnu-readelf`, used by `make strip` and `make verify-deps`.

Nothing else is needed: no `libfdisk`/`libblkid`/`libmount`/`libuuid` headers, no kernel tree. GPT and VERONA-table parsing is plain Go (`pkg/verona/gpt.go`).

## 2. Clone the repository

```sh
git clone git@github.com:symops/verona-tool.git
cd verona-tool
```

There is no `.config` or any other generated input: the repository is the whole source.

## 3. Build the ARM64 binary

```sh
make build
```

This runs:

```sh
CGO_ENABLED=1 CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64 \
    go build -ldflags '-linkmode=external' -o verona-tool-arm64 ./cmd/verona-tool
```

**`-linkmode=external` is explicit and required.** `CGO_ENABLED=1` alone does not force dynamic linking when the package contains no real cgo code: Go's linker still defaults to a fully static binary in that case. The target (the rescue/recovery environment of both Monarch and Duo units) ships a normal glibc userspace, so a dynamic binary is what is wanted, and `make verify-deps` (next step) proves it needs nothing the target lacks.

The output is `verona-tool-arm64` in the repository root.

## 4. Verify the dependency set

```sh
make verify-deps
```

Rebuilds, then checks the binary's `NEEDED` entries (`readelf -d`) against the exact library set the target runtime ships (`ALLOWED_LIBS` in the `Makefile`). In practice the binary needs only `libc.so.6`, plus `ld-linux-aarch64.so.1` as its ELF interpreter. If a code change ever pulls in another shared library, this target fails: fix the dependency, or update `ALLOWED_LIBS` only if the target's own library set has genuinely changed. Do not ignore the failure.

## 5. Strip

```sh
make strip
```

Runs `verify-deps` first, then `aarch64-linux-gnu-strip`. **Always produce the binary you ship this way**, not with a bare `go build`: skipping `verify-deps` risks shipping a binary that silently needs a library the real rescue environment does not have.

## 6. Host build and unit tests

```sh
make build-host
```

Produces a plain `CGO_ENABLED=0` binary for your own machine, named `verona-tool` (no `-arm64` suffix). Use it to iterate against raw disk images or a copy of a real table, with no need to cross-compile and run under emulation:

```sh
./verona-tool -p mch -d ./mch-dump.img -l
```

`-d` takes a block device or a plain file interchangeably; the tool opens whatever path it is given and never assumes a device node.

```sh
make test        # go test ./...
```

runs the Go unit tests (`cmd/verona-tool/args_test.go` for the command-line rules, `pkg/verona/gpt_test.go` for GPT boundary math such as the last-partition case), which cover edge cases that are impractical to reach through the full CLI.

**Note on file names:** `make build-host` produces `./verona-tool` (a host-architecture binary) and `make build` produces `./verona-tool-arm64`. They never overwrite each other, but when you copy the ARM64 one to a unit or attach it to a release it is named plain `verona-tool` (step 8).

## 7. Run it

```sh
# Against a real device (needs root; -d takes a block device OR a plain file):
sudo ./verona-tool -p mch -d /dev/sda -l             # Monarch: -d is required
sudo ./verona-tool -p duo -l                          # Duo: -d defaults to /dev/mmcblk0

# Dry run: print what would be done and which checks would pass, change nothing
./verona-tool -p duo -d ./duo-dump.img -n -w -k=emmc.uImage
```

Useful facts when working with it:

- **Slots.** Both platforms use the same 13-entry table, addressed by index: `0 GoldKernel, 1 GoldRescueDeviceTree, 2 GoldRescueRootfs, 3 GoldAudio, 4 uBoot, 5 Kernel, 6 RescueDeviceTree, 7 KernelDeviceTree, 8 RescueRootFS, 9 KernelRootFS, 10 Audio, 11 RescueAudio, 12 RescueKernel`. Always read the count from the table, never assume 13 (`-l` shows it).
- **Shortcuts.** `-k` / `-t` / `-f` select the kernel / device-tree / rootfs *pair* (active + rescue copy). In write mode they take an attached file as `--kernel=FILE`, `-k=FILE` or `-k:FILE`, and the same file goes to both slots, only if it fits both.
- **Order and checksum.** `-B` (backup) runs first, then every `-r`, then every `-w`; the table checksum is recalculated once, after all writes, and each write is read back and compared.
- **Locking.** The device is `flock`ed for the whole run (shared for reads, exclusive for writes); a second instance fails at once with exit code 7.
- **Exit codes.** `0` success, `1` usage, `2` bad signature/table/backup, `3` file too large for the slot (refused), `4` slot out of range, `5` argument conflict, `6` verification mismatch, `7` device locked. Full table in `SPEC.md`.

**Writing (`-w`, `-R`) to a real device is irreversible and not interactively confirmed.** There is no prompt. Take a backup first (`-B FILE`, or `-r SLOT:backup.img`), prove the exact command against a copy of the image (`cp` the device to a file and use `-d` on the file, plus `-n`), and double-check `-d` before running anything that writes against real hardware.

### Flashing a kernel build (what the kernel guides' vendor tools did)

With the images produced by the kernel guides (`emmc.uImage`, `rescue.emmc.dtb`, `rescue.root.emmc.cpio.gz_pad.img` for Duo; `sata.uImage`, `rescue.sata.dtb`, `rescue.root.sata.cpio.gz_pad.img` for Monarch), one invocation writes both copies of each slot and updates the table:

```sh
# Duo (replaces fwtutil -r 5:.. -r 12:.. -r 8:.. -r 9:.. -r 6:.. -r 7:..)
verona-tool -p duo -B duo-table-backup.img \
    -w -k=/root/emmc.uImage -w -t=/root/rescue.emmc.dtb \
    -w -f=/root/rescue.root.emmc.cpio.gz_pad.img

# Monarch (replaces the dd + fwtablectl firmware update sequence)
verona-tool -p mch -d /dev/sdb -B mch-table-backup.img \
    -w -k=/root/sata.uImage -w -t=/root/rescue.sata.dtb \
    -w -f=/root/rescue.root.sata.cpio.gz_pad.img
verona-tool -p mch -d /dev/sdb -l       # check the result
```

On Monarch `-d` is the **whole disk**: the tool reads the GPT, finds the `FW_TABLE` partition itself and never hardcodes its position. Prove a build boots through the kernel guides' USB rescue path before flashing it this way.

## 8. Make a release

The release asset is the stripped ARM64 binary under the name plain **`verona-tool`**, and the release itself is named just after the tag (`v1.0`), not "verona-tool v1.0".

```sh
git switch main && git pull --ff-only
make strip                                   # build + verify-deps + strip
mkdir -p /tmp/rel && cp verona-tool-arm64 /tmp/rel/verona-tool
sha256sum /tmp/rel/verona-tool
gh release create vX.Y /tmp/rel/verona-tool --target main --title "vX.Y" --notes-file notes.md
```

Put the SHA-256 of the binary in the release notes. The version printed by `verona-tool -v` is the `version` constant in `cmd/verona-tool/args.go`: bump it before tagging so the binary and the tag agree.

## 9. The autotest harness

`test/autotest/autotest_driver.py` is a standalone Python harness. It is **not part of the Go build** (never imported by or compiled into `cmd/verona-tool`) and `make` does not run it. It drives the real cross-compiled `verona-tool-arm64` under `qemu-aarch64-static`, next to the real vendor tools (`fwtablectl`, `fwtutil`) for cross-validation, against full-size golden disk images (one Monarch, one Monarch with a 128-entry GPT, one Duo), inside an isolated container. See the comments at the top of the file for the directory layout it expects (`/opt/tools/`, `/root/work/golden/`, `/root/work/scratch/`, ...).

It runs random operations and fixed edge cases (kill-mid-write, concurrent locks, cross-device independence, all spellings of the platform name and of the shortcut flags) for a given number of seconds:

```sh
python3 autotest_driver.py 1200 combined     # 20 minutes, mixed scenarios
```

Every iteration works on a fresh copy of a golden image in the scratch directory. The driver cleans that directory when it starts (unless another driver run is active); every 25 iterations, or when less than 12 GiB are free, it deletes entries older than 30 minutes; and it stops cleanly (rather than reporting `ENOSPC` as failures) if less than 3 GiB remain. The golden images live outside the scratch directory and are never touched. **Give the container a filesystem with tens of GiB free**: each scratch image is several GiB.

It is a correctness/regression tool for this repository's own development, not something an end user deploying `verona-tool` needs to run.
