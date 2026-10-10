// Package verona implements the on-disk VERONA firmware table format shared
// by the WD My Cloud Home (Monarch/MCH) and My Cloud Home Duo (Pelican/MCHD).
package verona

import (
	"bytes"
	"encoding/binary"
	"fmt"
)

const (
	HeaderSize    = 32
	PartEntrySize = 48
	FwEntrySize   = 32
	Signature     = "VERONA__"

	// MaxEntries guards against parsing a corrupt header into an absurd
	// allocation.
	MaxEntries = 10000
)

// FwType is the firmware-entry type enum stored as a single byte.
type FwType uint8

const (
	Reserved FwType = iota
	Bootcode
	Kernel
	RescueDeviceTree
	KernelDeviceTree
	RescueRootFS
	KernelRootFS
	Audio
	AudioFile
	VideoFile
	Ext4
	Ubifs
	Squash
	Ext3
	Odd
	YAFFS2
	ISO
	Swap
	NTFS
	JFFS2
	ImageFile
	ImageFile1
	ImageFile2
	AudioFile1
	AudioFile2
	VideoFile1
	VideoFile2
	Video
	Video2
	ECPU
	Tee
	GoldKernel
	GoldRescueDeviceTree
	GoldRescueRootfs
	GoldAudio
	GoldTee
	Config
	UBoot
	BL31
	Hypervisor
	GoldBL31
	RSAKeyFW
	RSAKeyTee
	RescueKernel
	RescueAudio
	RescueConfig
	UnknownFwType
)

var fwTypeNames = []string{
	"Reserved", "Bootcode", "Kernel", "RescueDeviceTree", "KernelDeviceTree",
	"RescueRootFS", "KernelRootFS", "Audio", "AudioFile", "VideoFile", "Ext4",
	"Ubifs", "Squash", "Ext3", "Odd", "YAFFS2", "ISO", "Swap", "NTFS", "JFFS2",
	"ImageFile", "ImageFile1", "ImageFile2", "AudioFile1", "AudioFile2",
	"VideoFile1", "VideoFile2", "Video", "Video2", "eCPU", "Tee", "GoldKernel",
	"GoldRescueDeviceTree", "GoldRescueRootfs", "GoldAudio", "GoldTee",
	"Config", "uBoot", "BL31", "Hypervisor", "GoldBL31", "RSAKeyFW",
	"RSAKeyTee", "RescueKernel", "RescueAudio", "RescueConfig", "Unknown",
}

func (t FwType) String() string {
	if int(t) < len(fwTypeNames) {
		return fwTypeNames[t]
	}
	return fmt.Sprintf("Invalid(%d)", uint8(t))
}

// Slot index -> type, as validated against real Monarch and Duo hardware
// dumps earlier in this project's history. Not used to bound fwcount --
// always trust the on-disk header for that -- only used for the -k/-t/-o
// shortcut slot groups.
const (
	SlotGoldKernel             = 0
	SlotGoldRescueDeviceTree   = 1
	SlotGoldRescueRootfs       = 2
	SlotGoldAudio              = 3
	SlotUBoot                  = 4
	SlotKernel                 = 5
	SlotRescueDeviceTree       = 6
	SlotKernelDeviceTree       = 7
	SlotRescueRootFS           = 8
	SlotKernelRootFS           = 9
	SlotAudio                  = 10
	SlotRescueAudio            = 11
	SlotRescueKernel           = 12
)

// Header is the 32-byte table header.
type Header struct {
	Signature   [8]byte
	Checksum    uint32
	Version     uint8
	Reserved    [7]byte
	Paddings    uint32
	PartListLen uint32
	FwListLen   uint32
}

// PartEntry is one 48-byte partition-list entry.
type PartEntry struct {
	Type       uint8
	Options    uint8
	Length     uint64
	FwCount    uint8
	FwType     uint8
	EmmcPartID uint8
	Reserved   [3]byte
	MountPoint [32]byte
}

