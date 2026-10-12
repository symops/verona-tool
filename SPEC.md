# verona-tool specification

## On-disk VERONA table format

Header, 32 bytes, little-endian:
`Signature[8]="VERONA__"`, `Checksum uint32 @8`, `Version uint8 @12`,
`Reserved[7] @13-19`, `Paddings uint32 @20` (total table size incl.
padding), `PartListLen uint32 @24`, `FwListLen uint32 @28`.

PartEntry, 48 bytes: `Type uint8 @0` (0=reserved,1=firmware,
2=filesystem), `Options uint8 @1` (bit7=ReadOnly), `Length uint64
@2-9`, `FwCount uint8 @10`, `FwType uint8 @11` (0=jffs2,1=yaffs2,
2=squash,3=rawfile,4=ext4,5=ubifs,6=none,7=unknown), `EmmcPartID uint8
@12`, `Reserved[3] @13-15`, `MountPoint[32] @16-47` (NUL-padded).

FwEntry, 32 bytes: `Type uint8 @0` (enum below), `Options uint8 @1`
(bit6=Lzma, bit7=ReadOnly), `Version uint32 @2-5`, `TargetAddress
uint32 @6-9`, `Offset uint32 @10-13` (absolute byte offset on the
whole device), `Length uint32 @14-17`, `Paddings uint32 @18-21`
(length rounded up to 512, unchanged if already a multiple of 512),
`Checksum uint32 @22-25`, `Reserved[6] @26-31`.

FwType enum (index = stored byte): 0 Reserved, 1 Bootcode, 2 Kernel,
3 RescueDeviceTree, 4 KernelDeviceTree, 5 RescueRootFS, 6
KernelRootFS, 7 Audio, 8 AudioFile, 9 VideoFile, 10 Ext4, 11 Ubifs,
12 Squash, 13 Ext3, 14 Odd, 15 YAFFS2, 16 ISO, 17 Swap, 18 NTFS,
19 JFFS2, 20 ImageFile, 21 ImageFile1, 22 ImageFile2, 23 AudioFile1,
24 AudioFile2, 25 VideoFile1, 26 VideoFile2, 27 Video, 28 Video2,
29 eCPU, 30 Tee, 31 GoldKernel, 32 GoldRescueDeviceTree,
33 GoldRescueRootfs, 34 GoldAudio, 35 GoldTee, 36 Config, 37 uBoot,
38 BL31, 39 Hypervisor, 40 GoldBL31, 41 RSAKeyFW, 42 RSAKeyTee,
43 RescueKernel, 44 RescueAudio, 45 RescueConfig, 46 Unknown.

Checksum: simple sum of bytes mod 2^32, over header[12:32] + all
PartEntry bytes + all FwEntry bytes (not including trailing zero
padding out to `Paddings`). Validated against two real production
dumps (one Monarch, one Duo).

Known index-to-type mapping (both platforms, validated against real
hardware, both real tables examined had exactly 13 entries in this
order -- never hardcode "13", always read `fwcount` from the header):
`0 GoldKernel, 1 GoldRescueDeviceTree, 2 GoldRescueRootfs, 3 GoldAudio,
4 uBoot, 5 Kernel, 6 RescueDeviceTree, 7 KernelDeviceTree, 8
RescueRootFS, 9 KernelRootFS, 10 Audio, 11 RescueAudio, 12
RescueKernel`.

## Locating the table

- **Duo (MCHD)**: fixed absolute byte offset `6,422,528` (`0x620000`)
  on `-d`. Empirically confirmed against a real `fwtutil` binary. Not
  the first occurrence of "VERONA__" in a raw dump (a decoy exists
  inside filesystem data, plus a stale backup copy of the real table
  at +65536 bytes -- never read from or written to, by anything).
- **Monarch (MCH)**: parse the real GPT on `-d` at runtime (GPT header
  at LBA1, partition array per its own fields), find the partition
  named `FW_TABLE`, use its StartingLBA. Never hardcode LBA 34 --
  layouts can differ between units.
- Both: verify signature "VERONA__" exactly at that position (else
  exit 2), `PartListLen % 48 == 0` and `FwListLen % 32 == 0` (else
  exit 2), and `partcount`/`fwcount` each `<= 10000` (else exit 2).

## Boundary computation (for the write-overflow refusal only -- there
is no padding/zero-fill feature at all; a write places exactly
`len(file)` bytes, nothing more)

- **Monarch**: parse the real GPT at runtime, sort *all* partitions by
  StartingLBA (not just ones with a table entry), boundary = start of
  the next partition by LBA order. If the slot is the last partition
  by LBA, boundary = `(total_sectors - 33) * 512` -- 33 sectors is the
  standard UEFI GPT backup-header + backup-partition-array reserve
  (default 128-entry array), not a reference-image-specific number.
  Using "next fw-table entry" instead of "next real GPT partition"
  would overestimate the boundary for slots followed by a partition
  with no table entry (empirically proven: this would overestimate
  uBoot's true 1MB boundary as 3MB, and RescueAudio's true 4MB as 5MB).
- **Duo**: sort all fw-table entries by Offset, boundary = next
  entry's Offset minus this entry's Offset. If last by offset,
  boundary = size of `-d`. **Known, accepted limitation**: Duo has no
  GPT and no independent way to detect unlisted reserved regions
  between entries; this is not fixable without more reference data.
