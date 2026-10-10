// Command verona-tool reads and writes the VERONA firmware table shared by
// the WD My Cloud Home (Monarch/MCH) and My Cloud Home Duo (Pelican/MCHD).
package main

import (
	"fmt"
	"os"
)

// Exit codes, as agreed in SPEC.md.
const (
	exitOK             = 0
	exitArgs           = 1
	exitTableInvalid   = 2
	exitTooLarge       = 3
	exitSlotRange      = 4
	exitConflict       = 5
	exitVerifyFailed   = 6
	exitLocked         = 7
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(argv []string) int {
	a, err := parseArgs(argv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}

	if a.help || a.version {
		if len(argv) != 1 {
			fmt.Fprintln(os.Stderr, "error: -h/--help and -v/--version must be used alone")
			return exitArgs
		}
		if a.help {
			fmt.Print(helpText)
		} else {
			fmt.Printf("verona-tool %s\nAuthor: Symo <symops@gmail.com>\n", version)
		}
		return exitOK
	}

	// Expand shortcuts into the final slot-level operation plan, validate
	// grammar/conflicts, and run.
	plan, err := buildPlan(a)
	if err != nil {
		if pe, ok := err.(*planError); ok {
			if a.dryRun && pe.code == exitConflict {
				// A conflict is a property of the arguments themselves,
				// not of the device's state -- but per spec, --dry-run
				// still only ever reports it as text and exits 0, the
				// same as an overflow refusal it would otherwise detect
				// once real execution started.
				fmt.Fprintln(os.Stderr, "would fail:", err, "(exit code would be", pe.code, ")")
				return exitOK
			}
			fmt.Fprintln(os.Stderr, "error:", err)
			return pe.code
		}
		fmt.Fprintln(os.Stderr, "error:", err)
		return exitArgs
	}

	return execute(plan)
}
