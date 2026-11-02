#!/usr/bin/env python3
"""GitOps GitHub webhook filter for the project-stdio-orchestrator app.

Reads the webhook payload JSON on stdin. Emits transformed JSON to pass
through, or nothing / [SILENT] / nonzero exit to ignore the event.

Conventions:
  - Only repos in ~/.hermes/data/gitops-repos.txt are processed.
  - @cole mention (issue body or comment body)     -> directive "discuss":
    engage in discussion — think through ideas, give advice, answer questions.
    Works with or without the agent:cole label. Never labels, never plans, never opens PRs.
  - issue carrying both "agent:cole" and "methods" (opened, labeled or
    reopened), unless "cole:hold" is on it         -> instruction "build-for-review":
    build the PR without waiting for a "go"; the merge gate holds it until
    learhy approves the PR's current head (Claude's review)
  - issues.labeled with label "agent:cole"         -> instruction "plan"
  - issue_comment.created on an issue labeled "agent:cole":
      body == go / 👍 / lgtm / ship it             -> instruction "execute"
      anything else with the label present          -> ignored (use @cole to discuss)
  - pull_request_review.submitted by learhy (approved / changes_requested)
                                                   -> instruction "review-submitted"
    (run the merge gate / review loop on that PR now)
  - All other issues / pull_request / push events are ignored.
"""
import json
import os
import re
import sys

REPOS_FILE = os.path.expanduser("~/.hermes/data/gitops-repos.txt")
DEPLOY_FILE = os.path.expanduser("~/.hermes/data/gitops-deploy.json")
WORK_LABEL = "agent:cole"
GAM_LABEL = "cole:go-and-merge"
REVIEW_LABEL = "methods"   # built on sight, merged only after learhy approval
HOLD_LABEL = "cole:hold"   # keeps a methods issue in plan/discuss mode
GO_RE = re.compile(r"^\s*(go( and merge|-and-merge)?|👍|lgtm|ship it)\s*[.!]*\s*$", re.IGNORECASE)
GAM_BODY_RE = re.compile(r"\bgo-and-merge\b|\bgo and merge\b", re.IGNORECASE)
MENTION_RE = re.compile(r"@(cole|project-stdio-orchestrator)\b", re.IGNORECASE)

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)

repo = (payload.get("repository") or {}).get("full_name", "")
try:
    allowed = [ln.strip() for ln in open(REPOS_FILE) if ln.strip() and not ln.startswith("#")]
except FileNotFoundError:
    allowed = []
if repo not in allowed:
    sys.exit(0)

labels = [l.get("name") for l in (payload.get("issue") or {}).get("labels", [])]
action = payload.get("action", "")
out = dict(payload)

if "issue" in payload and "comment" not in payload:  # issues event
    body = (payload.get("issue") or {}).get("body", "") or ""
    # cole:go-and-merge label (at creation or applied later) = zero-comment
    # fast lane: build + self-merge on sight, no plan confirmation needed.
    if action == "labeled" and payload.get("label", {}).get("name") == GAM_LABEL:
        out["hermes_instruction"] = "execute-merge"
        print(json.dumps(out))
        sys.exit(0)
    if action in ("opened", "edited") and (GAM_LABEL in labels or GAM_BODY_RE.search(body)):
        out["hermes_instruction"] = "execute-merge"
        print(json.dumps(out))
        sys.exit(0)
    # agent:cole + methods = build-for-review lane: build on sight, no "go"
    # needed; the merge gate holds the PR until learhy approves its head.
    if (action in ("opened", "labeled", "reopened")
            and WORK_LABEL in labels and REVIEW_LABEL in labels and HOLD_LABEL not in labels):
        out["hermes_instruction"] = "build-for-review"
        print(json.dumps(out))
        sys.exit(0)
    if action == "labeled" and payload.get("label", {}).get("name") == WORK_LABEL:
        out["hermes_instruction"] = "plan"
        print(json.dumps(out))
        sys.exit(0)
    # issue created with agent:cole already attached (label picker at creation)
    if action == "opened" and WORK_LABEL in labels:
        out["hermes_instruction"] = "plan"
        print(json.dumps(out))
        sys.exit(0)
    # cole:deploy-now = Dan's critical-fix override: deploy immediately,
    # bypassing PR-batch deferral. One-shot: worker clears the label after deploying.
    if action == "labeled" and payload.get("label", {}).get("name") == "cole:deploy-now":
        try:
            deploy = json.load(open(DEPLOY_FILE))
        except Exception:
            deploy = {}
        entry = deploy.get(repo)
        if entry:
            out["hermes_instruction"] = "deploy-now"
            out["hermes_deploy"] = entry
            print(json.dumps(out))
            sys.exit(0)
        sys.exit(1)  # no deploy entry for this repo: surface as filter error
    if action in ("opened", "edited", "reopened") and MENTION_RE.search(body):
        out["hermes_instruction"] = "discuss"
        print(json.dumps(out))
        sys.exit(0)
    # issues reopened/edited while already labeled: only plan if not yet planned
    if action in ("reopened",) and WORK_LABEL in labels:
        out["hermes_instruction"] = "plan"
        print(json.dumps(out))
        sys.exit(0)
    sys.exit(0)