- File size > boundary -> refuse entirely, write nothing, exit 3.

## CLI

`-p`/`--platform` VALUE (required). Aliases: `m,monarch,mch,home` ->
Monarch; `p,pelican,mchd,duo` -> Duo; `a,auto` -> auto-detect from
`-d`'s content.

`-d`/`--device` PATH. Required for Monarch; optional for Duo (default
`/dev/mmcblk0`); **always required when `-p` is `auto`** (no default
can be applied before the platform is known). A plain file works too
(needed for testing).

### Auto-detection (`-p auto`)

Independently validates both candidate layouts on `-d`:
- **Monarch candidate**: a valid GPT header at LBA1 (signature
  `EFI PART`, revision `0x00010000`, and a correct header CRC32 --
  stricter than the plain signature check used once a platform is
  already known), a partition named `FW_TABLE` in its partition
  array, and a valid (signature + checksum) VERONA table at that
  partition's `StartingLBA`.
- **Duo candidate**: a valid (signature + checksum) VERONA table at
  the fixed offset 6,422,528, ignoring GPT entirely.

Both are always checked independently (neither short-circuits the
other). Exactly one validating -> use that platform. Both validating
-> ambiguous, exit 1, telling the user to specify `-p` explicitly.
Neither validating -> exit 1 with both candidates' failure reasons.

`-r`/`--read` `SLOT[:FILE]` or `SLOT=FILE` (repeatable). FILE omitted
-> `./SLOT.img`. Reading an empty (length 0) slot -> empty file, no
error. Duplicate reads of the same slot are allowed (warning only).
All `-r` run before any `-w`.

`-w`/`--write` `SLOT:FILE` or `SLOT=FILE` (repeatable, FILE required).
Updates only `Length`/`Paddings`/`Checksum`; never touches
`Type`/`Options`/`Version`/`TargetAddress`. Checked against the
boundary above before writing anything. Table checksum is recomputed
once, after every `-w` has been applied.

`-k`/`--kernel`, `-t`/`--dtb`, `-f`/`--rootfs` -- shortcuts for slot
pairs `{5,12}`, `{7,6}`, `{9,8}`. Must be combined with an explicit
bare `-r` or `-w` to select mode (a shortcut with neither present is a
usage error). Read mode: no value, auto-names both outputs by slot
number. Write mode: requires `=FILE` (long-option attachment syntax
only -- `getopt_long`-style space-separated optional arguments aren't
supported, hence the custom parser), same file written identically
into both slots, checked atomically against both boundaries before
writing to either.

`-B`/`--backup` `[FILE]` -- default
`verona-table-backup-<platform>-<YYYYMMDDHHMMSS>.img`. Saves the
whole table region (exactly `Paddings` bytes). Always runs first,
unconditionally, even if later `-r`/`-w` in the same call end up
failing. Compatible with `-r`/`-w`/`-k`/`-t`/`-f`; not with `-l`/`-R`.

`-l`/`--list` -- prints the full table, exits. Fully exclusive.

`-R`/`--restore` `FILE` (required). Validates FILE's own signature,
checksum, alignment, and that its `Paddings` does not exceed the real
space available for the table region on the current `-d`, before
writing it back wholesale. Fully exclusive, including vs. `-B`.

`-y`/`--dry-run` -- compatible with everything. Performs every
read-side check and reports what would happen / what would fail,
without writing anything. Exits 0 whenever the simulation itself ran
to completion (even if it reports that the real run would fail).
Alone (no action flag) is a usage error.

`-D`/`--debug` -- compatible with everything. Prints extra internal
diagnostics (table location, computed boundaries, lock acquisition,
byte offsets, checksums) alongside the normal output. Purely additive:
has no effect on what is actually done or on the resulting file/table
state.

`-h`/`--help`, `-v`/`--version` -- fully exclusive, must be used
alone. Version author: Symo <symops@gmail.com>.

### Ordering & validation

- At least one of `-r`/`-w`/`-B`/`-l`/`-R` required, unless `-h`/`-v`
  present.
- Order: `-B` -> all `-r` -> all `-w` (textual order on the command
  line is irrelevant).
- Conflict (exit 5): the same slot targeted by more than one `-w`
  after shortcut expansion; the same shortcut given twice in write
  mode; an explicit `-w N` whose N also belongs to an active
  write-mode shortcut group.
- Output-path collisions across `-r`/`-B` targets -> warning, not an
  error.
- Locking: `flock` on `-d` for the whole invocation, non-blocking.
  Shared lock for read-only invocations, exclusive if any
  write/restore is present. Already locked -> immediate failure, exit
  7, no retry.
- After every data write and every table write (including `-R`):
  re-read the exact range just written and compare to the source;
  mismatch -> exit 6, no retry.
- `fsync` after every write, before close.
- No `-w` in the invocation -> the table is never rewritten at all.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | argument/usage error |
| 2 | signature/table-corrupt/backup-validation error |
| 3 | file too large for the destination slot -- refused |
| 4 | slot index out of range |
| 5 | argument conflict |
| 6 | post-write verification mismatch |
| 7 | device locked by another process |
