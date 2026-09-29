// Package left pads strings on the left.
package left

import "strings"

// Pad returns s padded on the left with c up to width runes.
func Pad(s string, width int, c rune) string {
	if n := width - len([]rune(s)); n > 0 {
		return strings.Repeat(string(c), n) + s
	}
	return s
}
