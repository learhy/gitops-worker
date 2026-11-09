#!/usr/bin/env python3
"""Ledger sync: mirror gitops-github-worker run starts/finishes into the
human-visible ledger (~/mockup-previews/gitops/runs.jsonl).

Runs every 2 min as a no_agent cron job. State lives in STATE_FILE as JSON:
{"last_rowid": <int>, "seen_start": [<rowid>, ...]} — last_rowid is the
highest executions.rowid already recorded, seen_start the rowids that got a
'start' line but are still in flight. Each run appends up to two lines:
  ▶ started — <trigger>        (trigger attributed from gateway.log)
  outcome line                 (from that run's own output file)
"""
import json
import os
import sqlite3
import re
import glob
import time
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# DST-aware Pacific time. A hardcoded offset (-7) silently drifts by an hour
# every time US daylight saving ends (first Sunday in November): the ledger
# stamped 2026-11-01.. as PDT while the true local zone was PST, so every
# sync-written line read an hour AHEAD of the run it described.
PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = "/home/dan.rohan/mockup-previews/gitops/.ledger_state"
# The in-flight rowids live in a SIDECAR file, not in STATE_FILE. STATE_FILE
# stays a bare integer because the PREVIOUS version of this script read it
# with int(); if a rollback to that version ever happens, it must still read a
# valid high-water mark. Were the sidecar merged into STATE_FILE as JSON, the
# old int() would raise, last_rowid would fall back to 0, and the old script
# would re-write a 'start'+'done' line for every run in the database's history
# — a duplicate flood in the ledger. The sidecar keeps rollback safe.
PENDING_FILE = "/home/dan.rohan/mockup-previews/gitops/.ledger_pending"
LEDGER = "/home/dan.rohan/mockup-previews/gitops/runs.jsonl"
JOB_ID = "f42f6ffda76b"
OUT_DIR = f"/home/dan.rohan/.hermes/cron/output/{JOB_ID}"
GATEWAY_LOG = "/home/dan.rohan/.hermes/logs/gateway.log"
CRED_MARK = "/home/dan.rohan/mockup-previews/gitops/.cred_retry_state"
EXEC_DB = "/home/dan.rohan/.hermes/cron/executions.db"
TRIGGER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO gateway\.platforms\.webhook: "
    r"\[webhook\] cron-trigger event=(\S+) route=\S+ job=" + JOB_ID)
# Output files are named after the run's finish time in local time, e.g. a run
# that started 18:30:36 and finished 19:39:34 writes 2026-11-08_19-39-34.md.
OUT_NAME_FMT = "%Y-%m-%d_%H-%M-%S"
# How far (seconds) an output file's timestamp may sit from a row's finished_at
# and still be attributed to that row.
OUT_MATCH_WINDOW = 300

# webhook cron-trigger events, indexed from gateway.log (log stamps are UTC).
# Filled in by main(); attribute_trigger() falls back to "10m poll" when empty.
triggers = []  # (datetime_utc_naive, event)

# --- self-heal: the worker's concurrent appends can land multiple JSON objects
# on one line (missing newline between writers), which blanks the status page.
# Split any concatenated objects back to one-per-line before syncing.
def _repair_ledger():
    try:
        raw = open(LEDGER).read().strip()
    except FileNotFoundError:
        return 0
    dec = json.JSONDecoder()
    out, fixed = [], 0
    for chunk in raw.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = 0
        while p < len(chunk):
            try:
                obj, end = dec.raw_decode(chunk, p)
            except json.JSONDecodeError:
                out.append(chunk[p:])
                break
            out.append(json.dumps(obj, ensure_ascii=False))
            p = end
            if end < len(chunk):
                fixed += 1
    if fixed:
        with open(LEDGER, "w") as fh:
            fh.write("\n".join(out) + "\n")
    return fixed


