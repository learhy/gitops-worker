#!/usr/bin/env python3
"""Tests for the ledger sync (gitops-ledger-sync.py).

Pure in-memory + tempdir tests: no network, no live ledger, no live state
file, no cron execution. The module is imported by path (its filename is not
a valid identifier) and its path-dependent helpers are called with explicit
tempdir paths.
"""
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "gitops-ledger-sync.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("ledger_sync", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ledger_sync"] = mod
    spec.loader.exec_module(mod)
    return mod


sync = _load_module()


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A stand-in for ~/.hermes/cron/executions.db with the worker's rows."""
    path = tmp_path / "executions.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE executions (job_id TEXT, started_at TEXT, finished_at TEXT, "
        "status TEXT, error TEXT)")
    conn.commit()
    return conn


@pytest.fixture
def env(tmp_path):
    """Temp ledger, temp state file, temp output dir."""
    ledger = tmp_path / "runs.jsonl"
    state = tmp_path / ".ledger_state"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return {"ledger": str(ledger), "state": str(state), "out_dir": str(out_dir),
            "tmp": tmp_path}


def add_row(conn, job_id="job1", started=None, finished=None, status="completed",
            error=None):
    cur = conn.execute(
        "INSERT INTO executions (job_id, started_at, finished_at, status, error) "
        "VALUES (?,?,?,?,?)", (job_id, started, finished, status, error))
    conn.commit()
    return cur.lastrowid


def write_out(out_dir, finished_at, response="all good"):
    """Write a run output file named after the run's finish time."""
    stamp = sync.pt_of(finished_at)
    name = stamp.strftime(sync.OUT_NAME_FMT) + ".md"
    path = os.path.join(out_dir, name)
    with open(path, "w") as fh:
        fh.write("# Cron Job\n\n## Response\n\n%s\n" % response)
    return path


