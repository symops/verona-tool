package main

import (
	"reflect"
	"strings"
	"testing"
)

// The short shortcut spellings -k/-t/-f with an attached FILE ("=" or ":")
// must parse exactly like the long --kernel/--dtb/--rootfs=FILE forms.
func TestShortShortcutValueEquivalentToLong(t *testing.T) {
	cases := []struct {
		short []string
		long  []string
	}{
		{[]string{"-w", "-t=rescue.dtb"}, []string{"-w", "--dtb=rescue.dtb"}},
		{[]string{"-w", "-t:rescue.dtb"}, []string{"-w", "--dtb=rescue.dtb"}},
		{[]string{"-w", "-k=emmc.uImage"}, []string{"-w", "--kernel=emmc.uImage"}},
		{[]string{"-w", "-k:emmc.uImage"}, []string{"-w", "--kernel=emmc.uImage"}},
		{[]string{"-w", "-f=root.img"}, []string{"-w", "--rootfs=root.img"}},
		{[]string{"-w", "-f:root.img"}, []string{"-w", "--rootfs=root.img"}},
		{[]string{"-w", "--write", "-t:a"}, []string{"-w", "--write", "--dtb=a"}},
		// value keeps further '=' / ':' characters
		{[]string{"-w", "-k=a:b=c"}, []string{"-w", "--kernel=a:b=c"}},
		{[]string{"-w", "-f:dir/a:b"}, []string{"-w", "--rootfs=dir/a:b"}},
		// several writes, mixed spellings
		{
			[]string{"-p", "p", "-d", "/dev/x", "-w", "-k=a", "-w", "-t:b", "-w", "--rootfs=c"},
			[]string{"-p", "p", "-d", "/dev/x", "-w", "--kernel=a", "-w", "--dtb=b", "-w", "--rootfs=c"},
		},
		// bare shortcut letters are unchanged (read mode)
		{[]string{"-r", "-t"}, []string{"-r", "--dtb"}},
		{[]string{"-r", "-k", "-r", "-f"}, []string{"-r", "--kernel", "-r", "--rootfs"}},
		// an empty attached value behaves the same as the long form's
		{[]string{"-w", "-t="}, []string{"-w", "--dtb="}},
		{[]string{"-w", "-t:"}, []string{"-w", "--dtb="}},
	}
	for _, c := range cases {
		got, err1 := parseArgs(c.short)
		want, err2 := parseArgs(c.long)
		if (err1 == nil) != (err2 == nil) {
			t.Errorf("%v: err=%v, but %v: err=%v", c.short, err1, c.long, err2)
			continue
		}
		if err1 != nil {
			if err1.Error() != err2.Error() {
				t.Errorf("%v: error %q != %q", c.short, err1, err2)
			}
			continue
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%v parsed differently from %v:\n got  %+v\n want %+v", c.short, c.long, got, want)
		}
	}
}

func TestShortShortcutValueSetsWriteFile(t *testing.T) {
	a, err := parseArgs([]string{"-w", "-t:rescue.dtb", "-w", "-k=a:b"})
	if err != nil {
		t.Fatal(err)
	}
	if got := a.shortcutWriteFile[shortcutDTB]; got != "rescue.dtb" {
		t.Errorf("dtb file = %q, want rescue.dtb", got)
	}
	if got := a.shortcutWriteFile[shortcutKernel]; got != "a:b" {
		t.Errorf("kernel file = %q, want a:b", got)
	}
}

// A shortcut with an attached value still has to be preceded by its own
// bare -r/-w, exactly like the long form.
func TestShortShortcutValueStillNeedsBareFlag(t *testing.T) {
	for _, argv := range [][]string{
		{"-t=x"},
		{"-k:x"},
		{"-p", "p", "-t=x"},
		{"-w", "-k=a", "-t=b"}, // one -w cannot cover two shortcuts
		{"-w", "5:f", "-t=b"},  // a valued -w is not a bare -w
	} {
		if _, err := parseArgs(argv); err == nil {
			t.Errorf("%v: expected an error", argv)
		}
	}
}

// Only -k/-t/-f gain the attached-value spelling; every other short flag
// keeps rejecting "-x=value" / "-x:value".
func TestOtherShortFlagsUnchanged(t *testing.T) {
	for _, tok := range []string{"-p=p", "-p:p", "-d=/dev/x", "-d:/dev/x", "-R=f", "-B=f", "-n=1", "-l=1", "-r=5", "-w=5:f"} {
		_, err := parseArgs([]string{tok})
		if err == nil {
			t.Errorf("%s: expected an error, got none", tok)
			continue
		}
		if !strings.Contains(err.Error(), "unknown flag") {
			t.Errorf("%s: error %q should be \"unknown flag\"", tok, err)
		}
	}
}
