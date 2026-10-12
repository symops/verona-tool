package main

import (
	"fmt"
	"strconv"
	"strings"
)

const version = "0.1.0"

// slotFile is one -r/-w SLOT[:FILE] or SLOT=FILE argument.
type slotFile struct {
	slot int
	file string // "" means "not given" (only valid for -r)
}

// shortcut names the -k/-t/-o groups.
type shortcut int

const (
	shortcutNone shortcut = iota
	shortcutKernel
	shortcutDTB
	shortcutRootFS
)

func (s shortcut) slots() [2]int {
	switch s {
	case shortcutKernel:
		return [2]int{5, 12}
	case shortcutDTB:
		return [2]int{7, 6}
	case shortcutRootFS:
		return [2]int{9, 8}
	}
	return [2]int{-1, -1}
}

func (s shortcut) String() string {
	switch s {
	case shortcutKernel:
		return "--kernel"
	case shortcutDTB:
		return "--dtb"
	case shortcutRootFS:
		return "--rootfs"
	}
	return "?"
}

type args struct {
	help    bool
	version bool

	platform string
	device   string

	reads  []slotFile
	writes []slotFile

	// shortcuts used, each optionally carrying a write-mode file (""
	// means read mode / not yet known which mode).
	shortcutsSeen   []shortcut
	shortcutWriteFile map[shortcut]string

	readMode  bool // bare -r present anywhere
	writeMode bool // bare -w present anywhere

	backup        bool
	backupFile    string
	backupFileSet bool

	list    bool
	restore string

	dryRun bool
	debug  bool
}

// splitSlotSep splits "N:file", "N=file" or bare "N" into slot+file. For -r,
// file may be absent (ok==true, file=="").
func splitSlotSep(tok string) (slot int, file string, err error) {
	sep := -1
	for i, c := range tok {
		if c == ':' || c == '=' {
			sep = i
			break
		}
	}
	numPart := tok
	if sep != -1 {
		numPart = tok[:sep]
		file = tok[sep+1:]
	}
	n, perr := strconv.Atoi(numPart)
	if perr != nil {
		return 0, "", fmt.Errorf("invalid slot number %q", numPart)
	}
	return n, file, nil
}

// parseArgs implements the custom grammar: long options only accept
// attached "--flag=value" (never a separate following token for their
// value), matching the agreed spec; short options take the next token as
// their value where a value is required/optional per flag.
func parseArgs(argv []string) (*args, error) {
	a := &args{shortcutWriteFile: map[shortcut]string{}}

	for i := 0; i < len(argv); i++ {
		tok := argv[i]
		var name, attached string
		hasAttached := false
		if strings.HasPrefix(tok, "--") {
			body := tok[2:]
			if eq := strings.IndexByte(body, '='); eq != -1 {
				name, attached, hasAttached = body[:eq], body[eq+1:], true
			} else {
				name = body
			}
		} else if strings.HasPrefix(tok, "-") && len(tok) > 1 {
			name = tok[1:]
		} else {
			return nil, fmt.Errorf("unexpected argument %q", tok)
		}

		switch name {
		case "h", "help":
			a.help = true
		case "v", "version":
			a.version = true
		case "y", "dry-run":
			a.dryRun = true
		case "D", "debug":
			a.debug = true
		case "l", "list":
			a.list = true
		case "p", "platform":
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-p/--platform")
			if err != nil {
				return nil, err
			}
			a.platform = v
		case "d", "device":
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-d/--device")
			if err != nil {
				return nil, err
			}
			a.device = v
		case "R", "restore":
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-R/--restore")
			if err != nil {
				return nil, err
			}
			a.restore = v
		case "B", "backup":
			a.backup = true
			// -B/--backup FILE is optional. Only long form may carry it
			// attached (--backup=file); for the short form we peek at the
			// next token and consume it only if it doesn't itself look
			// like a flag.
			if hasAttached {
				a.backupFile, a.backupFileSet = attached, true
			} else if name == "backup" {
				// bare --backup with no '=': no value.
			} else if i+1 < len(argv) && !looksLikeFlag(argv[i+1]) {
				i++
				a.backupFile, a.backupFileSet = argv[i], true
			}
		case "r", "read":
			a.readMode = true
			// Optional SLOT[:FILE]; short form peeks next token, long form
			// requires attachment if any.
			if hasAttached {
				slot, file, err := splitSlotSep(attached)
				if err != nil {
					return nil, err
				}
				a.reads = append(a.reads, slotFile{slot, file})
			} else if name == "read" {
				// bare --read: no slot given here (must come from a
				// separate -r/--read=N occurrence or a shortcut).
			} else if i+1 < len(argv) && !looksLikeFlag(argv[i+1]) {
				i++
				slot, file, err := splitSlotSep(argv[i])
				if err != nil {
					return nil, err
				}
				a.reads = append(a.reads, slotFile{slot, file})
			}
		case "w", "write":
			a.writeMode = true
			var tokv string
			var got bool
			if hasAttached {
				tokv, got = attached, true
			} else if name != "write" && i+1 < len(argv) && !looksLikeFlag(argv[i+1]) {
				// Bare "-w" immediately followed by a flag (e.g. a
				// shortcut's "--kernel=file") just selects write mode,
				// the same as bare "--write" -- it must NOT swallow that
				// flag as its own malformed SLOT:FILE value.
				i++
				tokv, got = argv[i], true
			}
			if got {
				slot, file, err := splitSlotSep(tokv)
				if err != nil {
					return nil, err
				}
				if file == "" {
					return nil, fmt.Errorf("-w requires SLOT:FILE or SLOT=FILE (filename missing for slot %d)", slot)
				}
				a.writes = append(a.writes, slotFile{slot, file})
			}
		case "k", "kernel":
			if err := a.addShortcut(shortcutKernel, hasAttached, attached); err != nil {
				return nil, err
			}
		case "t", "dtb":
			if err := a.addShortcut(shortcutDTB, hasAttached, attached); err != nil {
				return nil, err
			}
		case "f", "rootfs":
			if err := a.addShortcut(shortcutRootFS, hasAttached, attached); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("unknown flag %q", tok)
		}
	}
	return a, nil
}

func (a *args) addShortcut(s shortcut, hasAttached bool, attached string) error {
	a.shortcutsSeen = append(a.shortcutsSeen, s)
	if hasAttached {
		a.shortcutWriteFile[s] = attached
	}
	return nil
}

func attachedOrNext(hasAttached bool, attached string, i *int, argv []string, flagName string) (string, error) {
	if hasAttached {
		return attached, nil
	}
	*i++
	if *i >= len(argv) {
		return "", fmt.Errorf("missing value after %s", flagName)
	}
	return argv[*i], nil
}

func looksLikeFlag(tok string) bool {
	return strings.HasPrefix(tok, "-")
}
