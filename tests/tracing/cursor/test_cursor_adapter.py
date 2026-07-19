#!/usr/bin/env python3
"""Tests for tracing.cursor.hooks.adapter — Cursor-specific adapter module."""
import hashlib
import json
import sqlite3
import threading

import pytest

from tracing.cursor.hooks import adapter

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a temp directory for every test."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


# ── trace_id_from_generation ──────────────────────────────────────────────


class TestTraceIdFromGeneration:
    def test_returns_32_hex(self):
        result = adapter.trace_id_from_generation("gen-abc")
        assert len(result) == 32
        int(result, 16)  # must be valid hex

    def test_deterministic(self):
        a = adapter.trace_id_from_generation("gen-abc")
        b = adapter.trace_id_from_generation("gen-abc")
        assert a == b

    def test_different_inputs_differ(self):
        a = adapter.trace_id_from_generation("gen-abc")
        b = adapter.trace_id_from_generation("gen-xyz")
        assert a != b

    def test_generation_state_namespace_is_collision_resistant(self):
        adapter.gen_root_span_save("g/a", "span-a")
        adapter.gen_root_span_save("g?a", "span-b")

        assert adapter.gen_root_span_get("g/a") == "span-a"
        assert adapter.gen_root_span_get("g?a") == "span-b"

    def test_generation_state_key_accepts_lone_surrogate(self):
        key = adapter.generation_state_key("generation-\ud800")
        assert key.startswith("g_")
        assert len(key) == 66

    def test_matches_md5(self):
        """Verify output matches: echo -n 'gen-abc' | md5sum | cut -c1-32"""
        expected = hashlib.md5(b"gen-abc").hexdigest()[:32]
        assert adapter.trace_id_from_generation("gen-abc") == expected


# ── span_id_16 ────────────────────────────────────────────────────────────


class TestSpanId16:
    def test_returns_16_hex(self):
        result = adapter.span_id_16()
        assert len(result) == 16
        int(result, 16)

    def test_unique(self):
        a = adapter.span_id_16()
        b = adapter.span_id_16()
        assert a != b


# ── sanitize ──────────────────────────────────────────────────────────────


class TestSanitize:
    def test_unchanged(self):
        assert adapter.sanitize("hello") == "hello"

    def test_slash(self):
        assert adapter.sanitize("foo/bar") == "foo_bar"

    def test_preserves_dots_hyphens_underscores(self):
        assert adapter.sanitize("foo.bar-baz_qux") == "foo.bar-baz_qux"

    def test_special_chars(self):
        assert adapter.sanitize("a@b#c$d") == "a_b_c_d"

    def test_empty(self):
        assert adapter.sanitize("") == ""


# ── state_push / state_pop ────────────────────────────────────────────────


