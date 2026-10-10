package main

import (
	"bytes"
	"fmt"
	"os"

	"github.com/symops/verona-tool/pkg/verona"
)

func execute(p *plan) int {
	printWarnings(p)

	platform, err := verona.ParsePlatform(p.platform)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}

	disk := p.disk
	if platform == verona.PlatformAuto && disk == "" {
		fmt.Fprintln(os.Stderr, "error: -d/--disk is required when -p/--platform is auto")
		return exitArgs
	}
	if disk == "" {
		if platform == verona.PlatformMonarch {
			fmt.Fprintln(os.Stderr, "error: -d/--disk is required for the monarch/mch platform")
			return exitArgs
		}
		disk = "/dev/mmcblk0"
	}

	if platform == verona.PlatformAuto {
		f, ferr := os.Open(disk)
		if ferr != nil {
			fmt.Fprintln(os.Stderr, "error:", ferr)
			return exitTableInvalid
		}
		detected, derr := verona.DetectPlatform(f)
		f.Close()
		if derr != nil {
			fmt.Fprintln(os.Stderr, "error:", derr)
			return exitTableInvalid
		}
		platform = detected
		fmt.Printf("auto-detected platform: %s\n", platform)
	}

	// --dry-run never actually opens the device for writing or takes an
	// exclusive lock, regardless of what operations are planned -- it only
	// reads and reports what WOULD happen.
	needWrite := !p.dryRun && (p.doRestore != "" || len(p.writes) > 0)
	exclusive := needWrite

	dev, err := verona.Open(disk, platform, needWrite)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	defer dev.Close()

	if err := dev.Lock(exclusive); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitLocked
	}

	switch {
	case p.doList:
		printTable(dev)
		return exitOK
	case p.doRestore != "":
		return doRestore(dev, p.doRestore, p.dryRun)
	}

	if p.doBackup {
		if code := doBackup(dev, p.backupFile, p.dryRun); code != exitOK {
			return code
		}
	}

	for _, r := range p.reads {
		if code := doRead(dev, r, p.dryRun); code != exitOK {
			return code
		}
	}

	if len(p.writes) == 0 {
		return exitOK
	}

	// Pre-flight: compute boundaries for every write and group, refusing
	// atomically before touching the disk if anything would overflow.
	fileSizes := map[int]int64{}
	for _, w := range p.writes {
		fi, err := os.Stat(w.file)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return exitArgs
		}
		fileSizes[w.slot] = fi.Size()
	}
	overflowed := false
	for _, w := range p.writes {
		b, err := dev.Boundary(w.slot)
		if err != nil {
			if p.dryRun {
				fmt.Fprintln(os.Stderr, "would fail:", err)
				overflowed = true
				continue
			}
			fmt.Fprintln(os.Stderr, "error:", err)
			return exitSlotRange
		}
		if uint64(fileSizes[w.slot]) > b {
			msg := fmt.Sprintf("refusing: file for slot %d is %d bytes, exceeds destination boundary of %d bytes", w.slot, fileSizes[w.slot], b)
			if w.group >= 0 {
				msg += fmt.Sprintf(" (part of an atomic pair; neither slot %d nor its sibling will be written)", w.slot)
			}
			if p.dryRun {
				fmt.Fprintln(os.Stderr, "would fail:", msg, "(exit code would be", exitTooLarge, ")")
				overflowed = true
				continue
			}
			fmt.Fprintln(os.Stderr, "error:", msg)
			return exitTooLarge
		}
	}

	if p.dryRun {
		if overflowed {
			fmt.Println("dry-run: at least one write above would be refused; the real run would exit non-zero and write nothing")
			return exitOK
		}
		for _, w := range p.writes {
			fmt.Printf("would write %d bytes to slot %d (%s) from %s\n", fileSizes[w.slot], w.slot, dev.Table.Fws[w.slot].FwType(), w.file)
		}
		fmt.Println("dry-run: table checksum would be recalculated and written back")
		return exitOK
	}

	for _, w := range p.writes {
		if code := doWrite(dev, w); code != exitOK {
			return code
		}
	}

	return finalizeTable(dev)
}

func doRead(dev *verona.Device, r readOp, dryRun bool) int {
	if r.slot < 0 || r.slot >= len(dev.Table.Fws) {
		fmt.Fprintf(os.Stderr, "error: slot %d out of range (table has %d entries)\n", r.slot, len(dev.Table.Fws))
		return exitSlotRange
	}
	fw := dev.Table.Fws[r.slot]
	if dryRun {
		fmt.Printf("would read %d bytes from slot %d (%s) into %s\n", fw.Length, r.slot, fw.FwType(), r.file)
		return exitOK
	}
	buf := make([]byte, fw.Length)
	if fw.Length > 0 {
		if _, err := dev.ReadAt(buf, int64(fw.Offset)); err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return exitTableInvalid
		}
	}
	if err := os.WriteFile(r.file, buf, 0644); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}
	fmt.Printf("read slot %d (%s): %d bytes -> %s\n", r.slot, fw.FwType(), fw.Length, r.file)
	return exitOK
}

