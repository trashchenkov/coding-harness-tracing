package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/doctor"
)

// TestRunDoctor_FailingChecksReturnExitCodeError verifies that failing health
// checks surface as a returned *exitCodeError{code: 3} rather than calling
// os.Exit. main() relies on this contract to set the process status, and the
// returnable form is what makes runDoctor testable at all.
func TestRunDoctor_FailingChecksReturnExitCodeError(t *testing.T) {
	// OTLP probe gets a healthy response so the only failures come from the
	// empty HOME (missing venv/settings) — keeping the test hermetic.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	err := runDoctor(context.Background(), doctor.Options{
		HomeDir:      t.TempDir(), // empty home → venv/settings checks fail
		HTTPClient:   srv.Client(),
		OTLPEndpoint: srv.URL,
	})
	if err == nil {
		t.Fatal("expected an error from failing doctor checks, got nil")
	}

	var ec *exitCodeError
	if !errors.As(err, &ec) {
		t.Fatalf("expected *exitCodeError, got %T: %v", err, err)
	}
	if ec.code != 3 {
		t.Errorf("exit code = %d, want 3", ec.code)
	}
	if got, want := ec.Error(), "exited with code 3"; got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}
}
