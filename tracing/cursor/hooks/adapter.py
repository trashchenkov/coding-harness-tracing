#!/usr/bin/env python3
"""Cursor-specific adapter: deterministic trace IDs, state stack, sanitization.

Cursor is architecturally different from Claude Code and Codex — it uses a
single dispatcher for all 12 hook events, deterministic trace IDs from
generation IDs, and a disk-backed state stack for merging before/after hook
pairs.

Replaces cursor-tracing/hooks/common.sh (195 lines).
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager

from core.common import FileLock, env, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants from HARNESSES["cursor"] ---
_HARNESS = HARNESSES["cursor"]
SERVICE_NAME = _HARNESS["service_name"]  # "cursor"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-cursor-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/cursor
MAX_ATTR_CHARS = int(os.environ.get("CURSOR_TRACE_MAX_ATTR_CHARS", "100000"))
GENERATION_GUARD_TIMEOUT_SECONDS = 30.0
COMPLETION_DB_TIMEOUT_SECONDS = 30.0
# Fixed upper bound: once reached, tracing stays fail-closed rather than
# evicting provenance or allowing the database to grow without limit.
COMPLETION_LEDGER_MAX_ROWS = 100_000
PENDING_CLEANUP_BATCH_SIZE = 256


def _ensure_private_dir(path) -> None:
    """Create or tighten a directory that stores private tracing state."""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _ensure_private_file(path) -> None:
    """Create a private file without an umask-dependent exposure window."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)