def read_ledger(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run_pass(db, env, state):
    """Run one sync pass against the temp paths; returns (lines, next_state)."""
    return sync.sync_pass(db, state, ledger=env["ledger"], state_path=env["state"],
                           out_dir=env["out_dir"], job_id="job1")


# --- (a) a run first seen 'running' must still get its 'done' line ---------

def test_run_first_seen_running_gets_start_then_done(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    rowid = add_row(db, started=started, status="running")
    write_out(env["out_dir"], finished, "run finished cleanly")

    lines, state = run_pass(db, env, sync.load_state(env["state"]))
    assert [l["status"] for l in lines] == ["start"]
    assert state["seen_start"] == [rowid]
    # last_rowid must not advance past a row that never got a 'done' line
    assert state["last_rowid"] == 0

    # the run finishes; the next pass must still find the row
    db.execute("UPDATE executions SET status='completed', finished_at=? WHERE rowid=?",
               (finished, rowid))
    db.commit()
    lines2, state2 = run_pass(db, env, state)

    assert [l["status"] for l in lines2] == ["done"]
    assert lines2[0]["outcome"] == "run finished cleanly"
    assert lines2[0]["time"] == "2026-11-08 19:39:34"
    assert state2["seen_start"] == []
    assert state2["last_rowid"] == rowid

    ledger = read_ledger(env["ledger"])
    assert [l["status"] for l in ledger] == ["start", "done"]
    assert sum(1 for l in ledger if l["status"] == "start") == 1
    assert sum(1 for l in ledger if l["status"] == "done") == 1


# --- (b) a run still 'running' across passes gets one start, no done -------

def test_run_still_running_across_passes_gets_one_start_only(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    rowid = add_row(db, started=started, status="running")

    lines, state = run_pass(db, env, sync.load_state(env["state"]))
    assert [l["status"] for l in lines] == ["start"]

    lines2, state2 = run_pass(db, env, state)
    assert lines2 == []
    assert state2["seen_start"] == [rowid]
    assert state2["last_rowid"] == 0

    ledger = read_ledger(env["ledger"])
    assert len(ledger) == 1
    assert ledger[0]["status"] == "start"


# --- (c) the outcome text comes from THIS row's output file ----------------

def test_done_line_uses_its_own_output_file_not_the_newest(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished, "THIS RUN'S OUTPUT")
    # a newer, unrelated run's file exists in the same directory
    write_out(env["out_dir"], "2026-11-08T20:05:00.000000-08:00", "SOMEONE ELSE'S OUTPUT")

    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    done = [l for l in lines if l["status"] == "done"]
    assert len(done) == 1
    assert done[0]["outcome"] == "THIS RUN'S OUTPUT"


def test_outcome_falls_back_to_nearest_file_within_window(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    # no exact-name file; a file 120s away is close enough
    write_out(env["out_dir"], "2026-11-08T19:37:34.564870-08:00", "NEARBY OUTPUT")
    write_out(env["out_dir"], "2026-11-08T21:00:00.000000-08:00", "FAR OUTPUT")

    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "NEARBY OUTPUT"


def test_outcome_refuses_a_file_beyond_the_match_window(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], "2026-11-08T19:50:00.000000-08:00", "TOO FAR AWAY")

    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "finished (no output captured)"


# --- (d) error rows and the GITOPS-QUIET token ---------------------------

def test_error_row_yields_error_outcome(db, env):
    add_row(db, started="2026-11-08T18:30:36.042103-08:00",
            finished="2026-11-08T18:31:00.000000-08:00", status="failed",
            error="RuntimeError: boom")
    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "ERROR: RuntimeError: boom"


def test_quiet_response_yields_gitops_quiet(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished, "GITOPS-QUIET nothing to do")

    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "GITOPS-QUIET"


# --- (e) the old bare-integer state file migrates without a duplicate start

def test_legacy_integer_state_file_is_migrated(db, env):
    started = "2026-11-08T19:50:36.893132-08:00"
    running_rowid = add_row(db, started=started, status="running")
    # the old writer left a bare integer behind
    with open(env["state"], "w") as fh:
        fh.write(str(running_rowid))

    state = sync.load_state(env["state"])
    assert state["legacy"] is True
    assert state["last_rowid"] == running_rowid

    lines, new_state = run_pass(db, env, state)
    # the in-flight row was already started by the old writer: no second start
    assert [l["status"] for l in lines] == []
    assert new_state["seen_start"] == [running_rowid]

    ledger = read_ledger(env["ledger"])
    assert ledger == []

    # the migrated state file is now JSON
    with open(env["state"]) as fh:
        saved = json.load(fh)
    assert saved["last_rowid"] == running_rowid
    assert saved["seen_start"] == [running_rowid]


def test_legacy_state_file_completes_the_in_flight_run(db, env):
    started = "2026-11-08T19:50:36.893132-08:00"
    finished = "2026-11-08T20:02:00.000000-08:00"
    rowid = add_row(db, started=started, status="running")
    with open(env["state"], "w") as fh:
        fh.write(str(rowid))

    state = sync.load_state(env["state"])
    lines, new_state = run_pass(db, env, state)
    assert lines == []

    db.execute("UPDATE executions SET status='completed', finished_at=? WHERE rowid=?",
               (finished, rowid))
    db.commit()
    lines2, new_state2 = run_pass(db, env, new_state)
    assert [l["status"] for l in lines2] == ["done"]
    assert new_state2["seen_start"] == []


# --- (f) the ledger stays one JSON object per line ------------------------

def test_ledger_lines_are_one_json_object_each(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    add_row(db, started="2026-11-08T18:40:36.042103-08:00", status="running")

    run_pass(db, env, sync.load_state(env["state"]))

    with open(env["ledger"]) as fh:
        raw = fh.read()
    assert raw.endswith("\n")
    for line in raw.splitlines():
        obj = json.loads(line)  # raises if two objects share a line
        assert set(obj) == {"time", "trigger", "outcome", "status"}
    assert len(read_ledger(env["ledger"])) == 3


# --- extra: the trigger attribution and start backdating are unchanged -----

def test_start_line_is_backdated_to_the_run_start(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    add_row(db, started=started, finished="2026-11-08T19:39:34.564870-08:00",
            status="completed")
    lines, _state = run_pass(db, env, sync.load_state(env["state"]))
    start = [l for l in lines if l["status"] == "start"][0]
    assert start["time"] == "2026-11-08 18:30:36"
    assert start["trigger"] == "10m poll"


def test_seen_start_is_pruned_so_it_cannot_grow_without_bound(db, env):
    for i in range(3):
        add_row(db, started="2026-11-08T18:30:36.042103-08:00",
                finished="2026-11-08T18:31:00.000000-08:00", status="completed")
    lines, state = run_pass(db, env, sync.load_state(env["state"]))
    assert len([l for l in lines if l["status"] == "done"]) == 3
    assert state["seen_start"] == []
