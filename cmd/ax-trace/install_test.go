package main

import (
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestInstallCommandsRegistered(t *testing.T) {
	want := []string{"claude", "codex", "copilot", "cursor", "gemini", "kiro"}
	got := map[string]bool{}
	for _, cmd := range rootCmd.Commands() {
		got[cmd.Use] = true
	}
	for _, w := range want {
		if !got[w] {
			t.Errorf("expected install command %q registered, available: %v", w, strings.Join(commandNames(rootCmd), ","))
		}
	}
}

func commandNames(c *cobra.Command) []string {
	out := []string{}
	for _, sub := range c.Commands() {
		out = append(out, sub.Use)
	}
	return out
}
