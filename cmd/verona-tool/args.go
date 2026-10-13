package main

import (
	"fmt"
	"strconv"
	"strings"
)

const version = "1.0"

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

	// Flags that aren't meant to be repeated (unlike -r/-w and the
	// shortcuts, which are legitimately repeatable, or -h/--help and
	// -v/--version, which are already caught by main.go's "must be used
	// alone" check against argv's total length). A second occurrence of
	// any of these -- even with an identical value -- is a usage error
	// rather than silently keeping the last (or first) one.
	seen := map[string]bool{}
	checkOnce := func(key, label string) error {
		if seen[key] {
			return fmt.Errorf("%s given more than once", label)
		}
		seen[key] = true
		return nil
	}

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
		case "n", "dry-run":
			if err := checkOnce("dry-run", "-n/--dry-run"); err != nil {
				return nil, err
			}
			a.dryRun = true
		case "D", "debug":
			if err := checkOnce("debug", "-D/--debug"); err != nil {
				return nil, err
			}
			a.debug = true
		case "l", "list":
			if err := checkOnce("list", "-l/--list"); err != nil {
				return nil, err
			}
			a.list = true
		case "p", "platform":
			if err := checkOnce("platform", "-p/--platform"); err != nil {
				return nil, err
			}
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-p/--platform")
			if err != nil {
				return nil, err
			}
			a.platform = v
		case "d", "device":
			if err := checkOnce("device", "-d/--device"); err != nil {
				return nil, err
			}
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-d/--device")
			if err != nil {
				return nil, err
			}
			a.device = v
		case "R", "restore":
			if err := checkOnce("restore", "-R/--restore"); err != nil {
				return nil, err
			}
			v, err := attachedOrNext(hasAttached, attached, &i, argv, "-R/--restore")
			if err != nil {
				return nil, err
			}
			a.restore = v
		case "B", "backup":
			if err := checkOnce("backup", "-B/--backup"); err != nil {
				return nil, err
			}
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
			// Optional SLOT[:FILE]: both short (-r) and long (--read)
			// forms accept it either attached ("--read=2") or as a
			// separate following token ("-r 2", "--read 2") -- there's
			// no flag/value ambiguity for a numeric slot the way there
			// is for the shortcuts' optional FILE argument, so both
			// spellings peek the next token identically.
			if hasAttached {
				slot, file, err := splitSlotSep(attached)
				if err != nil {
					return nil, err
				}
				a.reads = append(a.reads, slotFile{slot, file})
			} else if i+1 < len(argv) && !looksLikeFlag(argv[i+1]) {
				i++
				slot, file, err := splitSlotSep(argv[i])
				if err != nil {
					return nil, err
				}
				a.reads = append(a.reads, slotFile{slot, file})
			} else {
				// bare -r/--read: no slot given here -- valid only when
				// immediately followed by a -k/-t/-f shortcut, which
				// supplies the actual slot(s); that's the real-world
				// pattern every legitimate "-r --kernel" invocation
				// uses. NOT immediately followed by a shortcut means
				// this occurrence is dangling and a usage error (this
				// does NOT apply to valued -r, e.g. "-r 5:file", which
				// remains legitimately repeatable, including appearing
				// more than once).
				if !nextIsShortcut(argv, i) {
					return nil, fmt.Errorf("-r/--read with no slot must be immediately followed by a -k/-t/-f shortcut")
				}
			}
		case "w", "write":
			a.writeMode = true
			var tokv string
			var got bool
			if hasAttached {
				tokv, got = attached, true
			} else if i+1 < len(argv) && !looksLikeFlag(argv[i+1]) {
				// A following token that doesn't itself look like a flag
				// is unambiguously this -w/--write's SLOT[:FILE] value --
				// true for both spellings alike. (A following token that
				// DOES look like a flag, e.g. a shortcut's "--kernel=
				// file", is deliberately NOT consumed here -- it must
				// not be swallowed as a malformed SLOT:FILE value.)
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
			} else {
				// bare -w/--write: no SLOT:FILE given here -- valid
				// only when immediately followed by a -k/-t/-f
				// shortcut (its write-mode "=FILE" attachment
				// supplies the actual slot(s) and file); same
				// reasoning as bare -r above. Not immediately
				// followed by a shortcut means this occurrence is
				// dangling and a usage error (valued -w, e.g.
				// "-w 5:file", remains legitimately repeatable).
				if !nextIsShortcut(argv, i) {
					return nil, fmt.Errorf("-w/--write with no SLOT:FILE must be immediately followed by a -k/-t/-f shortcut")
				}
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

// flagName extracts the canonical name from a "-x", "--long" or
// "--long=value" token, stripping any attached value. Returns "" for a
// token that isn't a flag at all.
func flagName(tok string) string {
	if strings.HasPrefix(tok, "--") {
		body := tok[2:]
		if eq := strings.IndexByte(body, '='); eq != -1 {
			return body[:eq]
		}
		return body
	}
	if strings.HasPrefix(tok, "-") && len(tok) > 1 {
		return tok[1:]
	}
	return ""
}

// nextIsShortcut reports whether argv[i+1] is one of the -k/-t/-f
// shortcut flags (short or long form). A bare -r/-w immediately
// followed by a shortcut IS giving that occurrence meaning (the
// shortcut supplies the slot(s)) -- this is the real-world pattern
// every legitimate "-r --kernel"/"-w --kernel=file" invocation uses,
// and the basis for distinguishing a satisfied bare -r/-w from a
// genuinely dangling one.
func nextIsShortcut(argv []string, i int) bool {
	if i+1 >= len(argv) {
		return false
	}
	switch flagName(argv[i+1]) {
	case "k", "kernel", "t", "dtb", "f", "rootfs":
		return true
	}
	return false
}
