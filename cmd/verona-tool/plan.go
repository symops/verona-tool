package main

import (
	"fmt"
	"os"
	"time"
)

// planError carries the specific exit code a plan-building error maps to.
// Most mistakes caught in buildPlan are plain usage errors (exitArgs), but
// a slot conflict must report exitConflict per SPEC.md's table, not fall
// back to the generic exitArgs like everything else here.
type planError struct {
	code int
	err  error
}

func (e *planError) Error() string { return e.err.Error() }

func conflictErrorf(format string, args ...interface{}) error {
	return &planError{code: exitConflict, err: fmt.Errorf(format, args...)}
}

type writeOp struct {
	slot int
	file string
	// group, if >=0, is an index into plan.atomicGroups identifying the
	// shortcut pair this write belongs to (for the atomic both-or-neither
	// boundary check). -1 means a plain, standalone -w.
	group int
}

type readOp struct {
	slot int
	file string // already defaulted to "./N.img" if not given
}

type plan struct {
	platform string
	device   string
	dryRun   bool
	debug    bool

	doBackup   bool
	backupFile string

	doList    bool
	doRestore string // "" if not restoring

	reads  []readOp
	writes []writeOp

	// atomicGroups[i] is the list of slot indices (always length 2 for our
	// shortcuts) that must be checked together before either is written.
	atomicGroups [][]int

	warnings []string
}

func buildPlan(a *args) (*plan, error) {
	p := &plan{platform: a.platform, device: a.device, dryRun: a.dryRun, debug: a.debug}

	if a.platform == "" {
		return nil, fmt.Errorf("-p/--platform is required")
	}

	exclusiveCount := 0
	if a.list {
		exclusiveCount++
	}
	if a.restore != "" {
		exclusiveCount++
	}
	hasOrdinary := len(a.reads) > 0 || len(a.writes) > 0 || a.backup || len(a.shortcutsSeen) > 0
	if exclusiveCount > 0 && hasOrdinary {
		return nil, fmt.Errorf("-l/--list and -R/--restore cannot be combined with -r/-w/-k/-t/-f/-B")
	}
	if a.list && a.restore != "" {
		return nil, fmt.Errorf("-l/--list and -R/--restore cannot be combined with each other")
	}

	if a.list {
		p.doList = true
		return p, nil
	}
	if a.restore != "" {
		p.doRestore = a.restore
		return p, nil
	}

	// Ordinary mode: -b (optional) + -r/-w (incl. shortcuts), at least one
	// of which must be present.
	if !hasOrdinary {
		return nil, fmt.Errorf("at least one of -r/-w/-B/-l/-R is required")
	}

	if a.backup {
		p.doBackup = true
		if a.backupFileSet {
			p.backupFile = a.backupFile
		} else {
			p.backupFile = fmt.Sprintf("verona-table-backup-%s-%s.img", a.platform, time.Now().Format("20060102150405"))
		}
	}

	// Expand explicit reads/writes first.
	for _, r := range a.reads {
		file := r.file
		if file == "" {
			file = fmt.Sprintf("%d.img", r.slot)
		}
		p.reads = append(p.reads, readOp{r.slot, file})
	}
	for _, w := range a.writes {
		p.writes = append(p.writes, writeOp{w.slot, w.file, -1})
	}

	// Expand shortcuts. Each shortcut's mode is determined by whether it
	// carries an attached write file.
	for _, s := range a.shortcutsSeen {
		file, isWrite := a.shortcutWriteFile[s]
		slots := s.slots()
		if isWrite {
			if !a.writeMode {
				return nil, fmt.Errorf("%s=%s given but -w/--write was not present to select write mode", s, file)
			}
			group := len(p.atomicGroups)
			p.atomicGroups = append(p.atomicGroups, []int{slots[0], slots[1]})
			p.writes = append(p.writes, writeOp{slots[0], file, group})
			p.writes = append(p.writes, writeOp{slots[1], file, group})
		} else {
			if !a.readMode {
				return nil, fmt.Errorf("%s requires -r or -w to select read or write mode", s)
			}
			p.reads = append(p.reads, readOp{slots[0], fmt.Sprintf("%d.img", slots[0])})
			p.reads = append(p.reads, readOp{slots[1], fmt.Sprintf("%d.img", slots[1])})
		}
	}

	if a.readMode && len(p.reads) == 0 {
		return nil, fmt.Errorf("-r/--read given but no slot number or shortcut was specified")
	}
	if a.writeMode && len(p.writes) == 0 {
		return nil, fmt.Errorf("-w/--write given but no SLOT:FILE or shortcut was specified")
	}

	// Conflict detection on writes: any slot index targeted more than once
	// (after shortcut expansion) is an error.
	seen := map[int]bool{}
	for _, w := range p.writes {
		if seen[w.slot] {
			return nil, conflictErrorf("conflict: slot %d is targeted by more than one write (explicit -w and/or a shortcut)", w.slot)
		}
		seen[w.slot] = true
	}

	// Duplicate reads: allowed, just warn.
	readSeen := map[int]bool{}
	for _, r := range p.reads {
		if readSeen[r.slot] {
			p.warnings = append(p.warnings, fmt.Sprintf("warning: slot %d is read more than once in this invocation", r.slot))
		}
		readSeen[r.slot] = true
	}

	// Output-path collision warning across all read outputs + backup file.
	paths := map[string][]string{}
	for _, r := range p.reads {
		paths[r.file] = append(paths[r.file], fmt.Sprintf("read of slot %d", r.slot))
	}
	if p.doBackup {
		paths[p.backupFile] = append(paths[p.backupFile], "table backup")
	}
	for path, uses := range paths {
		if len(uses) > 1 {
			p.warnings = append(p.warnings, fmt.Sprintf("warning: output path %q is used by more than one operation: %v", path, uses))
		}
	}

	return p, nil
}

func printWarnings(p *plan) {
	for _, w := range p.warnings {
		fmt.Fprintln(os.Stderr, w)
	}
}
