package verona

import (
	"fmt"
	"os"
	"sort"
	"syscall"
)

type Platform int

const (
	PlatformMonarch Platform = iota // MCH
	PlatformDuo                     // MCHD
	// PlatformAuto is a pseudo-platform meaning "detect from -d's content"
	// (ParsePlatform's "a"/"auto" alias). Callers must resolve it via
	// DetectPlatform before using it anywhere a real Platform is expected
	// (Device.Open rejects it).
	PlatformAuto
)

func (p Platform) String() string {
	switch p {
	case PlatformMonarch:
		return "monarch"
	case PlatformDuo:
		return "duo"
	default:
		return "auto"
	}
}

// ParsePlatform accepts the aliases agreed for -p/--platform.
func ParsePlatform(s string) (Platform, error) {
	switch s {
	case "m", "monarch", "mch", "home":
		return PlatformMonarch, nil
	case "p", "pelican", "mchd", "duo":
		return PlatformDuo, nil
	case "a", "auto":
		return PlatformAuto, nil
	}
	return 0, fmt.Errorf("unknown platform %q", s)
}

// DuoTableOffset is the fixed, empirically-confirmed absolute byte offset
// of the real, active VERONA table on a Duo (MCHD) eMMC device. Do not
// confuse with the decoy signature occurrence found elsewhere in a raw
// image, or with the stale backup copy 65536 bytes later -- neither is ever
// read from or written to by this tool.
const DuoTableOffset = 6422528

const fwTablePartitionName = "FW_TABLE"

// Device wraps an open disk/image file together with everything needed to
// locate and interpret its VERONA table.
type Device struct {
	f        *os.File
	Platform Platform

	TableOffset int64 // absolute byte offset of the table's own location
	Table       *Table

	// Monarch-only: the parsed GPT, used for boundary computation.
	gptParts     []GPTPartition
	gptTotalSect uint64
}

