// Arize omp tracing hook (shim).
//
// OMP loads this extension in-process in Bun. The shim is intentionally a dumb
// bridge: it forwards four authoritative lifecycle payloads to arize-hook-omp;
// all state, span construction, privacy handling, and token math stay in Python.
//
// OMP awaits async extension handlers in registration order. We therefore await
// each short-lived Python dispatcher instead of detaching it, preserving event
// completion and per-session ordering. Python exports spans in its own detached
// fail-soft child, so this wait covers state mutation rather than OTLP latency.
//
// Forwarded payload contract:
//   { type, sessionId, ...eventFields }

import { spawn } from "node:child_process";
import { homedir, platform } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const FORWARD_TIMEOUT_MS = 1500;

function binaryPath(): string {
  const base = join(homedir(), ".arize", "harness", "venv");
  return platform() === "win32"
    ? join(base, "Scripts", "arize-hook-omp.exe")
    : join(base, "bin", "arize-hook-omp");
}

function forward(payload: unknown): Promise<void> {
  return new Promise((resolve) => {
    try {
      const child = spawn(binaryPath(), [], {
        stdio: ["pipe", "ignore", "ignore"],
      });
      let settled = false;
      const finish = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      };
      const stop = (): void => {
        try {
          child.kill();
        } catch {
          /* already exited */
        }
        finish();
      };
      const timer = setTimeout(stop, FORWARD_TIMEOUT_MS);

      child.once("error", finish);
      child.once("close", finish);
      child.stdin?.on("error", stop);
      try {
        child.stdin?.end(JSON.stringify(payload));
      } catch {
        stop();
      }
    } catch {
      resolve();
    }
  });
}

// session_shutdown has an empty event, so stamp every payload from context.
function sessionIdOf(ctx: ExtensionContext): string {
  try {
    return ctx.sessionManager.getSessionId() || "";
  } catch {
    return "";
  }
}

export default function (pi: ExtensionAPI): void {
  pi.on("before_agent_start", async (event, ctx) => {
    try {
      await forward({ type: "before_agent_start", sessionId: sessionIdOf(ctx), prompt: event.prompt });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("turn_end", async (event, ctx) => {
    try {
      await forward({
        type: "turn_end",
        sessionId: sessionIdOf(ctx),
        turnIndex: event.turnIndex,
        message: event.message,
        toolResults: event.toolResults,
      });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("agent_end", async (event, ctx) => {
    try {
      await forward({
        type: "agent_end",
        sessionId: sessionIdOf(ctx),
        messages: event.messages,
        willContinue: event.willContinue,
      });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    try {
      await forward({ type: "session_shutdown", sessionId: sessionIdOf(ctx) });
    } catch {
      /* fail-soft */
    }
  });
}
