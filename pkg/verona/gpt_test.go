package verona

import "testing"

// Edge case #13 from the autotest plan: a slot that is physically the LAST
// partition by LBA on the disk must be bounded by the GPT backup reserve
// (device_sectors-33 sectors), not by the device's raw size. Covered here
// as a direct unit test of NextBoundaryBytes rather than through the full
// CLI/qemu harness, since constructing a VERONA fw-entry that points at the
// last GPT partition would require hand-crafting a non-representative
// table; the boundary math itself is what matters and is independent of
// which fw-entry happens to reference it.
func TestNextBoundaryBytes_LastPartitionUsesGPTBackupReserve(t *testing.T) {
	totalSectors := uint64(2_000_000)
	parts := []GPTPartition{
		{Name: "FW_TABLE", StartLBA: 34, EndLBA: 2047},
		{Name: "KERNEL_A", StartLBA: 2048, EndLBA: 67583},
		{Name: "LAST_PART", StartLBA: 1_500_000, EndLBA: totalSectors - GPTBackupReserveSectors - 1},
	}

	// A partition that is NOT last: bounded by the next partition's start.
	got, err := NextBoundaryBytes(parts, totalSectors, 2048)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := uint64(1_500_000) * sectorSize; got != want {
		t.Errorf("non-last partition: got boundary %d, want %d", got, want)
	}

	// The last partition by LBA: bounded by the GPT backup reserve, not by
	// totalSectors itself.
	got, err = NextBoundaryBytes(parts, totalSectors, 1_500_000)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := (totalSectors - GPTBackupReserveSectors) * sectorSize
	if got != want {
		t.Errorf("last partition: got boundary %d, want %d (= (totalSectors-%d)*512)", got, want, GPTBackupReserveSectors)
	}

	// A write of exactly up to that boundary (relative to LAST_PART's own
	// start) must be allowed; the Device.Boundary() caller subtracts the
	// slot's own offset from this absolute value, so the AVAILABLE size
	// for LAST_PART is boundary - LAST_PART.StartLBA*512.
	available := want - 1_500_000*sectorSize
	if available == 0 {
		t.Fatalf("test setup produced a zero-size available region, adjust totalSectors/StartLBA")
	}
	t.Logf("LAST_PART available size = %d bytes (a write of exactly this many bytes must succeed, one more byte must be refused)", available)
}

// At the standard default GPT array size (128 entries), confirm there's no
// off-by-one at the array edge: a partition near the end (index 126 of 127)
// must be bounded by the very last partition's start, and the actual last
// partition (index 127) must fall through to the GPT backup reserve --
// exactly like the small-scale case above, just at the real array size a
// fully-populated disk would actually have.
func TestNextBoundaryBytes_128EntryArray(t *testing.T) {
	const n = 128
	totalSectors := uint64(2_000_000)
	parts := make([]GPTPartition, n)
	for i := 0; i < n; i++ {
		start := uint64(2048 + i*2048)
		parts[i] = GPTPartition{Name: "FILLER", StartLBA: start, EndLBA: start + 2047}
	}

	// Second-to-last entry: bounded by the last entry's start.
	got, err := NextBoundaryBytes(parts, totalSectors, parts[n-2].StartLBA)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if want := parts[n-1].StartLBA * sectorSize; got != want {
		t.Errorf("second-to-last of %d: got boundary %d, want %d", n, got, want)
	}

	// The actual last entry (index 127): bounded by the GPT backup reserve.
	got, err = NextBoundaryBytes(parts, totalSectors, parts[n-1].StartLBA)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := (totalSectors - GPTBackupReserveSectors) * sectorSize
	if got != want {
		t.Errorf("last of %d: got boundary %d, want %d", n, got, want)
	}
}

func TestNextBoundaryBytes_DeviceTooSmallForReserve(t *testing.T) {
	parts := []GPTPartition{{Name: "X", StartLBA: 10, EndLBA: 20}}
	if _, err := NextBoundaryBytes(parts, GPTBackupReserveSectors, 10); err == nil {
		t.Error("expected an error when totalSectors <= GPTBackupReserveSectors, got nil")
	}
}