func (p *PartEntry) ReadOnly() bool { return p.Options&0x80 != 0 }

func (p *PartEntry) MountPointString() string {
	n := bytes.IndexByte(p.MountPoint[:], 0)
	if n == -1 {
		n = len(p.MountPoint)
	}
	return string(p.MountPoint[:n])
}

// FwEntry is one 32-byte firmware-list entry.
type FwEntry struct {
	Type          uint8
	Options       uint8
	Version       uint32
	TargetAddress uint32
	Offset        uint32
	Length        uint32
	Paddings      uint32
	Checksum      uint32
	Reserved      [6]byte
}

func (f *FwEntry) ReadOnly() bool { return f.Options&0x80 != 0 }
func (f *FwEntry) Lzma() bool     { return f.Options&0x40 != 0 }
func (f *FwEntry) FwType() FwType { return FwType(f.Type) }

// Table is a fully parsed VERONA table together with the raw bytes it was
// parsed from (header+parts+fws, i.e. exactly HeaderSize+PartListLen+FwListLen
// bytes -- NOT including the trailing zero padding out to Paddings).
type Table struct {
	Header Header
	Parts  []PartEntry
	Fws    []FwEntry

	// Raw is the exact byte image the table was parsed from, length
	// HeaderSize+PartListLen+FwListLen. Mutating Header/Parts/Fws does not
	// update Raw automatically -- call Encode() to re-serialize.
	Raw []byte
}

// checksum is the simple byte-sum algorithm used by the real vendor tools:
// sum of all bytes (mod 2^32), computed over header[12:] (skipping the
// 8-byte Signature and 4-byte Checksum fields) concatenated with every
// PartEntry's raw bytes and every FwEntry's raw bytes. The trailing zero
// padding out to Paddings is NOT included.
func checksum(data []byte) uint32 {
	var s uint32
	for _, b := range data {
		s += uint32(b)
	}
	return s
}

// bodyForChecksum returns header[12:]+parts+fws from a raw table image of
// at least HeaderSize+partListLen+fwListLen bytes.
func bodyForChecksum(raw []byte, partListLen, fwListLen uint32) []byte {
	end := HeaderSize + int(partListLen) + int(fwListLen)
	return raw[12:end]
}

// ParseHeader reads and validates just the 32-byte header, without
// requiring the rest of the table to be present yet. It is used by callers
// that need PartListLen/FwListLen before deciding how many more bytes to
// read (e.g. from a block device).
func ParseHeader(raw []byte) (Header, error) {
	if len(raw) < HeaderSize {
		return Header{}, fmt.Errorf("short header read: %d bytes", len(raw))
	}
	var h Header
	if err := binary.Read(bytes.NewReader(raw[:HeaderSize]), binary.LittleEndian, &h); err != nil {
		return Header{}, err
	}
	if string(h.Signature[:]) != Signature {
		return Header{}, ErrSignature
	}
	if h.PartListLen%PartEntrySize != 0 {
		return Header{}, fmt.Errorf("%w: PartListLen %d not a multiple of %d", ErrCorrupt, h.PartListLen, PartEntrySize)
	}
	if h.FwListLen%FwEntrySize != 0 {
		return Header{}, fmt.Errorf("%w: FwListLen %d not a multiple of %d", ErrCorrupt, h.FwListLen, FwEntrySize)
	}
	partCount := h.PartListLen / PartEntrySize
	fwCount := h.FwListLen / FwEntrySize
	if partCount > MaxEntries || fwCount > MaxEntries {
		return Header{}, fmt.Errorf("%w: implausible entry counts part=%d fw=%d", ErrCorrupt, partCount, fwCount)
	}
	return h, nil
}

// ErrSignature and ErrCorrupt are sentinel errors; callers map them to the
// "table invalid" exit code.
var (
	ErrSignature = fmt.Errorf("signature error: expected %q", Signature)
	ErrCorrupt   = fmt.Errorf("corrupt table")
)

