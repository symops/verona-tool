package verona

import (
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
)

// strictGPTHeaderValid checks the GPT header signature, revision, and its
// own CRC32 -- stricter than ReadGPT's plain signature check, used
// specifically by platform auto-detection where we must be confident we
// are looking at a real GPT and not a coincidental 8-byte match.
func strictGPTHeaderValid(hdr []byte) bool {
	if len(hdr) < 92 || string(hdr[0:8]) != "EFI PART" {
		return false
	}
	revision := binary.LittleEndian.Uint32(hdr[8:12])
	if revision != 0x00010000 {
		return false
	}
	headerSize := binary.LittleEndian.Uint32(hdr[12:16])
	if headerSize < 92 || int(headerSize) > len(hdr) {
		return false
	}
	storedCRC := binary.LittleEndian.Uint32(hdr[16:20])
	buf := make([]byte, headerSize)
	copy(buf, hdr[:headerSize])
	binary.LittleEndian.PutUint32(buf[16:20], 0) // CRC32 field is zeroed during its own computation
	return crc32.ChecksumIEEE(buf) == storedCRC
}

// candidateResult records whether one platform's layout validated on -d,
// and why not if it didn't (used only for diagnostics on failure).
type candidateResult struct {
	valid bool
	why   string
}

func tryMonarchCandidate(r io.ReaderAt) candidateResult {
	hdr := make([]byte, sectorSize)
	if _, err := r.ReadAt(hdr, 1*sectorSize); err != nil {
		return candidateResult{false, fmt.Sprintf("could not read LBA1: %v", err)}
	}
	if !strictGPTHeaderValid(hdr) {
		return candidateResult{false, "no valid GPT header at LBA1 (signature/revision/CRC32)"}
	}
	parts, _, err := ReadGPT(r)
	if err != nil {
		return candidateResult{false, fmt.Sprintf("GPT partition array unreadable: %v", err)}
	}
	fw, err := FindPartitionByName(parts, fwTablePartitionName)
	if err != nil {
		return candidateResult{false, "no GPT partition named FW_TABLE"}
	}
	off := int64(fw.StartLBA) * sectorSize
	if _, err := validateVeronaTableAt(r, off); err != nil {
		return candidateResult{false, fmt.Sprintf("FW_TABLE partition has no valid VERONA table: %v", err)}
	}
	return candidateResult{true, ""}
}

func tryDuoCandidate(r io.ReaderAt) candidateResult {
	if _, err := validateVeronaTableAt(r, DuoTableOffset); err != nil {
		return candidateResult{false, fmt.Sprintf("no valid VERONA table at the fixed Duo offset: %v", err)}
	}
	return candidateResult{true, ""}
}

// validateVeronaTableAt fully parses and checksum-verifies a VERONA table
// at the given absolute byte offset.
func validateVeronaTableAt(r io.ReaderAt, off int64) (*Table, error) {
	hdrBuf := make([]byte, HeaderSize)
	if _, err := r.ReadAt(hdrBuf, off); err != nil {
		return nil, err
	}
	hdr, err := ParseHeader(hdrBuf)
	if err != nil {
		return nil, err
	}
	need := HeaderSize + int(hdr.PartListLen) + int(hdr.FwListLen)
	full := make([]byte, need)
	if _, err := r.ReadAt(full, off); err != nil {
		return nil, err
	}
	table, err := Parse(full)
	if err != nil {
		return nil, err
	}
	if err := table.VerifyChecksum(); err != nil {
		return nil, err
	}
	return table, nil
}

// DetectPlatform independently validates both the Monarch (GPT + FW_TABLE
// partition + VERONA) and Duo (fixed-offset VERONA) candidate layouts on r,
// and returns the platform if -- and only if -- exactly one of them fully
// validates.
func DetectPlatform(r io.ReaderAt) (Platform, error) {
	mch := tryMonarchCandidate(r)
	duo := tryDuoCandidate(r)

	switch {
	case mch.valid && duo.valid:
		return 0, fmt.Errorf("ambiguous: both the Monarch (GPT+FW_TABLE) and Duo (fixed-offset) VERONA layouts validate on this device -- specify -p explicitly instead of auto")
	case mch.valid:
		return PlatformMonarch, nil
	case duo.valid:
		return PlatformDuo, nil
	default:
		return 0, fmt.Errorf("could not auto-detect platform: Monarch candidate failed (%s); Duo candidate failed (%s); specify -p explicitly", mch.why, duo.why)
	}
}
