#!/usr/bin/env python3
"""Ledger sync: mirror gitops-github-worker run starts/finishes into the
human-visible ledger (~/mockup-previews/gitops/runs.jsonl).

Runs every 2 min as a no_agent cron job. State is the highest
executions.rowid already recorded; each new run appends up to two lines:
  ▶ started — <trigger>        (trigger attributed from gateway.log)
  outcome line                 (from the run's output file)
"""
import json
import os
import sqlite3
import re
import glob
import time
import subprocess
from datetime import datetime, timezone, timedelta

PT = timezone(timedelta(hours=-7))
STATE_FILE = "/home/dan.rohan/mockup-previews/gitops/.ledger_state"
LEDGER = "/home/dan.rohan/mockup-previews/gitops/runs.jsonl"
JOB_ID = "f42f6ffda76b"
OUT_DIR = f"/home/dan.rohan/.hermes/cron/output/{JOB_ID}"
GATEWAY_LOG = "/home/dan.rohan/.hermes/logs/gateway.log"
TRIGGER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO gateway\.platforms\.webhook: "
    r"\[webhook\] cron-trigger event=(\S+) route=\S+ job=" + JOB_ID)

os.makedirs(os.path.dirname(LEDGER), exist_ok=True)

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

_repair_ledger()
last_rowid = 0
if os.path.exists(STATE_FILE):
    try:
        last_rowid = int(open(STATE_FILE).read().strip() or 0)
    except ValueError:
        last_rowid = 0

# --- build trigger index from gateway.log (webhook fires; log stamps are UTC)
triggers = []  # (datetime_utc_naive, event)
try:
    with open(GATEWAY_LOG, errors="replace") as fh:
        for line in fh:
            m = TRIGGER_RE.match(line)
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                triggers.append((dt, m.group(2)))
except FileNotFoundError:
    pass

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
        better = (
            best_delta is None
            or (PRIO.get(ev, 9), delta) < (PRIO.get(best_ev, 9), best_delta)
        )
        if better:
            best_dt, best_ev, best_delta = dt, ev, delta
    return f"webhook:{best_ev}" if best_dt else "10m poll"

def outcome_line(finished_at, error):
    if error:
        return f"ERROR: {error[:110]}"
    files = sorted(glob.glob(f"{OUT_DIR}/*.md"))
    if finished_at and files:
        src = open(files[-1]).read()
        resp = src.split("## Response")[-1].strip()
        if "GITOPS-QUIET" in resp[:50]:
            return "GITOPS-QUIET"
        return resp.split("\n")[0][:120]
    return "finished (no output captured)"

db = sqlite3.connect("/home/dan.rohan/.hermes/cron/executions.db")
rows = db.execute(
    "SELECT rowid, started_at, finished_at, status, error FROM executions "
    "WHERE job_id=? AND rowid>? ORDER BY rowid", (JOB_ID, last_rowid)).fetchall()

def pt_of(iso_or_none, fallback_dt=None):
    if not iso_or_none:
        return fallback_dt or datetime.now(PT)
    try:
        return datetime.fromisoformat(iso_or_none).astimezone(PT)
    except Exception:
        return fallback_dt or datetime.now(PT)

new_lines = []
new_max = last_rowid
for rowid, started, finished, status, error in rows:
    new_max = max(new_max, rowid)
    t0 = pt_of(started)
    trig = attribute_trigger(started)
    if status == "running":
        new_lines.append({"time": t0.strftime("%Y-%m-%d %H:%M:%S"),
                          "trigger": trig, "outcome": "▶ started", "status": "start"})
    else:
        # backdate the start if we never saw it live
        new_lines.append({"time": t0.strftime("%Y-%m-%d %H:%M:%S"),
                          "trigger": trig, "outcome": "▶ started", "status": "start"})
        t1 = pt_of(finished, t0)
        new_lines.append({"time": t1.strftime("%Y-%m-%d %H:%M:%S"),
                          "trigger": trig,
                          "outcome": outcome_line(finished, error),
                          "status": "done"})

if new_lines:
    with open(LEDGER, "a") as fh:
        for ln in new_lines:
            fh.write(json.dumps(ln) + "\n")
with open(STATE_FILE, "w") as fh:
    fh.write(str(new_max))

# --- auto-recovery: if a run failed on the portal credential (expires hourly,
# refresh can transiently fail), re-fire the worker once the token is usable.
# Cooldown 6 min to avoid loops; retry fires only when a NEW credential failure
# was observed in this pass.
CRED_MARK = "/home/dan.rohan/mockup-previews/gitops/.cred_retry_state"
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
                fh.write(json.dumps({"time": datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S"),
                                     "trigger": "auto-recovery",
                                     "outcome": f"credential failure detected ({len(cred_failures)} run(s)); credential usable again — worker re-fired",
                                     "status": "done"}) + "\n")
            fired = True
    with open(CRED_MARK, "w") as fh:
        fh.write(str(now))
print(f"ledger synced: {len(new_lines)} new lines" + ("; auto-recovery fired" if fired else "")
      if new_lines else ("ledger clean" + ("; auto-recovery fired" if fired else "")))
