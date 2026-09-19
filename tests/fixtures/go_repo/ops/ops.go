// Package ops holds the trivial functions the Go harness fixture exercises.
package ops

import "strings"

// Add returns the sum of a and b.
func Add(a, b int) int {
	return a + b
}

// Subtract returns a minus b.
func Subtract(a, b int) int {
	return a - b
}

// Slugify lowercases s and joins its whitespace-separated fields with "-".
func Slugify(s string) string {
	return strings.Join(strings.Fields(strings.ToLower(s)), "-")
}