if "issue" in payload and "comment" in payload:  # issue_comment event
    body = (payload.get("comment") or {}).get("body", "") or ""
    # Build directives route as execute regardless of mention — a comment that
    # asks for the artifact ("ship it as a PR @cole") is a go, not a chat opener.
    # go-and-merge / "go and merge" = execute + self-merge + deploy in one shot.
    if WORK_LABEL in labels and GO_RE.match(body):
        out["hermes_instruction"] = "execute-merge" if "merge" in body.lower() else "execute"
        print(json.dumps(out))
        sys.exit(0)
    if re.search(r"\b(ship it|ship as|open (a|the) pr|build it|implement (it|this|that)|go ahead|go-and-merge|go and merge)\b", body, re.IGNORECASE):
        out["hermes_instruction"] = "execute-merge" if "merge" in body.lower() else "execute"
        print(json.dumps(out))
        sys.exit(0)
    # @cole mention (without a build directive) ALWAYS means discussion, label or not
    if MENTION_RE.search(body):
        out["hermes_instruction"] = "discuss"
        print(json.dumps(out))
        sys.exit(0)
    # Any other comment on a labeled issue: wake the worker to run its normal
    # state-derivation cycle (catches loosely-phrased approvals like "Go, great").
    if WORK_LABEL in labels:
        out["hermes_instruction"] = "cycle"
        print(json.dumps(out))
        sys.exit(0)
    sys.exit(0)

# pull_request_review event: learhy approving (or requesting changes on) a
# PR is exactly when the merge gate's answer changes, so wake the worker to
# run the gate on that PR now instead of on the next poll. Needs the GitHub
# App subscribed to "Pull request review" events.
if "review" in payload and "pull_request" in payload:
    review = payload.get("review") or {}
    reviewer = (review.get("user") or {}).get("login", "")
    state = (review.get("state") or "").lower()
    if (action == "submitted" and reviewer == "learhy"
            and state in ("approved", "changes_requested")):
        out["hermes_instruction"] = "review-submitted"
        out["hermes_pr_number"] = (payload.get("pull_request") or {}).get("number")
        print(json.dumps(out))
        sys.exit(0)
    sys.exit(0)

if "pull_request" in payload:  # pull_request event
    sys.exit(0)

if "push" in payload or ("ref" in payload and "commits" in payload):  # push event
    try:
        deploy = json.load(open(DEPLOY_FILE))
    except Exception:
        deploy = {}
    entry = deploy.get(repo)
    if not entry or payload.get("ref") != f"refs/heads/{entry.get('branch')}":
        sys.exit(0)
    out["hermes_instruction"] = "deploy"
    out["hermes_deploy"] = entry
    print(json.dumps(out))
    sys.exit(0)

sys.exit(0)
