package version

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func setHome(t *testing.T, dir string) {
	t.Helper()
	t.Setenv("HOME", dir)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", dir)
	}
}

// writeDistInfo creates a fake venv site-packages tree containing a single
// coding_harness_tracing dist-info with the given METADATA contents.
func writeDistInfo(t *testing.T, home, version string) string {
	t.Helper()

	var siteDir string
	if runtime.GOOS == "windows" {
		siteDir = filepath.Join(home, ".arize", "harness", "venv", "Lib", "site-packages")
	} else {
		siteDir = filepath.Join(home, ".arize", "harness", "venv", "lib", "python3.11", "site-packages")
	}

	distDir := filepath.Join(siteDir, "coding_harness_tracing-"+version+".dist-info")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatalf("mkdir dist-info: %v", err)
	}

	metadata := "Metadata-Version: 2.1\n" +
		"Name: coding-harness-tracing\n" +
		"Version: " + version + "\n" +
		"Summary: test fixture\n"
	if err := os.WriteFile(filepath.Join(distDir, "METADATA"), []byte(metadata), 0o644); err != nil {
		t.Fatalf("write METADATA: %v", err)
	}
	return distDir
}

func TestFindPackageVersion_Success(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	writeDistInfo(t, tmp, "0.1.0")

	got, err := FindPackageVersion()
	if err != nil {
		t.Fatalf("FindPackageVersion() error = %v", err)
	}
	if got != "0.1.0" {
		t.Errorf("FindPackageVersion() = %q, want %q", got, "0.1.0")
	}
}

func TestFindPackageVersion_Missing(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	if _, err := FindPackageVersion(); err == nil {
		t.Errorf("FindPackageVersion() with no venv should fail, got nil error")
	}
}

func TestGather_Sentinels(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	info := Gather("v1.2.3", "abcdef0")
	if info.PackageVersion != "(not installed)" {
		t.Errorf("PackageVersion = %q, want %q", info.PackageVersion, "(not installed)")
	}
	if info.PythonVersion != "(no venv)" {
		t.Errorf("PythonVersion = %q, want %q", info.PythonVersion, "(no venv)")
	}
}

func TestGather_PropagatesBinaryFields(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	info := Gather("v9.9.9", "deadbeef")
	if info.BinaryVersion != "v9.9.9" {
		t.Errorf("BinaryVersion = %q, want %q", info.BinaryVersion, "v9.9.9")
	}
	if info.BinaryCommit != "deadbeef" {
		t.Errorf("BinaryCommit = %q, want %q", info.BinaryCommit, "deadbeef")
	}
	if info.VenvPath == "" {
		t.Errorf("VenvPath should be set even when venv is absent, got empty string")
	}
}

func TestGather_FindsPackageVersion(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	writeDistInfo(t, tmp, "0.4.2")

	info := Gather("v1.2.3", "abcdef0")
	if info.PackageVersion != "0.4.2" {
		t.Errorf("PackageVersion = %q, want %q", info.PackageVersion, "0.4.2")
	}
}
