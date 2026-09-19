package ops

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(2, 3); got != 5 {
		t.Errorf("Add(2, 3) = %d, want 5", got)
	}
}

func TestSubtract(t *testing.T) {
	if got := Subtract(5, 3); got != 2 {
		t.Errorf("Subtract(5, 3) = %d, want 2", got)
	}
	if got := Subtract(0, 4); got != -4 {
		t.Errorf("Subtract(0, 4) = %d, want -4", got)
	}
}

func TestSlugify(t *testing.T) {
	if got := Slugify("Hello World"); got != "hello-world" {
		t.Errorf("Slugify(%q) = %q, want %q", "Hello World", got, "hello-world")
	}
	if got := Slugify("  Mixed   Case  Text "); got != "mixed-case-text" {
		t.Errorf("Slugify(%q) = %q, want %q", "  Mixed   Case  Text ", got, "mixed-case-text")
	}
}
