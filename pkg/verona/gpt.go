package verona

import (
	"encoding/binary"
	"fmt"
	"io"
	"sort"
	"unicode/utf16"
)

const sectorSize = 512

// GPTPartition is one entry from a GPT partition array, trimmed to the
// fields we need.
type GPTPartition struct {
	Name       string
	StartLBA   uint64
	EndLBA     uint64 // inclusive, per the GPT spec
}

// ReadGPT parses the primary GPT header (at LBA 1) and its partition array
// from r, and returns the partitions sorted by StartLBA, plus the total
// device size in sectors (read from the GPT header's AlternateLBA, which
// points at the last usable LBA of the device == backup header location).
func ReadGPT(r io.ReaderAt) ([]GPTPartition, uint64, error) {
	hdr := make([]byte, sectorSize)
	if _, err := r.ReadAt(hdr, 1*sectorSize); err != nil {
		return nil, 0, fmt.Errorf("reading GPT header: %w", err)
	}
	if string(hdr[0:8]) != "EFI PART" {
		return nil, 0, fmt.Errorf("no GPT signature at LBA1 (found %q)", hdr[0:8])
	}
	partEntryLBA := binary.LittleEndian.Uint64(hdr[72:80])
	numEntries := binary.LittleEndian.Uint32(hdr[80:84])
	entrySize := binary.LittleEndian.Uint32(hdr[84:88])
	altLBA := binary.LittleEndian.Uint64(hdr[32:40]) // AlternateLBA: backup header's own LBA, == last LBA of device

	if numEntries == 0 || numEntries > MaxEntries || entrySize < 128 {
		return nil, 0, fmt.Errorf("implausible GPT entry table: numEntries=%d entrySize=%d", numEntries, entrySize)
	}

	arr := make([]byte, uint64(numEntries)*uint64(entrySize))
	if _, err := r.ReadAt(arr, int64(partEntryLBA)*sectorSize); err != nil {
		return nil, 0, fmt.Errorf("reading GPT partition array: %w", err)
	}

	var parts []GPTPartition
	for i := uint32(0); i < numEntries; i++ {
		e := arr[uint64(i)*uint64(entrySize) : uint64(i)*uint64(entrySize)+uint64(entrySize)]
		typeGUID := e[0:16]
		allZero := true
		for _, b := range typeGUID {
			if b != 0 {
				allZero = false
				break
			}
		}
		if allZero {
			continue // unused entry
		}
		start := binary.LittleEndian.Uint64(e[32:40])
		end := binary.LittleEndian.Uint64(e[40:48])
		nameRaw := e[56:128]
		parts = append(parts, GPTPartition{
			Name:     decodeUTF16LEName(nameRaw),
			StartLBA: start,
			EndLBA:   end,
		})
	}

	sort.Slice(parts, func(i, j int) bool { return parts[i].StartLBA < parts[j].StartLBA })

	// altLBA is the backup GPT header's own LBA, which by spec equals the
	// last addressable LBA of the device, so device size in sectors is
	// altLBA+1.
	totalSectors := altLBA + 1
	return parts, totalSectors, nil
}

func decodeUTF16LEName(b []byte) string {
	u16 := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		v := binary.LittleEndian.Uint16(b[i : i+2])
		if v == 0 {
			break
		}
		u16 = append(u16, v)
	}
	return string(utf16.Decode(u16))
}

// FindPartitionByName returns the partition whose Name matches exactly, or
// an error if none is found.
func FindPartitionByName(parts []GPTPartition, name string) (GPTPartition, error) {
	for _, p := range parts {
		if p.Name == name {
			return p, nil
		}
	}
	return GPTPartition{}, fmt.Errorf("no GPT partition named %q", name)
}

// GPTBackupReserveSectors is the standard UEFI GPT reserve at the end of any
// disk with the default 128-entry, 128-byte partition array: 1 sector for
// the backup header + 32 sectors for the backup partition array. This is a
// protocol constant, not something derived from a single reference image.
const GPTBackupReserveSectors = 33

// NextBoundaryBytes returns, for a partition starting at startLBA among all
// GPT partitions (sorted by StartLBA), the byte offset of the start of the
// next partition by LBA order -- regardless of whether that next partition
// has any VERONA table entry -- or, if startLBA belongs to the last
// partition on the disk, (totalSectors-GPTBackupReserveSectors)*512.
func NextBoundaryBytes(parts []GPTPartition, totalSectors uint64, startLBA uint64) (uint64, error) {
	idx := -1
	for i, p := range parts {
		if p.StartLBA == startLBA {
			idx = i
			break
		}
	}
	if idx == -1 {
		return 0, fmt.Errorf("no GPT partition starts at LBA %d", startLBA)
	}
	if idx+1 < len(parts) {
		return parts[idx+1].StartLBA * sectorSize, nil
	}
	if totalSectors <= GPTBackupReserveSectors {
		return 0, fmt.Errorf("device too small (%d sectors) to hold the GPT backup reserve", totalSectors)
	}
	return (totalSectors - GPTBackupReserveSectors) * sectorSize, nil
}
