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
import re
import sqlite3
import sys
from datetime import datetime

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
    """Temp ledger, temp state file, temp pending sidecar, temp output dir."""
    ledger = tmp_path / "runs.jsonl"
    state = tmp_path / ".ledger_state"
    pending = tmp_path / ".ledger_pending"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return {"ledger": str(ledger), "state": str(state), "pending": str(pending),
            "out_dir": str(out_dir), "tmp": tmp_path}


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
                           pending_path=env["pending"], out_dir=env["out_dir"],
                           job_id="job1")


# --- (a) a run first seen 'running' must still get its 'done' line ---------

def test_run_first_seen_running_gets_start_then_done(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    rowid = add_row(db, started=started, status="running")
    write_out(env["out_dir"], finished, "run finished cleanly")

    lines, state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
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
    assert lines2[0]["time"] == "2026-11-08T19:39:34-08:00"
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

    lines, state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
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

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
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

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "NEARBY OUTPUT"


def test_outcome_refuses_a_file_beyond_the_match_window(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T19:39:34.564870-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], "2026-11-08T19:50:00.000000-08:00", "TOO FAR AWAY")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "finished (no output captured)"


# --- (d) error rows and the GITOPS-QUIET token ---------------------------

def test_error_row_yields_error_outcome(db, env):
    add_row(db, started="2026-11-08T18:30:36.042103-08:00",
            finished="2026-11-08T18:31:00.000000-08:00", status="failed",
            error="RuntimeError: boom")
    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "ERROR: RuntimeError: boom"


def test_quiet_response_yields_gitops_quiet(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished, "GITOPS-QUIET nothing to do")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    done = [l for l in lines if l["status"] == "done"]
    assert done[0]["outcome"] == "GITOPS-QUIET"


# --- (e) the old bare-integer state file migrates without a duplicate start

def test_legacy_integer_state_file_is_migrated(db, env):
    started = "2026-11-08T19:50:36.893132-08:00"
    running_rowid = add_row(db, started=started, status="running")
    # the old writer left a bare integer behind
    with open(env["state"], "w") as fh:
        fh.write(str(running_rowid))

    state = sync.load_state(env["state"], env["pending"])
    assert state["last_rowid"] == running_rowid

    lines, new_state = run_pass(db, env, state)
    # the in-flight row was already started by the old writer: no second start
    assert [l["status"] for l in lines] == []
    assert new_state["seen_start"] == [running_rowid]

    ledger = read_ledger(env["ledger"])
    assert ledger == []

    # the state file stays a bare integer, so the OLD script can still read it
    with open(env["state"]) as fh:
        assert fh.read().strip() == str(running_rowid)
    # the in-flight set lives in the sidecar
    with open(env["pending"]) as fh:
        assert json.load(fh) == [running_rowid]


def test_legacy_state_file_completes_the_in_flight_run(db, env):
    started = "2026-11-08T19:50:36.893132-08:00"
    finished = "2026-11-08T20:02:00.000000-08:00"
    rowid = add_row(db, started=started, status="running")
    with open(env["state"], "w") as fh:
        fh.write(str(rowid))

    state = sync.load_state(env["state"], env["pending"])
    lines, new_state = run_pass(db, env, state)
    assert lines == []

    db.execute("UPDATE executions SET status='completed', finished_at=? WHERE rowid=?",
               (finished, rowid))
    db.commit()
    lines2, new_state2 = run_pass(db, env, new_state)
    assert [l["status"] for l in lines2] == ["done"]
    assert new_state2["seen_start"] == []


def test_state_file_stays_readable_by_the_previous_version(db, env):
    """Rollback safety: the state file must remain a bare integer, because the
    previous version of this script reads it with int(). Were it JSON, that
    int() would raise, last_rowid would fall back to 0, and a rollback would
    re-write a start+done line for every run in the database's history."""
    add_row(db, started="2026-11-08T18:30:36.042103-08:00",
            finished="2026-11-08T18:31:00.000000-08:00", status="completed")
    _lines, state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))

    with open(env["state"]) as fh:
        raw = fh.read().strip()
    assert int(raw) == state["last_rowid"]   # the old int() still works
    assert not raw.startswith("{")           # not JSON


