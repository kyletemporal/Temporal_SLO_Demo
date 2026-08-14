package config

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Duration is a time.Duration that accepts day and week suffixes in YAML.
//
// Go's time.ParseDuration tops out at hours — "28d" is a PARSE ERROR, not a
// 28-day duration. But SLO windows are naturally written in days, and the
// config contract for this service specifies `slo_window: 28d`. Left to the
// stock parser, that config fails to load at startup.
//
// So: "d" and "w" are translated to hours before delegating to the standard
// parser, which keeps every other Go duration form working unchanged.
type Duration time.Duration

// Std returns the underlying time.Duration.
func (d Duration) Std() time.Duration { return time.Duration(d) }

func (d Duration) String() string { return time.Duration(d).String() }

// dayWeek matches a leading integer or decimal followed by d or w.
var dayWeek = regexp.MustCompile(`^(\d+(?:\.\d+)?)([dw])$`)

func ParseDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty duration")
	}
	if m := dayWeek.FindStringSubmatch(s); m != nil {
		n, err := strconv.ParseFloat(m[1], 64)
		if err != nil {
			return 0, fmt.Errorf("parsing duration %q: %w", s, err)
		}
		hours := n * 24
		if m[2] == "w" {
			hours *= 7
		}
		return time.Duration(hours * float64(time.Hour)), nil
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("parsing duration %q (use ns/us/ms/s/m/h, or d/w): %w", s, err)
	}
	return v, nil
}

func (d *Duration) UnmarshalYAML(node *yaml.Node) error {
	// Dispatch on the YAML type tag, not on decode success. yaml.v3 will
	// happily decode the scalar `60` into the string "60", so trying string
	// first and falling back on error never reaches the numeric branch.
	switch node.Tag {
	case "!!int", "!!float":
		// A bare number means SECONDS. Decoded as a raw time.Duration it would
		// be 60 NANOSECONDS — a silent 10^9x error that would hammer the
		// Visibility API rather than politely polling it.
		var n float64
		if err := node.Decode(&n); err != nil {
			return err
		}
		*d = Duration(time.Duration(n * float64(time.Second)))
		return nil
	}

	var s string
	if err := node.Decode(&s); err != nil {
		return err
	}
	v, err := ParseDuration(s)
	if err != nil {
		return err
	}
	*d = Duration(v)
	return nil
}

func (d Duration) MarshalYAML() (any, error) { return time.Duration(d).String(), nil }