func doWrite(dev *verona.Device, w writeOp) int {
	data, err := os.ReadFile(w.file)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}
	fw := &dev.Table.Fws[w.slot]
	if _, err := dev.WriteAt(data, int64(fw.Offset)); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if err := dev.Sync(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitVerifyFailed
	}
	readback := make([]byte, len(data))
	if len(data) > 0 {
		if _, err := dev.ReadAt(readback, int64(fw.Offset)); err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return exitVerifyFailed
		}
	}
	if !bytes.Equal(readback, data) {
		fmt.Fprintf(os.Stderr, "error: post-write verification failed for slot %d\n", w.slot)
		return exitVerifyFailed
	}

	fw.Length = uint32(len(data))
	fw.Paddings = verona.RoundUpTo512(fw.Length)
	var sum uint32
	for _, b := range data {
		sum += uint32(b)
	}
	fw.Checksum = sum

	fmt.Printf("wrote slot %d (%s): %d bytes from %s, checksum 0x%08x\n", w.slot, fw.FwType(), len(data), w.file, sum)
	return exitOK
}

func finalizeTable(dev *verona.Device) int {
	if err := dev.Table.Encode(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if _, err := dev.WriteAt(dev.Table.Raw, dev.TableOffset); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if err := dev.Sync(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitVerifyFailed
	}
	readback := make([]byte, len(dev.Table.Raw))
	if _, err := dev.ReadAt(readback, dev.TableOffset); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitVerifyFailed
	}
	if !bytes.Equal(readback, dev.Table.Raw) {
		fmt.Fprintln(os.Stderr, "error: post-write verification of the table failed")
		return exitVerifyFailed
	}
	fmt.Printf("table checksum recalculated: 0x%08x\n", dev.Table.Header.Checksum)
	return exitOK
}

func doBackup(dev *verona.Device, file string, dryRun bool) int {
	if dryRun {
		fmt.Printf("would back up %d bytes of table to %s\n", dev.Table.Header.Paddings, file)
		return exitOK
	}
	raw := make([]byte, dev.Table.Header.Paddings)
	if _, err := dev.ReadAt(raw, dev.TableOffset); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if err := os.WriteFile(file, raw, 0644); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}
	fmt.Printf("backed up table (%d bytes) to %s\n", len(raw), file)
	return exitOK
}

func doRestore(dev *verona.Device, file string, dryRun bool) int {
	raw, err := os.ReadFile(file)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	hdr, err := verona.ParseHeader(raw)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	need := verona.HeaderSize + int(hdr.PartListLen) + int(hdr.FwListLen)
	if len(raw) < need {
		fmt.Fprintf(os.Stderr, "error: backup file %s is truncated\n", file)
		return exitTableInvalid
	}
	table, err := verona.Parse(raw[:need])
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if err := table.VerifyChecksum(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	bound, err := dev.TableRegionBoundary()
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if uint64(hdr.Paddings) > bound {
		fmt.Fprintf(os.Stderr, "error: backup table size %d bytes exceeds the %d bytes available for the table region on this device\n", hdr.Paddings, bound)
		return exitTableInvalid
	}

	if dryRun {
		fmt.Printf("would restore %d bytes from %s onto the table at offset %d\n", hdr.Paddings, file, dev.TableOffset)
		return exitOK
	}

	full := raw
	if int64(len(raw)) < int64(hdr.Paddings) {
		full = append(raw, make([]byte, int64(hdr.Paddings)-int64(len(raw)))...)
	} else {
		full = raw[:hdr.Paddings]
	}
	if _, err := dev.WriteAt(full, dev.TableOffset); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitTableInvalid
	}
	if err := dev.Sync(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitVerifyFailed
	}
	readback := make([]byte, len(full))
	if _, err := dev.ReadAt(readback, dev.TableOffset); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitVerifyFailed
	}
	if !bytes.Equal(readback, full) {
		fmt.Fprintln(os.Stderr, "error: post-restore verification failed")
		return exitVerifyFailed
	}
	fmt.Printf("restored table (%d bytes) from %s\n", len(full), file)
	return exitOK
}

func printTable(dev *verona.Device) {
	h := dev.Table.Header
	fmt.Printf("Signature: %s  Checksum: 0x%08x  Version: %d  Size: %d\n", h.Signature, h.Checksum, h.Version, h.Paddings)
	fmt.Printf("Partitions (%d):\n", len(dev.Table.Parts))
	for i, p := range dev.Table.Parts {
		fmt.Printf("  %2d: type=%d ro=%t length=%d fwCount=%d fsType=%d emmcPartID=%d mount=%q\n",
			i, p.Type, p.ReadOnly(), p.Length, p.FwCount, p.FwType, p.EmmcPartID, p.MountPointString())
	}
	fmt.Printf("Firmware entries (%d):\n", len(dev.Table.Fws))
	for i, f := range dev.Table.Fws {
		fmt.Printf("  %2d: %-22s ro=%t lzma=%t ver=%d target=0x%08x offset=0x%08x length=%d paddings=%d checksum=0x%08x\n",
			i, f.FwType(), f.ReadOnly(), f.Lzma(), f.Version, f.TargetAddress, f.Offset, f.Length, f.Paddings, f.Checksum)
	}
}