def _write_private_text(path, text: str) -> None:
    """Crash-safely replace text using a descriptor created with mode 0600."""
    _ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = path.parent / os.path.basename(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def _stable_bytes(value: str) -> bytes:
    """Encode even JSON-valid lone surrogates deterministically."""
    return value.encode("utf-8", errors="surrogatepass")


def trace_id_from_generation(gen_id: str) -> str:
    """Deterministic 32-hex trace ID from a Cursor generation_id.

    Maps one Cursor "turn" (generation) to one trace.
    Uses MD5 hash — matches bash: printf '%s' "$gen_id" | md5sum | cut -c1-32

    MD5 is NOT used for security here — it's used for deterministic mapping
    so all spans in the same generation share a trace_id.
    """
    return hashlib.md5(_stable_bytes(gen_id)).hexdigest()[:32]


def span_id_16() -> str:
    """Generate 16-hex random span ID.

    Replaces bash: od -An -tx1 -N8 /dev/urandom | tr -d ' \\n' | cut -c1-16
    """
    return os.urandom(8).hex()


def sanitize(s: str) -> str:
    """Replace non-alphanumeric characters (except ._-) with underscore.

    Matches bash: printf '%s' "$1" | tr -c '[:alnum:]._-' '_'
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def stable_digest(value: str) -> str:
    """Return a stable SHA-256 digest for arbitrary JSON string values."""
    return hashlib.sha256(_stable_bytes(value)).hexdigest()


def _generation_digest(gen_id: str) -> str:
    """Return a content-free stable identifier for generation bookkeeping."""
    return stable_digest(gen_id)


def generation_state_key(gen_id: str) -> str:
    """Return a collision-resistant, content-free state namespace token."""
    return f"g_{_generation_digest(gen_id)}"


def _generation_lock_dir():
    _ensure_private_dir(STATE_DIR)
    lock_dir = STATE_DIR / ".generation_locks"
    _ensure_private_dir(lock_dir)
    return lock_dir


@contextmanager
def generation_guard(gen_id: str):
    """Serialize one generation using a bounded, crash-safe SQLite stripe."""
    stripe = int(_generation_digest(gen_id)[:2], 16) % 64
    lock_db = _generation_lock_dir() / f"stripe_{stripe:02d}.sqlite3"
    _ensure_private_file(lock_db)
    connection = sqlite3.connect(
        lock_db,
        timeout=GENERATION_GUARD_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        try:
            lock_db.chmod(0o600)
        except OSError:
            pass
        # SQLite never takes over a live owner; process death releases the OS lock.
        connection.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


_completion_guard_local = threading.local()


@contextmanager
def completion_ledger_guard():
    """Serialize global ledger transitions with event handling, crash-safely."""
    pid = os.getpid()
    depth = getattr(_completion_guard_local, "depth", 0)
    if depth and getattr(_completion_guard_local, "pid", None) == pid:
        _completion_guard_local.depth = depth + 1
        try:
            yield
        finally:
            _completion_guard_local.depth -= 1
        return

    lock_db = _generation_lock_dir() / "completion_ledger.sqlite3"
    _ensure_private_file(lock_db)
    connection = sqlite3.connect(
        lock_db,
        timeout=GENERATION_GUARD_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        try:
            lock_db.chmod(0o600)
        except OSError:
            pass
        connection.execute("BEGIN EXCLUSIVE")
        _completion_guard_local.pid = pid
        _completion_guard_local.depth = 1
        try:
            yield
        finally:
            _completion_guard_local.depth = 0
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _completed_db_path():
    return STATE_DIR / "completed_generations.sqlite3"


def _read_completion_metadata(connection):
    row = connection.execute(
        "SELECT singleton, row_count, saturated " "FROM completion_metadata WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    singleton, row_count, saturated = row
    if singleton != 1 or not isinstance(row_count, int) or row_count < 0 or saturated not in (0, 1):
        raise sqlite3.DatabaseError("invalid completion ledger metadata")
    if row_count >= COMPLETION_LEDGER_MAX_ROWS and not saturated:
        raise sqlite3.DatabaseError("inconsistent completion ledger saturation")
    return row_count, saturated


def _install_digest_guards(connection) -> None:
    """Validate legacy rows unless exact application-owned guards are installed."""
    guards = {
        "completed_digest_insert_guard": ("completed_generations", "INSERT"),
        "completed_digest_update_guard": ("completed_generations", "UPDATE"),
        "pending_digest_insert_guard": ("pending_generation_cleanups", "INSERT"),
        "pending_digest_update_guard": ("pending_generation_cleanups", "UPDATE"),
    }

    def trigger_sql(name: str, table: str, operation: str) -> str:
        return (
            f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
            "WHEN NEW.digest IS NULL OR length(NEW.digest) != 64 "
            "OR NEW.digest GLOB '*[^0-9a-f]*' "
            "BEGIN SELECT RAISE(ABORT, 'invalid generation digest'); END"
        )

    installed = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall())
    exact_guards = all(
        installed.get(name) == trigger_sql(name, table, operation) for name, (table, operation) in guards.items()
    )
    if exact_guards:
        return

    # A missing or same-name/different-definition trigger is not provenance.
    # Validate all legacy rows before replacing guards so corruption fails closed.
    for table in ("completed_generations", "pending_generation_cleanups"):
        invalid = connection.execute(
            f"SELECT 1 FROM {table} "
            "WHERE digest IS NULL OR length(digest) != 64 "
            "OR digest GLOB '*[^0-9a-f]*' LIMIT 1"
        ).fetchone()
        if invalid is not None:
            raise sqlite3.DatabaseError(f"invalid digest in {table}")
    for name, (table, operation) in guards.items():
        if name in installed:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(trigger_sql(name, table, operation))


@contextmanager
def _completed_db_connect():
    _ensure_private_dir(STATE_DIR)
    ledger = _completed_db_path()
    _ensure_private_file(ledger)
    connection = sqlite3.connect(ledger, timeout=COMPLETION_DB_TIMEOUT_SECONDS)
    try:
        needs_reconciliation = False
        connection.execute("BEGIN")
        try:
            metadata = _read_completion_metadata(connection)
            if metadata is None:
                needs_reconciliation = True
            else:
                actual_count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
                connection.execute("SELECT count(*) FROM pending_generation_cleanups").fetchone()
                needs_reconciliation = actual_count != metadata[0]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            needs_reconciliation = True
        finally:
            connection.rollback()

        if needs_reconciliation:
            # DDL and metadata reconciliation share one crash-atomic transaction.
            # BEGIN IMMEDIATE also serializes concurrent first-use repair.
            connection.execute("BEGIN IMMEDIATE")
            pending_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' " "AND name = 'pending_generation_cleanups'"
                ).fetchone()
                is not None
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS completed_generations "
                "(digest TEXT NOT NULL PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_generation_cleanups "
                "(digest TEXT NOT NULL PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'), "
                "FOREIGN KEY(digest) REFERENCES completed_generations(digest))"
            )
            if not pending_table_exists:
                # Upgrade or recovery cannot prove which completed generations
                # still have state. Conservatively queue every tombstone.
                connection.execute(
                    "INSERT OR IGNORE INTO pending_generation_cleanups(digest) "
                    "SELECT digest FROM completed_generations"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS completion_metadata ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "row_count INTEGER NOT NULL CHECK(row_count >= 0), "
                "saturated INTEGER NOT NULL CHECK(saturated IN (0, 1)))"
            )
            metadata = _read_completion_metadata(connection)
            actual_count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
            if metadata is None:
                saturated = int(actual_count >= COMPLETION_LEDGER_MAX_ROWS)
            else:
                recorded_count, recorded_saturated = metadata
                # If rows disappeared, lost tombstones cannot be reconstructed.
                # Keep the actual count but globally fail closed from now on.
                saturated = int(
                    recorded_saturated or actual_count < recorded_count or actual_count >= COMPLETION_LEDGER_MAX_ROWS
                )
            connection.execute(
                "INSERT INTO completion_metadata(singleton, row_count, saturated) VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET row_count = excluded.row_count, "
                "saturated = excluded.saturated",
                (actual_count, saturated),
            )
            connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        _install_digest_guards(connection)
        connection.commit()
        try:
            ledger.chmod(0o600)
        except OSError:
            pass
        yield connection
        connection.commit()
    finally:
        connection.close()


def generation_mark_completed(gen_id: str, *, cleanup_pending: bool = False) -> None:
    """Durably retain completion and optional hashed cleanup work, or raise."""
    with completion_ledger_guard():
        with _completed_db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            metadata = _read_completion_metadata(connection)
            if metadata is None:
                raise sqlite3.DatabaseError("completion ledger metadata disappeared")
            row_count, saturated = metadata
            actual_count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
            if actual_count != row_count:
                raise sqlite3.DatabaseError("completion ledger count changed during mark")
            if saturated:
                return
            digest = _generation_digest(gen_id)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO completed_generations(digest) VALUES (?)",
                (digest,),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE completion_metadata "
                    "SET row_count = row_count + 1, "
                    "saturated = CASE WHEN row_count + 1 >= ? THEN 1 ELSE 0 END "
                    "WHERE singleton = 1",
                    (COMPLETION_LEDGER_MAX_ROWS,),
                )
            if cleanup_pending:
                connection.execute(
                    "INSERT OR IGNORE INTO pending_generation_cleanups(digest) VALUES (?)",
                    (digest,),
                )


def generation_pending_cleanup_batch() -> "tuple[list[str], bool] | None":
    """Return one bounded cleanup batch plus whether durable work remains."""
    with completion_ledger_guard():
        if not _completed_db_path().exists():
            return [], False
        try:
            with _completed_db_connect() as connection:
                rows = connection.execute(
                    "SELECT digest FROM pending_generation_cleanups ORDER BY digest LIMIT ?",
                    (PENDING_CLEANUP_BATCH_SIZE + 1,),
                ).fetchall()
                digests = [row[0] for row in rows]
                if any(
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    for digest in digests
                ):
                    raise sqlite3.DatabaseError("invalid pending cleanup digest")
                has_more = len(digests) > PENDING_CLEANUP_BATCH_SIZE
                return digests[:PENDING_CLEANUP_BATCH_SIZE], has_more
        except (OSError, sqlite3.DatabaseError):
            return None


def generation_finish_pending_cleanup_batch(processed: list[str], failed: list[str]) -> None:
    """Atomically delete successful markers from one bounded cleanup batch."""
    failed_set = set(failed)
    if not failed_set <= set(processed):
        raise ValueError("failed cleanup digest was not in processed batch")
    succeeded = [digest for digest in processed if digest not in failed_set]
    with completion_ledger_guard():
        with _completed_db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM pending_generation_cleanups WHERE digest = ?",
                ((digest,) for digest in succeeded),
            )


def generation_mark_cleanup_done(digest: str) -> None:
    """Delete one hashed cleanup marker after exact state removal succeeds."""
    with completion_ledger_guard():
        with _completed_db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM pending_generation_cleanups WHERE digest = ?", (digest,))


def generation_completion_status(gen_id: str) -> str:
    """Return ``active``, ``completed``, or fail-closed ledger state."""
    with completion_ledger_guard():
        if not _completed_db_path().exists():
            return "active"
        try:
            with _completed_db_connect() as connection:
                connection.execute("BEGIN")
                metadata = _read_completion_metadata(connection)
                if metadata is None:
                    raise sqlite3.DatabaseError("completion ledger metadata disappeared")
                row_count, saturated = metadata
                actual_count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
                if actual_count != row_count:
                    raise sqlite3.DatabaseError("completion ledger count changed during lookup")
                if saturated:
                    return "ledger-saturated"
                row = connection.execute(
                    "SELECT 1 FROM completed_generations WHERE digest = ? LIMIT 1",
                    (_generation_digest(gen_id),),
                ).fetchone()
        except (OSError, sqlite3.DatabaseError):
            return "ledger-unavailable"
        return "completed" if row is not None else "active"


def generation_is_completed(gen_id: str) -> bool:
    """Return whether replay protection requires dropping this generation."""
    return generation_completion_status(gen_id) != "active"


def truncate_attr(s: str, max_chars: "int | None" = None) -> str:
    """Truncate string to MAX_ATTR_CHARS (default 100000).

    Matches bash: if [[ ${#str} -gt $max ]]; then printf '%s' "${str:0:$max}"
    """
    limit = max_chars if max_chars is not None else MAX_ATTR_CHARS
    return s[:limit] if len(s) > limit else s


# --- Disk-backed state stack (LIFO) ---
# Replaces bash state_push/state_pop at lines 59-132.
# Used to merge before/after hook pairs (e.g., beforeShellExecution pushes
# command + start time, afterShellExecution pops it to create a merged span).


def state_push(key: str, value: dict) -> None:
    """Push a dict onto a named stack.

    Stack file: STATE_DIR/{key}.stack.json — a JSON list.
    Uses FileLock for concurrent access.

    Matches bash state_push() at lines 59-87.
    """
    _ensure_private_dir(STATE_DIR)
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"
    _ensure_private_file(lock_path)

    with FileLock(lock_path):
        if stack_file.exists():
            stack_file.chmod(0o600)
            try:
                data = json.loads(stack_file.read_text()) or []
            except json.JSONDecodeError:
                data = []
        else:
            data = []

        if not isinstance(data, list):
            data = []

        data.append(value)

        _write_private_text(stack_file, json.dumps(data, indent=2))


def state_pop(key: str) -> "dict | None":
    """Pop the last value from a named stack. Returns None if empty.

    Matches bash state_pop() at lines 91-132.
    """
    _ensure_private_dir(STATE_DIR)
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"
    _ensure_private_file(lock_path)

    with FileLock(lock_path):
        if not stack_file.exists():
            return None
        stack_file.chmod(0o600)

        try:
            data = json.loads(stack_file.read_text()) or []
        except json.JSONDecodeError:
            return None

        if not isinstance(data, list) or len(data) == 0:
            return None

        value = data[-1]
        data = data[:-1]

        if data:
            _write_private_text(stack_file, json.dumps(data, indent=2))
        else:
            stack_file.unlink(missing_ok=True)

    return value if isinstance(value, dict) else None


# --- Root span tracking per generation ---
# Replaces bash lines 138-155.


def gen_root_span_save(gen_id: str, span_id: str) -> None:
    """Save the root span ID under a collision-resistant generation namespace."""
    _ensure_private_dir(STATE_DIR)
    root_file = STATE_DIR / f"root_{generation_state_key(gen_id)}"
    _write_private_text(root_file, span_id)


def gen_root_span_get(gen_id: str) -> str:
    """Get the root span ID for a generation. Returns "" if not found."""
    if not gen_id:
        return ""
    root_file = STATE_DIR / f"root_{generation_state_key(gen_id)}"
    if root_file.exists():
        root_file.chmod(0o600)
        return root_file.read_text().strip()
    return ""


# --- Generation cleanup ---
# Replaces bash state_cleanup_generation() at lines 159-176.


def state_generation_digests_present() -> set[str]:
    """Find hashed generation tokens appearing in current state filenames once."""
    if not STATE_DIR.exists():
        return set()
    present: set[str] = set()
    for path in STATE_DIR.iterdir():
        present.update(re.findall(r"g_([0-9a-f]{64})(?![0-9a-f])", path.name))
    return present


def state_cleanup_generation(gen_id: str) -> None:
    """Remove only state files belonging to the hashed generation namespace."""
    state_cleanup_generation_digest(_generation_digest(gen_id))


def state_cleanup_generation_digest(digest: str) -> None:
    """Remove state for a trusted SHA-256 completion digest without recovering raw IDs."""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid generation digest")
    # Legacy sanitized names are inherently ambiguous (generation and tool IDs
    # shared underscore delimiters), so guessing their owner can delete another
    # active generation. Leave them untouched for explicit offline migration.
    token = f"g_{digest}"
    (STATE_DIR / f"root_{token}").unlink(missing_ok=True)
    patterns = (
        f"*_{token}.stack.json",
        f"*_{token}_*.stack.json",
        f".lock_*_{token}",
        f".lock_*_{token}_*",
    )
    for pattern in patterns:
        for path in STATE_DIR.glob(pattern):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
            else:
                path.unlink(missing_ok=True)


# --- Requirements check ---


def check_requirements() -> bool:
    """Check tracing enabled, ensure state directory exists."""
    if not env.trace_enabled:
        return False
    _ensure_private_dir(STATE_DIR)
    return True
