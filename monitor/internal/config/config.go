// Package config is the slo-config.yaml contract.
//
// Adding a workflow type to the SLO program must be a config edit, never a code
// change. Everything the monitor and the rule generator need comes from here.
package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type Kind string

const (
	SelfHosted Kind = "self-hosted"
	Cloud      Kind = "cloud"
)

// StuckDetection selects how the monitor identifies non-progressing executions.
//
// TemporalReportedProblems needs Server 1.30+, and on self-hosted it is off
// until system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute is
// set. "auto" probes the cluster at startup rather than assuming.
type StuckDetection string

const (
	StuckAuto             StuckDetection = "auto"
	StuckReportedProblems StuckDetection = "reported_problems"
	StuckFallback         StuckDetection = "fallback"
)

type Deployment struct {
	Kind           Kind           `yaml:"kind"`
	Namespace      string         `yaml:"namespace"`
	Address        string         `yaml:"address"`
	StuckDetection StuckDetection `yaml:"stuck_detection"`
}

type Defaults struct {
	PollInterval       Duration `yaml:"poll_interval"`
	WindowPollInterval Duration `yaml:"window_poll_interval"`
	Buckets            []int    `yaml:"buckets"`
	SLOWindow          Duration `yaml:"slo_window"`
	// Terminal states that count as a closed execution for the SLI.
	// Terminated and Canceled belong here: if terminating a late workflow
	// removed it from the denominator, the SLO would improve as we destroyed
	// the evidence.
	ClosedOverBudgetStatuses []string `yaml:"closed_over_budget_statuses"`
}

type WorkflowType struct {
	Name           string   `yaml:"name"`
	TaskQueue      string   `yaml:"task_queue"`
	Budget         Duration `yaml:"budget"`
	Objective      float64  `yaml:"objective"`
	Owner          string   `yaml:"owner"`
	PhaseAttribute string   `yaml:"phase_attribute,omitempty"`
}

type Config struct {
	Deployment    Deployment     `yaml:"deployment"`
	Defaults      Defaults       `yaml:"defaults"`
	WorkflowTypes []WorkflowType `yaml:"workflow_types"`
}

// MinPollInterval is enforced regardless of config. Visibility is rate-limited,
// hardest on Cloud, and a tight loop across many types is the fastest way to
// get a namespace throttled.
const MinPollInterval = 30 * time.Second

// Load reads and validates a config file.
//
// It returns an error for an unreviewed generated config on purpose — see the
// placeholder checks in Validate. The service must not start against budgets
// nobody has agreed to.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading slo-config: %w", err)
	}
	return Parse(data)
}

func Parse(data []byte) (*Config, error) {
	var c Config
	if err := yaml.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("parsing slo-config: %w", err)
	}
	c.applyDefaults()
	return &c, c.Validate()
}

func (c *Config) applyDefaults() {
	if c.Defaults.PollInterval == 0 {
		c.Defaults.PollInterval = Duration(60 * time.Second)
	}
	if c.Defaults.WindowPollInterval == 0 {
		c.Defaults.WindowPollInterval = Duration(10 * time.Minute)
	}
	if len(c.Defaults.Buckets) == 0 {
		c.Defaults.Buckets = []int{1, 2, 5}
	}
	if c.Defaults.SLOWindow == 0 {
		c.Defaults.SLOWindow = Duration(28 * 24 * time.Hour)
	}
	if len(c.Defaults.ClosedOverBudgetStatuses) == 0 {
		c.Defaults.ClosedOverBudgetStatuses = []string{
			"Completed", "Failed", "Canceled", "Terminated", "TimedOut",
		}
	}
	if c.Deployment.StuckDetection == "" {
		c.Deployment.StuckDetection = StuckAuto
	}
}

func (c *Config) Validate() error {
	switch c.Deployment.Kind {
	case SelfHosted, Cloud:
	default:
		return fmt.Errorf("deployment.kind must be %q or %q, got %q", SelfHosted, Cloud, c.Deployment.Kind)
	}
	switch c.Deployment.StuckDetection {
	case StuckAuto, StuckReportedProblems, StuckFallback:
	default:
		return fmt.Errorf("deployment.stuck_detection must be auto|reported_problems|fallback, got %q",
			c.Deployment.StuckDetection)
	}
	if c.Deployment.Namespace == "" {
		return fmt.Errorf("deployment.namespace is required")
	}
	if c.Defaults.PollInterval.Std() < MinPollInterval {
		return fmt.Errorf("defaults.poll_interval %v is below the %v floor; Visibility is rate-limited",
			c.Defaults.PollInterval.Std(), MinPollInterval)
	}
	if c.Defaults.WindowPollInterval.Std() < c.Defaults.PollInterval.Std() {
		return fmt.Errorf("defaults.window_poll_interval (%v) must be >= poll_interval (%v): the window queries span %v and are far more expensive",
			c.Defaults.WindowPollInterval.Std(), c.Defaults.PollInterval.Std(), c.Defaults.SLOWindow.Std())
	}

	// A bucket ladder without 1x cannot compute the SLI: bucket="1" is the
	// "still running past budget" term in the denominator.
	hasOne := false
	seen := map[int]bool{}
	for _, b := range c.Defaults.Buckets {
		if b < 1 {
			return fmt.Errorf("defaults.buckets must be >= 1, got %d", b)
		}
		if seen[b] {
			return fmt.Errorf("defaults.buckets has duplicate %d", b)
		}
		seen[b] = true
		if b == 1 {
			hasOne = true
		}
	}
	if !hasOne {
		return fmt.Errorf("defaults.buckets must include 1: bucket=\"1\" is the still-running-past-budget term in the SLI denominator")
	}

	if len(c.WorkflowTypes) == 0 {
		return fmt.Errorf("no workflow_types configured")
	}
	names := map[string]bool{}
	for i, w := range c.WorkflowTypes {
		if w.Name == "" {
			return fmt.Errorf("workflow_types[%d].name is required", i)
		}
		if names[w.Name] {
			return fmt.Errorf("workflow_types: duplicate name %q", w.Name)
		}
		names[w.Name] = true
		if w.Budget.Std() <= 0 {
			return fmt.Errorf("workflow_types[%s].budget must be positive", w.Name)
		}
		if w.Objective <= 0 || w.Objective >= 1 {
			return fmt.Errorf("workflow_types[%s].objective must be between 0 and 1 exclusive, got %v", w.Name, w.Objective)
		}
		if w.Owner == "" {
			return fmt.Errorf("workflow_types[%s].owner is required: it routes the alert", w.Name)
		}
		// budget-derive emits placeholders on purpose. Refusing to load them is
		// what makes "review before use" a guarantee rather than a comment
		// nobody read — an unreviewed config must not be able to page anyone.
		if strings.HasPrefix(strings.ToUpper(w.Owner), "TODO") {
			return fmt.Errorf("workflow_types[%s].owner is still the generated placeholder %q: set a real owner before loading this config", w.Name, w.Owner)
		}
		if strings.HasPrefix(strings.ToUpper(string(w.Budget.String())), "REPLACE_ME") {
			return fmt.Errorf("workflow_types[%s] has an unresolved budget placeholder", w.Name)
		}
	}
	return nil
}