class TestStateStack:
    def test_push_pop_single(self):
        adapter.state_push("test_key", {"a": 1})
        result = adapter.state_pop("test_key")
        assert result == {"a": 1}

    def test_lifo_order(self):
        adapter.state_push("k", {"val": "A"})
        adapter.state_push("k", {"val": "B"})
        assert adapter.state_pop("k") == {"val": "B"}
        assert adapter.state_pop("k") == {"val": "A"}

    def test_pop_empty_returns_none(self):
        assert adapter.state_pop("nonexistent") is None

    def test_pop_corrupted_returns_none(self):
        stack_file = adapter.STATE_DIR / "bad.stack.json"
        stack_file.write_text(":::not valid json{{{")
        assert adapter.state_pop("bad") is None

    def test_push_creates_file(self):
        adapter.state_push("new_key", {"x": 1})
        stack_file = adapter.STATE_DIR / "new_key.stack.json"
        assert stack_file.exists()

    def test_pop_last_removes_stack_file(self):
        adapter.state_push("k2", {"x": 1})
        adapter.state_pop("k2")
        stack_file = adapter.STATE_DIR / "k2.stack.json"
        assert not stack_file.exists()

    def test_concurrent_push(self):
        """5 threads push concurrently — all values present, no corruption."""
        errors = []

        def push_val(i):
            try:
                adapter.state_push("concurrent", {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=push_val, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stack_file = adapter.STATE_DIR / "concurrent.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 5
        values = sorted(d["i"] for d in data)
        assert values == [0, 1, 2, 3, 4]

    def test_stack_file_valid_json(self):
        adapter.state_push("json_check", {"a": 1})
        adapter.state_push("json_check", {"b": 2})
        stack_file = adapter.STATE_DIR / "json_check.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 2

    def test_stack_file_is_pretty_indented(self):
        """JSON output uses indent=2 per the project's serialization convention."""
        adapter.state_push("pretty", {"a": 1, "b": [1, 2]})
        stack_file = adapter.STATE_DIR / "pretty.stack.json"
        text = stack_file.read_text()
        # indented JSON contains newlines between entries
        assert "\n" in text
        # confirm round-trip
        assert json.loads(text) == [{"a": 1, "b": [1, 2]}]

    def test_push_then_pop_roundtrip_with_nested(self):
        """JSON encoder must round-trip nested dicts/lists with the same fidelity YAML did."""
        payload = {"cmd": "echo hi", "env": {"FOO": "bar"}, "args": ["a", "b", "c"]}
        adapter.state_push("nested", payload)
        popped = adapter.state_pop("nested")
        assert popped == payload

    def test_empty_file_treated_as_empty_stack(self):
        """An empty stack file should be treated as []. push must still succeed."""
        empty = adapter.STATE_DIR / "empty.stack.json"
        empty.write_text("")
        # push should not raise — empty content → JSONDecodeError → fallback to []
        adapter.state_push("empty", {"k": 1})
        assert json.loads(empty.read_text()) == [{"k": 1}]

    def test_corrupt_file_on_push_resets_to_list(self):
        """If existing file is corrupt JSON, push resets list and appends."""
        corrupt = adapter.STATE_DIR / "corrupt.stack.json"
        corrupt.write_text("{not json")
        adapter.state_push("corrupt", {"k": "v"})
        assert json.loads(corrupt.read_text()) == [{"k": "v"}]


# ── gen_root_span ─────────────────────────────────────────────────────────


class TestGenRootSpan:
    def test_save_and_get(self):
        adapter.gen_root_span_save("gen-1", "span123")
        assert adapter.gen_root_span_get("gen-1") == "span123"

    def test_get_no_save(self):
        assert adapter.gen_root_span_get("gen-missing") == ""

    def test_get_empty_gen_id(self):
        assert adapter.gen_root_span_get("") == ""

    def test_save_overwrites(self):
        adapter.gen_root_span_save("gen-2", "old_span")
        adapter.gen_root_span_save("gen-2", "new_span")
        assert adapter.gen_root_span_get("gen-2") == "new_span"


# ── state_cleanup_generation ──────────────────────────────────────────────


class TestStateCleanupGeneration:
    def test_cleanup_removes_all_files(self):
        gen_id = "gen-cleanup"
        safe = adapter.generation_state_key(gen_id)

        # Create root file
        adapter.gen_root_span_save(gen_id, "span1")
        # Create stack files
        adapter.state_push(f"before_{safe}_shell", {"cmd": "ls"})
        adapter.state_push(f"before_{safe}_mcp", {"tool": "read"})

        shard = adapter.STATE_DIR / safe
        assert shard.is_dir()

        adapter.state_cleanup_generation(gen_id)

        assert not shard.exists()

    def test_cleanup_no_files_no_error(self):
        adapter.state_cleanup_generation("gen-nonexistent")  # should not raise

    def test_cleanup_preserves_other_generations(self):
        adapter.gen_root_span_save("gen-keep", "span_keep")
        adapter.gen_root_span_save("gen-remove", "span_remove")

        adapter.state_cleanup_generation("gen-remove")

        assert adapter.gen_root_span_get("gen-keep") == "span_keep"

    def test_cleanup_does_not_guess_legacy_generation_ownership(self):
        adapter.state_push("shell_g1", {"command": "legacy-g1"})
        adapter.state_push("shell_g10", {"command": "legacy-g10"})
        adapter.state_push("tool_a_b", {"generation": "a", "tool": "b"})

        adapter.state_cleanup_generation("g1")
        adapter.state_cleanup_generation("a_b")

        assert adapter.state_pop("shell_g1") == {"command": "legacy-g1"}
        assert adapter.state_pop("shell_g10") == {"command": "legacy-g10"}
        assert adapter.state_pop("tool_a_b") == {"generation": "a", "tool": "b"}

    def test_state_reads_reject_symlinked_stack_and_root_files(self, tmp_path):
        gen_id = "gen-read-symlink"
        token = adapter.generation_state_key(gen_id)
        shard = adapter.STATE_DIR / token
        shard.mkdir()

        outside_stack = tmp_path / "outside-stack.json"
        outside_stack.write_text('[{"secret": "EXTERNAL_SECRET"}]')
        key = f"shell_{token}"
        (shard / f"{key}.stack.json").symlink_to(outside_stack)
        with pytest.raises(OSError):
            adapter.state_pop(key)
        assert "EXTERNAL_SECRET" in outside_stack.read_text()

        outside_root = tmp_path / "outside-root"
        outside_root.write_text("EXTERNAL_PARENT_SECRET")
        (shard / f"root_{token}").symlink_to(outside_root)
        with pytest.raises(OSError):
            adapter.gen_root_span_get(gen_id)
        assert outside_root.read_text() == "EXTERNAL_PARENT_SECRET"

    def test_cleanup_rejects_symlink_shard_without_touching_target(self, tmp_path):
        gen_id = "gen-symlink"
        safe = adapter.generation_state_key(gen_id)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("do not delete")
        (adapter.STATE_DIR / safe).symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            adapter.state_cleanup_generation(gen_id)

        assert victim.read_text() == "do not delete"

        (adapter.STATE_DIR / safe).unlink()
        (adapter.STATE_DIR / safe).symlink_to(tmp_path / "missing", target_is_directory=True)
        with pytest.raises(OSError):
            adapter.state_cleanup_generation(gen_id)

    def test_cleanup_nonempty_lock_dir(self):
        gen_id = "gen-lockdir"
        safe = adapter.generation_state_key(gen_id)
        lock_dir = adapter.STATE_DIR / safe / f".lock_before_{safe}_shell"
        lock_dir.mkdir(parents=True)
        # Put a file inside so rmdir fails
        (lock_dir / "stale").write_text("x")

        with pytest.raises(OSError):
            adapter.state_cleanup_generation(gen_id)
        # Incomplete cleanup must be observable so durable retry provenance remains.
        assert lock_dir.exists()


class TestCompletedGenerationLedger:
    def test_global_guard_is_reentrant_and_releases_after_exception(self):
        acquired = threading.Event()
        guard = getattr(adapter, "completion_ledger_guard")

        with pytest.raises(RuntimeError, match="boom"):
            with guard():
                with guard():
                    raise RuntimeError("boom")

        def acquire_after_exception():
            with guard():
                acquired.set()

        thread = threading.Thread(target=acquire_after_exception)
        thread.start()
        thread.join(timeout=5)
        assert acquired.is_set()
        assert not thread.is_alive()

    def test_ledger_contains_only_hash(self):
        gen_id = "generation-RAW-SECRET"
        adapter.generation_mark_completed(gen_id)

        assert adapter.generation_is_completed(gen_id) is True
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        assert gen_id.encode() not in ledger.read_bytes()
        with sqlite3.connect(ledger) as connection:
            values = connection.execute("SELECT digest FROM completed_generations").fetchall()
        assert values == [(hashlib.sha256(gen_id.encode()).hexdigest(),)]

    def test_malformed_existing_ledger_stays_fail_closed(self):
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        ledger.write_bytes(b"not-a-sqlite-database")
        assert adapter.generation_is_completed("generation") is True
        assert adapter.generation_completion_status("generation") == "ledger-unavailable"

        with pytest.raises(sqlite3.DatabaseError):
            adapter.generation_mark_completed("another-generation")
        assert ledger.read_bytes() == b"not-a-sqlite-database"
        assert adapter.generation_is_completed("another-generation") is True

    def test_named_but_untrusted_digest_guards_fail_closed(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        guard_names = (
            "completed_digest_insert_guard",
            "completed_digest_update_guard",
            "pending_digest_insert_guard",
            "pending_digest_update_guard",
        )
        with sqlite3.connect(ledger) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            for name in guard_names:
                connection.execute(f"DROP TRIGGER {name}")
                connection.execute(
                    f"CREATE TRIGGER {name} AFTER DELETE ON completed_generations " "BEGIN SELECT 1; END"
                )
            connection.execute(
                "INSERT INTO completed_generations(digest) VALUES (?)",
                ("z" * 64,),
            )
            connection.execute("UPDATE completion_metadata SET row_count = row_count + 1")

        assert adapter.generation_completion_status("unseen") == "ledger-unavailable"
        assert adapter.generation_is_completed("unseen") is True

    def test_fresh_guards_reject_blob_digest_storage_class(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO completed_generations(digest) VALUES (?)",
                (sqlite3.Binary(b"a" * 64),),
            )

    def test_exact_guards_do_not_hide_preexisting_blob_digest(self):
        gen_id = "existing"
        digest = adapter.stable_digest(gen_id)
        adapter.generation_mark_completed(gen_id)
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        guards = (
            ("completed_digest_insert_guard", "completed_generations", "INSERT"),
            ("completed_digest_update_guard", "completed_generations", "UPDATE"),
            ("pending_digest_insert_guard", "pending_generation_cleanups", "INSERT"),
            ("pending_digest_update_guard", "pending_generation_cleanups", "UPDATE"),
        )
        with sqlite3.connect(ledger) as connection:
            for name, _, _ in guards:
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE completed_generations SET digest = ? WHERE digest = ?",
                (sqlite3.Binary(digest.encode()), digest),
            )
            for name, table, operation in guards:
                connection.execute(
                    f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                    "WHEN NEW.digest IS NULL OR typeof(NEW.digest) != 'text' "
                    "OR length(NEW.digest) != 64 "
                    "OR NEW.digest GLOB '*[^0-9a-f]*' "
                    "BEGIN SELECT RAISE(ABORT, 'invalid generation digest'); END"
                )

        assert adapter.generation_completion_status(gen_id) == "ledger-unavailable"
        assert adapter.generation_is_completed(gen_id) is True

    def test_legacy_blob_digest_with_old_exact_guards_fails_closed(self):
        adapter.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute(
                "CREATE TABLE completed_generations "
                "(digest TEXT NOT NULL PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'))"
            )
            connection.execute(
                "CREATE TABLE pending_generation_cleanups "
                "(digest TEXT NOT NULL PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'))"
            )
            connection.execute(
                "CREATE TABLE completion_metadata ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "row_count INTEGER NOT NULL CHECK(row_count >= 0), "
                "saturated INTEGER NOT NULL CHECK(saturated IN (0, 1)))"
            )
            connection.execute(
                "INSERT INTO completed_generations(digest) VALUES (?)",
                (sqlite3.Binary(b"a" * 64),),
            )
            connection.execute("INSERT INTO completion_metadata VALUES (1, 1, 0)")
            for name, table, operation in (
                ("completed_digest_insert_guard", "completed_generations", "INSERT"),
                ("completed_digest_update_guard", "completed_generations", "UPDATE"),
                ("pending_digest_insert_guard", "pending_generation_cleanups", "INSERT"),
                ("pending_digest_update_guard", "pending_generation_cleanups", "UPDATE"),
            ):
                connection.execute(
                    f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                    "WHEN NEW.digest IS NULL OR length(NEW.digest) != 64 OR "
                    "NEW.digest GLOB '*[^0-9a-f]*' "
                    "BEGIN SELECT RAISE(ABORT, 'invalid generation digest'); END"
                )

        assert adapter.generation_completion_status("unseen") == "ledger-unavailable"
        assert adapter.generation_is_completed("unseen") is True

    def test_null_digest_is_rejected_by_fresh_schema(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO completed_generations(digest) VALUES (NULL)")

    def test_legacy_null_digest_with_old_exact_guards_fails_closed(self):
        adapter.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute(
                "CREATE TABLE completed_generations "
                "(digest TEXT PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'))"
            )
            connection.execute(
                "CREATE TABLE pending_generation_cleanups "
                "(digest TEXT PRIMARY KEY CHECK(length(digest) = 64 "
                "AND digest NOT GLOB '*[^0-9a-f]*'))"
            )
            connection.execute(
                "CREATE TABLE completion_metadata ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "row_count INTEGER NOT NULL CHECK(row_count >= 0), "
                "saturated INTEGER NOT NULL CHECK(saturated IN (0, 1)))"
            )
            connection.execute("INSERT INTO completed_generations(digest) VALUES (NULL)")
            connection.execute("INSERT INTO completion_metadata VALUES (1, 1, 0)")
            for name, table, operation in (
                ("completed_digest_insert_guard", "completed_generations", "INSERT"),
                ("completed_digest_update_guard", "completed_generations", "UPDATE"),
                ("pending_digest_insert_guard", "pending_generation_cleanups", "INSERT"),
                ("pending_digest_update_guard", "pending_generation_cleanups", "UPDATE"),
            ):
                connection.execute(
                    f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                    "WHEN length(NEW.digest) != 64 OR "
                    "NEW.digest GLOB '*[^0-9a-f]*' "
                    "BEGIN SELECT RAISE(ABORT, 'invalid generation digest'); END"
                )

        assert adapter.generation_completion_status("unseen") == "ledger-unavailable"
        assert adapter.generation_is_completed("unseen") is True

    def test_empty_metadata_singleton_is_repaired_atomically(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DELETE FROM completion_metadata")

        assert adapter.generation_completion_status("existing") == "completed"
        adapter.generation_mark_completed("new")

        with sqlite3.connect(ledger) as connection:
            metadata = connection.execute(
                "SELECT row_count, saturated FROM completion_metadata WHERE singleton = 1"
            ).fetchone()
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
        assert metadata == (2, 0)
        assert count == 2

    def test_concurrent_empty_metadata_repair_is_serialized(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DELETE FROM completion_metadata")

        barrier = threading.Barrier(8)
        statuses = []
        errors = []

        def repair():
            try:
                barrier.wait()
                statuses.append(adapter.generation_completion_status("existing"))
            except Exception as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        threads = [threading.Thread(target=repair) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert statuses == ["completed"] * 8
        with sqlite3.connect(ledger) as connection:
            rows = connection.execute("SELECT singleton, row_count, saturated FROM completion_metadata").fetchall()
        assert rows == [(1, 1, 0)]

    def test_empty_metadata_repair_reconstructs_saturation(self, monkeypatch):
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 1)
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DELETE FROM completion_metadata")

        assert adapter.generation_completion_status("unseen") == "ledger-saturated"
        with sqlite3.connect(ledger) as connection:
            metadata = connection.execute(
                "SELECT row_count, saturated FROM completion_metadata WHERE singleton = 1"
            ).fetchone()
        assert metadata == (1, 1)

    def test_malformed_metadata_fails_closed_explicitly(self):
        adapter.generation_mark_completed("existing")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DROP TABLE completion_metadata")
            connection.execute(
                "CREATE TABLE completion_metadata (singleton INTEGER, row_count INTEGER, saturated INTEGER)"
            )
            connection.execute("INSERT INTO completion_metadata VALUES (1, -1, 0)")

        assert adapter.generation_completion_status("unseen") == "ledger-unavailable"
        assert adapter.generation_is_completed("unseen") is True
        with pytest.raises(sqlite3.DatabaseError, match="invalid completion ledger metadata"):
            adapter.generation_mark_completed("unseen")

    def test_missing_empty_completion_table_is_recreated(self):
        adapter.generation_mark_completed("temporary")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DELETE FROM completed_generations")
            connection.execute("UPDATE completion_metadata SET row_count = 0, saturated = 0")
            connection.execute("DROP TABLE completed_generations")

        assert adapter.generation_completion_status("new") == "active"
        adapter.generation_mark_completed("new")
        assert adapter.generation_completion_status("new") == "completed"

    def test_missing_nonempty_completion_table_repairs_to_global_saturation(self):
        adapter.generation_mark_completed("lost")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("DROP TABLE completed_generations")

        assert adapter.generation_completion_status("unseen") == "ledger-saturated"
        adapter.generation_mark_completed("unseen")
        with sqlite3.connect(ledger) as connection:
            metadata = connection.execute(
                "SELECT row_count, saturated FROM completion_metadata WHERE singleton = 1"
            ).fetchone()
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
        assert metadata == (0, 1)
        assert count == 0

    def test_metadata_undercount_is_reconciled_before_cap_check(self, monkeypatch):
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 3)
        adapter.generation_mark_completed("one")
        adapter.generation_mark_completed("two")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("UPDATE completion_metadata SET row_count = 0, saturated = 0")

        adapter.generation_mark_completed("three")
        adapter.generation_mark_completed("four")
        with sqlite3.connect(ledger) as connection:
            metadata = connection.execute(
                "SELECT row_count, saturated FROM completion_metadata WHERE singleton = 1"
            ).fetchone()
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
        assert metadata == (3, 1)
        assert count == 3

    def test_metadata_overcount_fails_closed_via_global_saturation(self, monkeypatch):
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 3)
        adapter.generation_mark_completed("one")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute("UPDATE completion_metadata SET row_count = 2, saturated = 0")

        assert adapter.generation_completion_status("unseen") == "ledger-saturated"
        with sqlite3.connect(ledger) as connection:
            metadata = connection.execute(
                "SELECT row_count, saturated FROM completion_metadata WHERE singleton = 1"
            ).fetchone()
        assert metadata == (1, 1)

    def test_locked_ledger_mark_never_falsely_succeeds(self, monkeypatch):
        adapter.generation_mark_completed("seed")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        monkeypatch.setattr(adapter, "COMPLETION_DB_TIMEOUT_SECONDS", 0.01)

        with sqlite3.connect(ledger, isolation_level=None) as owner:
            owner.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError):
                adapter.generation_mark_completed("must-not-be-lost")

        assert adapter.generation_is_completed("must-not-be-lost") is False

    def test_ledger_saturates_fail_closed_at_hard_cap(self, monkeypatch):
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 3)
        for index in range(3):
            adapter.generation_mark_completed(f"generation-{index}")

        assert adapter.generation_is_completed("never-inserted") is True
        assert adapter.generation_completion_status("never-inserted") == "ledger-saturated"
        adapter.generation_mark_completed("also-not-inserted")
        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
            saturated = connection.execute("SELECT saturated FROM completion_metadata WHERE singleton = 1").fetchone()[
                0
            ]
        assert count == 3
        assert saturated == 1

    def test_generation_guard_busy_stripe_fails_without_takeover(self, monkeypatch):
        gen_id = "busy-generation"
        stripe = int(adapter._generation_digest(gen_id)[:2], 16) % 64
        lock_dir = adapter.STATE_DIR / ".generation_locks"
        lock_dir.mkdir(parents=True)
        lock_db = lock_dir / f"stripe_{stripe:02d}.sqlite3"
        monkeypatch.setattr(adapter, "GENERATION_GUARD_TIMEOUT_SECONDS", 0.01)

        with sqlite3.connect(lock_db, isolation_level=None) as owner:
            owner.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError):
                with adapter.generation_guard(gen_id):
                    pytest.fail("busy stripe was taken over")

    def test_ledger_retains_all_hashes_and_generation_locks_are_bounded(self):
        for index in range(100):
            gen_id = f"generation-{index}"
            with adapter.generation_guard(gen_id):
                adapter.generation_mark_completed(gen_id)

        ledger = adapter.STATE_DIR / "completed_generations.sqlite3"
        with sqlite3.connect(ledger) as connection:
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
        assert count == 100
        adapter.generation_mark_completed("generation-99")
        with sqlite3.connect(ledger) as connection:
            count = connection.execute("SELECT count(*) FROM completed_generations").fetchone()[0]
        assert count == 100
        assert len(list((adapter.STATE_DIR / ".generation_locks").glob("stripe_*"))) <= 64


# ── check_requirements ────────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        assert adapter.check_requirements() is True
        assert adapter.STATE_DIR.exists()

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        assert adapter.check_requirements() is False
