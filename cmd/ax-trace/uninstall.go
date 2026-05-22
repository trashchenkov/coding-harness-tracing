package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	axexec "github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/exec"
	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/paths"
)

func init() {
	cmd := &cobra.Command{
		Use:   "uninstall [harness]",
		Short: "Uninstall one harness, or all harnesses + the shared runtime",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return runUninstallAll(cmd.Context())
			}
			return runUninstallOne(cmd.Context(), args[0])
		},
	}
	rootCmd.AddCommand(cmd)
}

// runUninstallOne tears down a single harness by exec'ing its install.py with
// the "uninstall" subcommand. If the venv is missing, prints a friendly note
// and exits 0 — there's nothing to clean up.
func runUninstallOne(ctx context.Context, key string) error {
	if !venvExists() {
		fmt.Fprintln(os.Stdout, "[ax-trace] venv not found — nothing to uninstall")
		return nil
	}

	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}
	installPy := filepath.Join(installDir, "tracing", harnessSubdir(key), "install.py")
	if _, statErr := os.Stat(installPy); statErr != nil {
		return fmt.Errorf("unknown harness %q: install script not found at %s", key, installPy)
	}

	fmt.Fprintf(os.Stdout, "[ax-trace] uninstalling %s tracing...\n", key)
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: "python",
		Args:    []string{installPy, "uninstall"},
	})
	if err != nil {
		return fmt.Errorf("uninstalling %s: %w", key, err)
	}
	if exitCode != 0 {
		os.Exit(exitCode)
	}
	return nil
}

// runUninstallAll mirrors install.sh's full-wipe path: tear down each
// installed harness, then run `python -m core.setup.wipe` to remove the
// shared runtime, then delete ax-trace's own state directory.
//
// If the venv is missing, skip directly to deleting the install + ax-trace
// directories.
func runUninstallAll(ctx context.Context) error {
	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}
	axTraceHome, err := paths.AxTraceHome()
	if err != nil {
		return fmt.Errorf("resolving ax-trace home: %w", err)
	}

	if !venvExists() {
		fmt.Fprintln(os.Stdout, "[ax-trace] venv not found — removing install directories")
		if err := os.RemoveAll(installDir); err != nil {
			return fmt.Errorf("removing %s: %w", installDir, err)
		}
		if err := os.RemoveAll(axTraceHome); err != nil {
			return fmt.Errorf("removing %s: %w", axTraceHome, err)
		}
		fmt.Fprintln(os.Stdout, "[ax-trace] uninstall complete.")
		return nil
	}

	harnesses, err := listInstalledHarnesses(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[ax-trace] could not enumerate installed harnesses (continuing): %v\n", err)
	}
	for _, key := range harnesses {
		installPy := filepath.Join(installDir, "tracing", harnessSubdir(key), "install.py")
		if _, statErr := os.Stat(installPy); statErr != nil {
			fmt.Fprintf(os.Stderr, "[ax-trace] harness install script not found for %q (skipping): %v\n", key, statErr)
			continue
		}
		fmt.Fprintf(os.Stdout, "[ax-trace] uninstalling %s tracing...\n", key)
		exitCode, dispatchErr := axexec.Dispatch(ctx, axexec.DispatchOptions{
			BinName: "python",
			Args:    []string{installPy, "uninstall"},
		})
		if dispatchErr != nil {
			fmt.Fprintf(os.Stderr, "[ax-trace] %s uninstall failed (continuing): %v\n", key, dispatchErr)
			continue
		}
		if exitCode != 0 {
			fmt.Fprintf(os.Stderr, "[ax-trace] %s uninstall exited with code %d (continuing)\n", key, exitCode)
		}
	}

	fmt.Fprintln(os.Stdout, "[ax-trace] wiping shared runtime...")
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: "python",
		Args:    []string{"-m", "core.setup.wipe"},
	})
	if err != nil {
		return fmt.Errorf("wiping shared runtime: %w", err)
	}
	// Match install.sh's `set -e` behavior: if wipe fails (or the user
	// declines its confirmation prompt), abort before deleting ax-trace's own
	// state. This leaves breadcrumbs (bootstrap log, lock file) for debugging
	// a partial uninstall.
	if exitCode != 0 {
		os.Exit(exitCode)
	}

	if err := os.RemoveAll(axTraceHome); err != nil {
		return fmt.Errorf("removing %s: %w", axTraceHome, err)
	}

	fmt.Fprintln(os.Stdout, "[ax-trace] uninstall complete.")
	return nil
}

// venvExists reports whether the venv's python interpreter is present on
// disk. Used to short-circuit uninstall when there's nothing to tear down.
func venvExists() bool {
	pyPath, err := paths.VenvPython()
	if err != nil {
		return false
	}
	_, statErr := os.Stat(pyPath)
	return statErr == nil
}