def attribute_trigger(started_iso):
    """Match a run's start to the nearest webhook cron-trigger log line (±10s).
    gateway.log stamps are UTC; started_at is ISO with offset — compare in UTC."""
    if not started_iso or not triggers:
        return "10m poll"
    try:
        start_utc = datetime.fromisoformat(started_iso).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return "10m poll"
    best_dt, best_ev, best_delta = None, None, None
    PRIO = {"push": 0, "issues": 1, "pull_request": 2, "issue_comment": 3}
    for dt, ev in triggers:
        delta = abs((start_utc - dt).total_seconds())
        if delta > 10:
            continue
        better = (best_delta is None
                  or (PRIO.get(ev, 9), delta) < (PRIO.get(best_ev, 9), best_delta))
        if better:
            best_dt, best_ev, best_delta = dt, ev, delta
    return f"webhook:{best_ev}" if best_dt else "10m poll"


def pt_of(iso_or_none, fallback_dt=None):
    """Return a timezone-AWARE Pacific datetime, never a naive one.

    Every caller feeds the result into iso_stamp(), which refuses naive
    datetimes on purpose: a naive datetime renders without a UTC offset,
    which is exactly the ambiguity this module exists to avoid."""
    if iso_or_none:
        try:
            return datetime.fromisoformat(iso_or_none).astimezone(PT)
        except Exception:
            pass
    if fallback_dt is not None:
        # astimezone() on a naive fallback would silently re-interpret it in
        # the SYSTEM's zone, not Pacific; attaching PT keeps the wall clock.
        return fallback_dt if fallback_dt.tzinfo else fallback_dt.replace(tzinfo=PT)
    return datetime.now(PT)