// Open opens path for the given platform, locates the table, and fully
// validates it (signature, alignment, checksum). readWrite selects O_RDONLY
// vs O_RDWR.
func Open(path string, platform Platform, readWrite bool) (*Device, error) {
	if platform == PlatformAuto {
		return nil, fmt.Errorf("internal error: PlatformAuto must be resolved via DetectPlatform before calling Open")
	}
	flag := os.O_RDONLY
	if readWrite {
		flag = os.O_RDWR
	}
	f, err := os.OpenFile(path, flag, 0)
	if err != nil {
		return nil, err
	}
	d := &Device{f: f, Platform: platform}

	if platform == PlatformDuo {
		d.TableOffset = DuoTableOffset
	} else {
		parts, total, err := ReadGPT(f)
		if err != nil {
			f.Close()
			return nil, fmt.Errorf("reading GPT on %s: %w", path, err)
		}
		fw, err := FindPartitionByName(parts, fwTablePartitionName)
		if err != nil {
			f.Close()
			return nil, fmt.Errorf("locating VERONA table on %s: %w", path, err)
		}
		d.gptParts = parts
		d.gptTotalSect = total
		d.TableOffset = int64(fw.StartLBA) * sectorSize
	}

	hdrBuf := make([]byte, HeaderSize)
	if _, err := f.ReadAt(hdrBuf, d.TableOffset); err != nil {
		f.Close()
		return nil, fmt.Errorf("reading table header: %w", err)
	}
	hdr, err := ParseHeader(hdrBuf)
	if err != nil {
		f.Close()
		return nil, err
	}
	need := HeaderSize + int(hdr.PartListLen) + int(hdr.FwListLen)
	full := make([]byte, need)
	if _, err := f.ReadAt(full, d.TableOffset); err != nil {
		f.Close()
		return nil, fmt.Errorf("reading full table: %w", err)
	}
	table, err := Parse(full)
	if err != nil {
		f.Close()
		return nil, err
	}
	if err := table.VerifyChecksum(); err != nil {
		f.Close()
		return nil, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	d.Table = table
	return d, nil
}

// Lock acquires an flock on the underlying file descriptor; exclusive=true
// for LOCK_EX, false for LOCK_SH. Non-blocking: returns an error
// immediately if the lock is already held elsewhere, never waits/retries.
func (d *Device) Lock(exclusive bool) error {
	how := syscall.LOCK_SH | syscall.LOCK_NB
	if exclusive {
		how = syscall.LOCK_EX | syscall.LOCK_NB
	}
	if err := syscall.Flock(int(d.f.Fd()), how); err != nil {
		return fmt.Errorf("device is locked by another process: %w", err)
	}
	return nil
}

func (d *Device) Close() error {
	return d.f.Close()
}

// ReadAt/WriteAt expose the underlying file for data I/O elsewhere in the
// package and in cmd/verona-tool.
func (d *Device) ReadAt(p []byte, off int64) (int, error)  { return d.f.ReadAt(p, off) }
func (d *Device) WriteAt(p []byte, off int64) (int, error) { return d.f.WriteAt(p, off) }
func (d *Device) Sync() error                              { return d.f.Sync() }

// Size returns the device/file's total size in bytes.
func (d *Device) Size() (int64, error) {
	fi, err := d.f.Stat()
	if err != nil {
		return 0, err
	}
	return fi.Size(), nil
}

// Boundary computes the maximum number of bytes that may be written to the
// firmware entry at index slot, per the rules agreed for each platform:
//   - Monarch: start of the next real GPT partition by LBA order (whether
//     or not it has a table entry), or the GPT backup reserve if slot is
//     the last partition on the disk.
//   - Duo: distance to the next table entry by Offset, or the device's own
//     size if slot is the last entry by offset (an accepted limitation --
//     Duo has no independent structure to detect unlisted reserved
//     regions between entries).
func (d *Device) Boundary(slot int) (uint64, error) {
	if slot < 0 || slot >= len(d.Table.Fws) {
		return 0, fmt.Errorf("slot %d out of range (table has %d entries)", slot, len(d.Table.Fws))
	}
	off := uint64(d.Table.Fws[slot].Offset)

	if d.Platform == PlatformMonarch {
		startLBA := off / sectorSize
		if off%sectorSize != 0 {
			return 0, fmt.Errorf("slot %d offset %d is not sector-aligned", slot, off)
		}
		nextStart, err := NextBoundaryBytes(d.gptParts, d.gptTotalSect, startLBA)
		if err != nil {
			return 0, err
		}
		// NextBoundaryBytes returns the ABSOLUTE byte offset of the next
		// boundary (next partition's start, or the GPT backup reserve);
		// the available size for this slot is the distance from its own
		// offset to that point.
		if nextStart <= off {
			return 0, fmt.Errorf("slot %d: computed next boundary %d is not after its own offset %d", slot, nextStart, off)
		}
		return nextStart - off, nil
	}

	// Duo: gap to next fw entry by offset.
	offsets := make([]uint64, 0, len(d.Table.Fws))
	for _, f := range d.Table.Fws {
		offsets = append(offsets, uint64(f.Offset))
	}
	sort.Slice(offsets, func(i, j int) bool { return offsets[i] < offsets[j] })
	for i, o := range offsets {
		if o == off {
			if i+1 < len(offsets) && offsets[i+1] > off {
				return offsets[i+1] - off, nil
			}
			// Last by offset (or duplicate offsets collapse to gap 0,
			// which is an intentional fail-safe, not a crash).
			size, err := d.Size()
			if err != nil {
				return 0, err
			}
			if uint64(size) <= off {
				return 0, nil
			}
			return uint64(size) - off, nil
		}
	}
	return 0, fmt.Errorf("internal error: slot %d offset not found among entries", slot)
}

// TableRegionBoundary returns how large a replacement table (e.g. supplied
// to --restore) may be without overflowing into the first real firmware
// payload, for sanity-checking a backup file before restoring it.
func (d *Device) TableRegionBoundary() (uint64, error) {
	if d.Platform == PlatformMonarch {
		tableStartLBA := uint64(d.TableOffset) / sectorSize
		nextStart, err := NextBoundaryBytes(d.gptParts, d.gptTotalSect, tableStartLBA)
		if err != nil {
			return 0, err
		}
		if nextStart <= uint64(d.TableOffset) {
			return 0, fmt.Errorf("computed next boundary %d is not after the table's own offset %d", nextStart, d.TableOffset)
		}
		return nextStart - uint64(d.TableOffset), nil
	}
	// Duo: distance from the fixed table offset to the smallest fw-entry
	// offset (the table and firmware blobs share the same raw address
	// space with no GPT to consult).
	min := ^uint64(0)
	for _, f := range d.Table.Fws {
		if uint64(f.Offset) < min {
			min = uint64(f.Offset)
		}
	}
	if min == ^uint64(0) || min <= uint64(d.TableOffset) {
		size, err := d.Size()
		if err != nil {
			return 0, err
		}
		return uint64(size) - uint64(d.TableOffset), nil
	}
	return min - uint64(d.TableOffset), nil
}