// Parse decodes a full table from raw bytes that already contain at least
// HeaderSize+PartListLen+FwListLen bytes (ParseHeader must have already been
// used, or the caller must know the sizes in advance).
func Parse(raw []byte) (*Table, error) {
	h, err := ParseHeader(raw)
	if err != nil {
		return nil, err
	}
	need := HeaderSize + int(h.PartListLen) + int(h.FwListLen)
	if len(raw) < need {
		return nil, fmt.Errorf("short table read: have %d bytes, need %d", len(raw), need)
	}
	t := &Table{Header: h, Raw: raw[:need]}

	partCount := int(h.PartListLen / PartEntrySize)
	t.Parts = make([]PartEntry, partCount)
	off := HeaderSize
	for i := 0; i < partCount; i++ {
		if err := binary.Read(bytes.NewReader(raw[off:off+PartEntrySize]), binary.LittleEndian, &t.Parts[i]); err != nil {
			return nil, fmt.Errorf("part %d: %w", i, err)
		}
		off += PartEntrySize
	}

	fwCount := int(h.FwListLen / FwEntrySize)
	t.Fws = make([]FwEntry, fwCount)
	for i := 0; i < fwCount; i++ {
		if err := binary.Read(bytes.NewReader(raw[off:off+FwEntrySize]), binary.LittleEndian, &t.Fws[i]); err != nil {
			return nil, fmt.Errorf("fw %d: %w", i, err)
		}
		off += FwEntrySize
	}

	return t, nil
}

// VerifyChecksum reports whether the table's stored checksum matches the
// computed one.
func (t *Table) VerifyChecksum() error {
	got := checksum(bodyForChecksum(t.Raw, t.Header.PartListLen, t.Header.FwListLen))
	if got != t.Header.Checksum {
		return fmt.Errorf("checksum mismatch: stored 0x%08x, computed 0x%08x", t.Header.Checksum, got)
	}
	return nil
}

// Encode re-serializes Header/Parts/Fws into Raw (HeaderSize+PartListLen+
// FwListLen bytes) and recomputes+stores the checksum. Call this once after
// mutating any FwEntry, not per-entry.
func (t *Table) Encode() error {
	t.Header.PartListLen = uint32(len(t.Parts) * PartEntrySize)
	t.Header.FwListLen = uint32(len(t.Fws) * FwEntrySize)
	total := HeaderSize + int(t.Header.PartListLen) + int(t.Header.FwListLen)

	buf := new(bytes.Buffer)
	// Placeholder checksum of 0 while we serialize, filled in below.
	t.Header.Checksum = 0
	if err := binary.Write(buf, binary.LittleEndian, &t.Header); err != nil {
		return err
	}
	for i := range t.Parts {
		if err := binary.Write(buf, binary.LittleEndian, &t.Parts[i]); err != nil {
			return fmt.Errorf("part %d: %w", i, err)
		}
	}
	for i := range t.Fws {
		if err := binary.Write(buf, binary.LittleEndian, &t.Fws[i]); err != nil {
			return fmt.Errorf("fw %d: %w", i, err)
		}
	}
	raw := buf.Bytes()
	if len(raw) != total {
		return fmt.Errorf("internal error: encoded %d bytes, expected %d", len(raw), total)
	}
	cs := checksum(raw[12:])
	binary.LittleEndian.PutUint32(raw[8:12], cs)
	t.Header.Checksum = cs
	t.Raw = raw
	return nil
}

// RoundUpTo512 rounds n up to the next multiple of 512, but -- matching the
// real fwtutil binary's verified behaviour, NOT the buggy wdmch-tools Go
// reimplementation -- returns n unchanged when it is already a multiple of
// 512.
func RoundUpTo512(n uint32) uint32 {
	if n%512 == 0 {
		return n
	}
	return n + (512 - n%512)
}
