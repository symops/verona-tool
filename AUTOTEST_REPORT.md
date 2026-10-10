# verona-tool autotest report

**Note: this was a shortened 10-minute first pass**, per an explicit
instruction to scale the originally-planned 1-hour run down for this
first review. A longer run can follow once these results are
reviewed.

## Build verification

- `make build`: `CGO_ENABLED=1 CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64 go build -ldflags '-linkmode=external'`
- `readelf -d`: `NEEDED libc.so.6` (only dependency)
- `readelf -l`: `INTERP /lib/ld-linux-aarch64.so.1`
- Confirmed a normal dynamically-linked glibc binary, as required --
  not static, and needs nothing outside the target rescue
  environment's own library set.
- `aarch64-linux-gnu-strip` applied: **1,817,760 bytes** (≈1.73 MiB)
  stripped, still dynamically linked (`file` confirms `dynamically
  linked, interpreter /lib/ld-linux-aarch64.so.1, ... stripped`).

## Methodology

- Isolated `debian:trixie-slim` Docker container (`verona-autotest`),
  with `qemu-user-static` + a copied-in aarch64 sysroot
  (`ld-linux-aarch64.so.1`, `libc.so.6`) used to run the real
  `fwtutil` binary, a freshly cross-built `fwtablectl`
  (`github.com/sgissi/wdmch-tools`), and verona-tool's own arm64
  build, all via `qemu-aarch64-static -L <sysroot>`.
- **Duo reference data**: a copy of the real `mchd-1.img` eMMC dump,
  truncated to 110 MiB (table + backup copy + every real firmware
  entry's data all fit well inside that, verified against the header
  before truncating) so that per-scenario fresh copies are fast.
- **Monarch reference data**: since the only real dump available
  (`mch-1.img`) is just the `FW_TABLE` partition by itself, a
  synthetic full-disk GPT image was built with `sgdisk`, using the
  *exact* partition start/size/name from the real production
  `sfdisk`/`--part-label` script for partitions 1-18 (the ones
  verona-tool's boundary logic and our firmware slots actually touch)
  -- partitions 19-24 (the large filesystem/swap/data volumes) were
  shrunk to a token 1 MiB each, since only their position mattered,
  not their real multi-GB capacity. The real `mch-1.img` table dump
  was then written into partition 1's region. Both reference images
  are read-only "golden" copies; every scenario works on a fresh
  `cp`/`cp --sparse=always` copy.
- The test harness (`test/autotest/autotest_driver.py`) lives in its
  own directory, is not imported by or built into `cmd/verona-tool`,
  and is a plain script run directly with `python3` inside the
  container.
- Each scenario: pick a platform at random, pick one of 4 scenario
  kinds at random (single write+compare-vs-original, 2-3 sequential
  overwrites of one slot with read-back verification after each,
  deliberate-oversized-file refusal check, backup+mutate+restore
  round-trip), run it on a fresh copy, record pass/expected-divergence
  /genuine-failure.

## Results

| | Count |
|---|---|
| Total scenarios | 232 |
| Pass | 227 |
| Expected divergence (categorized) | 5 |
| Genuine verona-tool failures | **0** |

All 5 "expected divergence" cases are the already-catalogued
`fwtablectl` (the Go original tool for Monarch) padding bug: it always
adds a spare 512-byte sector even when a firmware entry's length is
already an exact multiple of 512, where verona-tool correctly leaves
it unchanged. Each occurred on a `single_write` scenario that happened
to pick a file size that is a multiple of 512 (33,554,432 bytes, i.e.
exactly the Kernel/RootFS-family slot boundary) for slots 8, 9 and 12.

**These were initially miscounted as "genuine failures" by a bug in
the test harness itself** (not in verona-tool): `classify_monarch_diffs`
assumed every byte past offset 32 belonged to the 48-byte `PartEntry`
list, without accounting for Monarch's `PartListLen` (192 bytes here)
before switching to the 32-byte `FwEntry` stride -- so a one-byte diff
inside an `FwEntry.Paddings` field (bytes 18-21) was checked against
the wrong field layout and flagged as unexplained. Manually tracing
each flagged byte offset (499, 531, 627 -- each exactly
`224 + slot*32 + 19`, the second byte of that slot's `Paddings` field)
confirmed every one is this single known divergence, not a new issue.
The harness's classifier has been fixed (committed alongside this
report) so future runs report it correctly as
`divergence_padding_always_plus_512` instead of `genuine_failure`.

### Coverage confirmed in this run

- **Duo vs real `fwtutil`**: single writes compared via whole-image
  SHA-256 (no known divergence exists here -- the real `fwtutil`
  already rounds padding correctly) -- all identical.
- **Monarch vs real `fwtablectl`**: table-region byte-for-byte diff
  after an equivalent `firmware update`, with the two known
  `wdmch-tools` bugs (padding round-up, `PartEntry.EmmcPartID` always
  reading back as 0) explicitly accounted for -- every other byte
  matched.
- **Sequential overwrites** (2-3 rounds on the same slot): read back
  and verified after *every* round, not just the last, on both
  platforms.
- **Deliberate oversized-file refusal**: confirmed verona-tool refuses
  (exit 3) and leaves the image's hash unchanged, for both platforms,
  without comparing against the originals (which do not refuse at
  all -- proven earlier in this project by writing an oversized file
  via the real `fwtutil` and watching it silently corrupt the next
  slot).
- **Backup -> mutate -> restore round-trip** (`-b`/`-w`/`-e`, which has
  no original-tool equivalent): confirmed the table is byte-identical
  before backup and after restore, on both platforms.
- **`-l`** was exercised indirectly through the backup/restore
  scenario's table reads; not separately scripted in this short run.

### A real bug found and fixed *before* this run

During initial smoke-testing (before the timed run started),
`Device.Boundary()` and `Device.TableRegionBoundary()` (Monarch path)
were found to return the *absolute* byte offset of the next GPT
partition instead of the *distance* to it from the current entry's own
offset -- e.g. a 40 MiB file was silently accepted into a 32 MiB
`Kernel` slot because the check compared the file size against
~33 MB (the position of the next partition on disk) rather than
~32 MB (the actual available space). Caught immediately by the first
few seconds of the autotest run itself (`refusal` scenario on slot 3)
pointing at a stale pre-fix ARM64 binary still sitting in the
container; fixed in `pkg/verona/device.go`
(commit `13af6c9`), re-verified against the exact two cases the
GPT-based design was meant to solve (`uBoot`: correctly refuses above
1 MiB, not the inflated 3 MiB a naive "next *table* entry" gap would
allow; `RescueAudio`: correctly refuses above 4 MiB, not 5 MiB), then
rebuilt and re-copied into the container before the timed run began.

## Next steps

- Run the full planned 1-hour pass once these results are reviewed.
- Add a scripted, standalone `-l` check (currently only exercised
  indirectly).
- Consider widening scenario coverage to the `-k`/`-t`/`-o` shortcuts
  specifically (this run exercised plain numeric `-r`/`-w` only).