def iso_stamp(dt):
    """Render a datetime as ISO-8601 with an explicit UTC offset.

    The ledger's "time" column is the human-visible truth line, and the live
    worker prompt already mandates this exact shape for its own lines:
    ISO-8601 with an explicit offset, taken from
    `TZ=America/Los_Angeles date -Iseconds`, e.g. "2026-11-08T22:30:50-08:00".
    The sync script used to write a bare local wall clock, so one column
    carried two formats — and during the DST fall-back hour the bare clock
    is genuinely ambiguous (the same wall clock happens twice, an hour apart
    in real time).

    isoformat() on a tz-aware datetime yields precisely that shape: seconds
    precision, no microseconds, and an offset with a colon. A naive datetime
    is rejected rather than rendered, because it would silently produce a
    string with no offset at all."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("refusing to stamp a naive datetime: %r" % (dt,))
    return dt.replace(microsecond=0).isoformat()


def out_file_for(finished_at, out_dir=None):
    """The output file belonging to THIS row, or None.

    The old code used files[-1] — the newest file in the directory — which
    belongs to a different run whenever runs overlap. Instead: prefer the file
    named after this row's finish time; otherwise accept the closest one
    within OUT_MATCH_WINDOW seconds, so an unrelated run's file is never
    attributed to this row."""
    out_dir = OUT_DIR if out_dir is None else out_dir
    if not finished_at:
        return None
    dt = pt_of(finished_at)
    exact = os.path.join(out_dir, dt.strftime(OUT_NAME_FMT) + ".md")
    if os.path.exists(exact):
        return exact
    best, best_delta = None, None
    for path in glob.glob(os.path.join(out_dir, "*.md")):
        try:
            stamp = datetime.strptime(os.path.basename(path)[:-3],
                                      OUT_NAME_FMT).replace(tzinfo=PT)
        except ValueError:
            continue
        delta = abs((stamp - dt).total_seconds())
        if delta > OUT_MATCH_WINDOW:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = path, delta
    return best


def outcome_line(finished_at, error, out_dir=None):
    if error:
        return f"ERROR: {error[:110]}"
    path = out_file_for(finished_at, out_dir=out_dir)
    if path:
        src = open(path).read()
        resp = src.split("## Response")[-1].strip()
        if "GITOPS-QUIET" in resp[:50]:
            return "GITOPS-QUIET"
        return resp.split("\n")[0][:120]
    return "finished (no output captured)"


def load_state(path=None, pending_path=None):
    """Read the high-water mark and the in-flight set.

    STATE_FILE stays a bare integer (the format the previous version of this
    script wrote), so a rollback to that version still reads a valid mark.
    The in-flight rowids live in the PENDING_FILE sidecar.

    A state file that holds JSON instead of an integer is accepted too (a
    forward-format file), and is read leniently.
    """
    path = STATE_FILE if path is None else path
    pending_path = PENDING_FILE if pending_path is None else pending_path
    try:
        raw = open(path).read().strip()
    except FileNotFoundError:
        raw = ""
    last_rowid, legacy = 0, False
    try:
        last_rowid = int(raw)
    except ValueError:
        # Maybe a JSON state file (forward format). Read it leniently.
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                last_rowid = int(data.get("last_rowid") or 0)
        except (ValueError, TypeError):
            last_rowid = 0
    seen = []
    try:
        data = json.loads(open(pending_path).read().strip() or "[]")
        if isinstance(data, list):
            seen = [int(r) for r in data if str(r).lstrip("-").isdigit()]
    except (FileNotFoundError, ValueError, TypeError):
        seen = []
    return {"last_rowid": last_rowid, "seen_start": seen, "legacy": legacy}


def save_state(state, path=None, pending_path=None):
    path = STATE_FILE if path is None else path
    pending_path = PENDING_FILE if pending_path is None else pending_path
    with open(path, "w") as fh:
        fh.write(str(int(state.get("last_rowid") or 0)))
    payload = sorted(int(r) for r in (state.get("seen_start") or []))
    with open(pending_path, "w") as fh:
        fh.write(json.dumps(payload))


def build_lines(rows, state, out_dir=None):
    """Turn candidate rows into ledger lines plus the next state.

    A row gets its 'start' line exactly once, backdated to its start; its
    'done' line exactly once, when it is no longer 'running', at which point
    its rowid leaves seen_start. last_rowid advances only past rows that
    received a 'done' line, so an unfinished row is never skipped; seen_start
    keeps only in-flight rows, so it cannot grow without bound."""
    last_rowid = int(state.get("last_rowid") or 0)
    seen_start = set(int(r) for r in (state.get("seen_start") or []))
    lines, new_max = [], last_rowid
    for rowid, started, finished, status, error in rows:
        t0 = pt_of(started)
        trig = attribute_trigger(started)
        if rowid not in seen_start:
            seen_start.add(rowid)
            lines.append({"time": iso_stamp(t0),
                          "trigger": trig, "outcome": "▶ started", "status": "start"})
        if status == "running":
            continue  # still in flight: the 'done' line waits for a later pass
        t1 = pt_of(finished, t0)  # backdate the start if we never saw it live
        lines.append({"time": iso_stamp(t1),
                      "trigger": trig,
                      "outcome": outcome_line(finished, error, out_dir=out_dir),
                      "status": "done"})
        seen_start.discard(rowid)
        new_max = max(new_max, rowid)
    # a row that vanished from executions.db must not keep seen_start alive
    seen_start &= {r[0] for r in rows}
    return lines, {"last_rowid": new_max, "seen_start": sorted(seen_start)}


def sync_pass(conn, state, ledger=None, state_path=None, out_dir=None, job_id=None,
              pending_path=None):
    """One sync pass: read state, write ledger lines, write the new state."""
    ledger = LEDGER if ledger is None else ledger
    state_path = STATE_FILE if state_path is None else state_path
    pending_path = PENDING_FILE if pending_path is None else pending_path
    out_dir = OUT_DIR if out_dir is None else out_dir
    job_id = JOB_ID if job_id is None else job_id
    state = dict(state)
    # The old writer advanced last_rowid past rows it only ever saw 'running',
    # and it had written a 'start' line for them. On the first pass after an
    # upgrade, seed seen_start with those in-flight rows so no 'start' line is
    # written twice, while their 'done' line still gets written when they
    # finish. This seeding is idempotent and only applies while the pending
    # sidecar is empty.
    if not state.get("seen_start"):
        in_flight = {r[0] for r in conn.execute(
            "SELECT rowid FROM executions WHERE job_id=? AND status='running' "
            "AND rowid<=?", (job_id, int(state["last_rowid"])))}
        state["seen_start"] = sorted(in_flight)
    # A row is a candidate when it is new (rowid > last_rowid), still
    # 'running', or still pending (in seen_start — its 'done' line has not
    # been written yet). The old query (rowid > last_rowid only) skipped every
    # run first seen while still running, so its 'done' line was never written.
    pending = sorted(int(r) for r in (state.get("seen_start") or []))
    sql = ("SELECT rowid, started_at, finished_at, status, error FROM executions "
           "WHERE job_id=? AND (rowid > ? OR status='running'")
    params = [job_id, int(state["last_rowid"])]
    if pending:
        sql += " OR rowid IN (%s)" % ",".join("?" * len(pending))
        params.extend(pending)
    sql += ") ORDER BY rowid"
    rows = conn.execute(sql, params).fetchall()
    lines, new_state = build_lines(rows, state, out_dir=out_dir)
    if lines:
        with open(ledger, "a") as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")
    save_state(new_state, state_path, pending_path)
    return lines, new_state


def main():
    global triggers
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    _repair_ledger()
    try:
        with open(GATEWAY_LOG, errors="replace") as fh:
            for line in fh:
                m = TRIGGER_RE.match(line)
                if m:
                    triggers.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                                     m.group(2)))
    except FileNotFoundError:
        pass
    conn = sqlite3.connect(EXEC_DB)
    new_lines, _new_state = sync_pass(conn, load_state())

    # --- auto-recovery: if a run failed on the portal credential (expires hourly,
    # refresh can transiently fail), re-fire the worker once the token is usable.
    # Cooldown 6 min to avoid loops; retry fires only when a NEW credential failure
    # was observed in this pass.
    cred_failures = [l for l in new_lines
                     if l.get("status") == "done" and "credential missing" in l.get("outcome", "")]
    fired = False
    if cred_failures:
        now = time.time()
        last = 0
        if os.path.exists(CRED_MARK):
            try:
                last = float(open(CRED_MARK).read().strip() or 0)
            except ValueError:
                last = 0
        if now - last > 360:
            # probe: is the credential usable again? (cheap models endpoint check)
            probe = subprocess.run(
                ["bash", "-c",
                 "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy "
                 "-u ALL_PROXY -u all_proxy python3 - <<'EOF'\n"
                 "import json, urllib.request\n"
                 "key = [l.split('=',1)[1].strip() for l in open('/home/dan.rohan/.hermes/.env') if l.startswith('NOUS_API_KEY=')][0]\n"
                 "req = urllib.request.Request('https://inference-api.nousresearch.com/v1/models')\n"
                 "req.add_header('Authorization', f'Bearer {key}')\n"
                 "urllib.request.urlopen(req, timeout=15)\n"
                 "print('ok')\n"
                 "EOF"],
                capture_output=True, text=True, timeout=40)
            if probe.returncode == 0 and "ok" in probe.stdout:
                subprocess.run(["hermes", "cron", "run", JOB_ID], capture_output=True, timeout=60)
                with open(LEDGER, "a") as fh:
                    fh.write(json.dumps({"time": iso_stamp(datetime.now(PT)),
                                         "trigger": "auto-recovery",
                                         "outcome": f"credential failure detected ({len(cred_failures)} run(s)); credential usable again — worker re-fired",
                                         "status": "done"}) + "\n")
                fired = True
        with open(CRED_MARK, "w") as fh:
            fh.write(str(now))
    print(f"ledger synced: {len(new_lines)} new lines" + ("; auto-recovery fired" if fired else "")
          if new_lines else ("ledger clean" + ("; auto-recovery fired" if fired else "")))


if __name__ == "__main__":
    main()