def test_pending_sidecar_survives_a_restart(db, env):
    """The in-flight set is persisted, so a restart between passes still
    remembers that a run already got its 'start' line."""
    started = "2026-11-08T18:30:36.042103-08:00"
    rowid = add_row(db, started=started, status="running")

    _lines, state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    assert state["seen_start"] == [rowid]

    # simulate a restart: state re-read from disk
    reloaded = sync.load_state(env["state"], env["pending"])
    assert reloaded["seen_start"] == [rowid]
    lines, state2 = run_pass(db, env, reloaded)
    assert lines == []            # no duplicate start
    assert state2["seen_start"] == [rowid]


# --- (f) the ledger stays one JSON object per line ------------------------

def test_ledger_lines_are_one_json_object_each(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    add_row(db, started="2026-11-08T18:40:36.042103-08:00", status="running")

    run_pass(db, env, sync.load_state(env["state"], env["pending"]))

    with open(env["ledger"]) as fh:
        raw = fh.read()
    assert raw.endswith("\n")
    for line in raw.splitlines():
        obj = json.loads(line)  # raises if two objects share a line
        assert set(obj) == {"time", "trigger", "outcome", "status"}
    assert len(read_ledger(env["ledger"])) == 3


# --- (h) the outcome text is cut at a word boundary, not mid-word ---------
#
# The old code hard-cut the outcome at exactly 120 characters (110 for the
# error detail), which sliced words in half: "…close two open loops rather
# than escalate" was recorded as "...close two open loops rather tha". The
# reader could not tell a truncated sentence from a complete one, and the
# clipped fragment was unreadable. The cut now lands on a word boundary and
# an ellipsis marks that the text was elided.


def test_long_outcome_is_cut_at_a_word_boundary(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    long_response = ("Close two open loops rather than escalate: add credits "
                     "or lower the request rate for this workspace, then "
                     "re-run the deploy job and confirm the checks are green.")
    assert len(long_response) > sync.OUTCOME_MAX
    write_out(env["out_dir"], finished, long_response)

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    outcome = [l for l in lines if l["status"] == "done"][0]["outcome"]

    # the elision is marked
    assert outcome.endswith("…")
    assert sync.ELLIPSIS in outcome
    # and no word was cut in half: the text before the ellipsis ends on a
    # complete word, and the truncated word is gone entirely
    assert not outcome[: -len(sync.ELLIPSIS)].endswith("esc")
    assert outcome[:-len(sync.ELLIPSIS)] == long_response[:len(outcome) - 1].rstrip()
    assert outcome[:-len(sync.ELLIPSIS)] in long_response


def test_truncated_outcome_never_exceeds_the_budget(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished,
              "word " * 200 + "tail")  # one very long, whitespace-separated run

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    outcome = [l for l in lines if l["status"] == "done"][0]["outcome"]

    assert len(outcome) <= sync.OUTCOME_MAX
    assert outcome.endswith(sync.ELLIPSIS)


def test_short_outcome_is_returned_unchanged(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished, "run finished cleanly")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    outcome = [l for l in lines if l["status"] == "done"][0]["outcome"]

    assert outcome == "run finished cleanly"
    assert sync.ELLIPSIS not in outcome


def test_long_error_is_cut_at_a_word_boundary_and_stays_within_budget(db, env):
    error = ("RuntimeError: the credential for the portal could not be "
             "refreshed after three attempts, giving up for now and "
             "scheduling a retry in the next cron cycle")
    assert len(error) > sync.ERROR_MAX
    add_row(db, started="2026-11-08T18:30:36.042103-08:00",
            finished="2026-11-08T18:31:00.000000-08:00", status="failed",
            error=error)

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    outcome = [l for l in lines if l["status"] == "done"][0]["outcome"]

    assert outcome.startswith("ERROR: ")
    detail = outcome[len("ERROR: "):]
    assert detail.endswith(sync.ELLIPSIS)
    assert len(detail) <= sync.ERROR_MAX
    assert len(outcome) <= sync.OUTCOME_MAX
    # the kept text ends on a complete word, never mid-word
    assert detail[:-len(sync.ELLIPSIS)] in error
    assert detail[:-len(sync.ELLIPSIS)] == error[:len(detail) - 1].rstrip()


def test_gitops_quiet_is_returned_verbatim(db, env):
    """The QUIET token must never be shortened or ellipsised."""
    started = "2026-11-08T18:30:36.042103-08:00"
    finished = "2026-11-08T18:31:00.000000-08:00"
    add_row(db, started=started, finished=finished, status="completed")
    write_out(env["out_dir"], finished, "GITOPS-QUIET nothing to do")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    outcome = [l for l in lines if l["status"] == "done"][0]["outcome"]

    assert outcome == "GITOPS-QUIET"


# --- (i) truncate() edge cases -------------------------------------------

def test_truncate_leaves_short_text_untouched():
    assert sync.truncate("all good", sync.OUTCOME_MAX) == "all good"


def test_truncate_leaves_text_at_the_budget_untouched():
    text = "x" * sync.OUTCOME_MAX
    assert sync.truncate(text, sync.OUTCOME_MAX) == text
    assert sync.ELLIPSIS not in sync.truncate(text, sync.OUTCOME_MAX)


def test_truncate_handles_empty_text():
    assert sync.truncate("", sync.OUTCOME_MAX) == ""


def test_truncate_handles_text_with_no_whitespace():
    """A single unbreakable token has no word boundary to cut on; the budget
    still wins and the elision is still marked."""
    text = "y" * 300
    out = sync.truncate(text, sync.OUTCOME_MAX)
    assert len(out) <= sync.OUTCOME_MAX
    assert out.endswith(sync.ELLIPSIS)


def test_truncate_never_splits_a_word():
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    out = sync.truncate(text, 30)
    assert len(out) <= 30
    assert out.endswith(sync.ELLIPSIS)
    kept = out[:-len(sync.ELLIPSIS)]
    assert kept == "alpha bravo charlie delta"
    for word in kept.split():
        assert word in text.split()


def test_truncate_cuts_at_the_last_boundary_inside_the_budget():
    text = "aaaa bbbb cccc dddd eeee"
    # the boundary after "dddd" is the last one at or before 20 chars
    out = sync.truncate(text, 20)
    assert out == "aaaa bbbb cccc dddd…"
    assert len(out) == 20


def test_truncate_respects_a_tiny_budget():
    out = sync.truncate("hello world", 3)
    assert len(out) <= 3
    assert out.endswith(sync.ELLIPSIS)


# --- extra: the trigger attribution and start backdating are unchanged -----

def test_start_line_is_backdated_to_the_run_start(db, env):
    started = "2026-11-08T18:30:36.042103-08:00"
    add_row(db, started=started, finished="2026-11-08T19:39:34.564870-08:00",
            status="completed")
    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    start = [l for l in lines if l["status"] == "start"][0]
    assert start["time"] == "2026-11-08T18:30:36-08:00"
    assert start["trigger"] == "10m poll"


def test_seen_start_is_pruned_so_it_cannot_grow_without_bound(db, env):
    for i in range(3):
        add_row(db, started="2026-11-08T18:30:36.042103-08:00",
                finished="2026-11-08T18:31:00.000000-08:00", status="completed")
    lines, state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    assert len([l for l in lines if l["status"] == "done"]) == 3
    assert state["seen_start"] == []


# --- (g) the "time" column is ISO-8601 with an explicit UTC offset ---------
#
# The live worker prompt mandates this exact shape for its own ledger lines
# (ISO-8601 with an offset, taken from `TZ=America/Los_Angeles
# date -Iseconds`, e.g. "2026-11-08T22:30:50-08:00"). The sync script used
# to write a bare local wall clock, so one column carried two formats — and
# during the DST fall-back hour the bare clock is genuinely ambiguous: the
# same wall clock occurs twice, an hour apart in real time.

# The shape `date -Iseconds` prints: seconds precision, no microseconds,
# and a UTC offset with a colon.
ISO_SECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def test_dst_offset_follows_the_season(db, env):
    """July is PDT (-07:00); December is PST (-08:00).

    A hardcoded offset would read an hour AHEAD of the true local time for
    half the year — the exact bug this offset format exists to prevent."""
    july_started = "2026-07-04T12:00:00.000000-07:00"
    july_finished = "2026-07-04T13:30:00.000000-07:00"
    dec_started = "2026-12-04T12:00:00.000000-08:00"
    dec_finished = "2026-12-04T13:30:00.000000-08:00"
    add_row(db, started=july_started, finished=july_finished, status="completed")
    add_row(db, started=dec_started, finished=dec_finished, status="completed")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    times = [l["time"] for l in lines]
    assert times == ["2026-07-04T12:00:00-07:00",
                     "2026-07-04T13:30:00-07:00",
                     "2026-12-04T12:00:00-08:00",
                     "2026-12-04T13:30:00-08:00"]
    assert "-07:00" in times[0] and "-08:00" in times[2]


def test_every_written_line_carries_a_utc_offset(db, env):
    """No written line may fall back to a bare, offset-less wall clock."""
    add_row(db, started="2026-07-04T12:00:00.000000-07:00",
            finished="2026-07-04T13:30:00.000000-07:00", status="completed")
    add_row(db, started="2026-11-08T18:30:36.042103-08:00", status="running")

    run_pass(db, env, sync.load_state(env["state"], env["pending"]))

    ledger = read_ledger(env["ledger"])
    assert ledger, "the pass wrote no ledger lines"
    for line in ledger:
        parsed = datetime.fromisoformat(line["time"])
        assert parsed.utcoffset() is not None, (
            "ledger line has no UTC offset: %r" % (line["time"],))


def test_rendered_time_matches_the_date_i_seconds_shape(db, env):
    """Seconds precision, no microseconds, offset with a colon."""
    add_row(db, started="2026-11-08T18:30:36.042103-08:00",
            finished="2026-11-08T19:39:34.564870-08:00", status="completed")
    write_out(env["out_dir"], "2026-11-08T19:39:34.564870-08:00", "ok")

    lines, _state = run_pass(db, env, sync.load_state(env["state"], env["pending"]))
    assert lines
    for line in lines:
        stamp = line["time"]
        assert ISO_SECONDS_RE.match(stamp), stamp
        assert "." not in stamp, "microseconds leaked into the stamp: %r" % (stamp,)
    # the microseconds of the source instants are dropped, not rounded
    assert [l["time"] for l in lines] == ["2026-11-08T18:30:36-08:00",
                                          "2026-11-08T19:39:34-08:00"]


def test_iso_stamp_rejects_a_naive_datetime():
    """A naive datetime would render WITHOUT an offset — refuse it loudly."""
    naive = datetime(2026, 11, 8, 22, 30, 50)
    with pytest.raises(ValueError):
        sync.iso_stamp(naive)
    # the tz-aware equivalent renders the mandated shape
    assert sync.iso_stamp(naive.replace(tzinfo=sync.PT)) == "2026-11-08T22:30:50-08:00"


def test_pt_of_always_returns_an_aware_datetime():
    """Every fallback path in pt_of() must stay tz-aware, or the ledger
    would silently regress to a bare wall clock."""
    aware = sync.pt_of("2026-11-08T18:30:36.042103-08:00")
    assert aware.tzinfo is not None and aware.utcoffset() is not None
    # no input at all -> now() in PT
    assert sync.pt_of(None).tzinfo is not None
    # unparseable input -> the same fallback
    assert sync.pt_of("not-a-timestamp").tzinfo is not None
    # explicit fallback instant
    assert sync.pt_of(None, aware).tzinfo is not None
    # a naive fallback is anchored to PT rather than the system zone
    naive_fallback = datetime(2026, 11, 8, 18, 30, 36)
    anchored = sync.pt_of(None, naive_fallback)
    assert anchored.tzinfo is not None
    assert anchored.utcoffset() is not None
    assert anchored.isoformat() == "2026-11-08T18:30:36-08:00"
