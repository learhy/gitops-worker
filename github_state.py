#!/usr/bin/env python3
"""Fresh GitHub state collection for merge decisions.

Shapes the raw ``gh api`` output into the structured state dict that
``merge_decision.decide_merge`` consumes. Every call re-fetches from GitHub —
nothing here reads cache, and the worker prompt requires this module to run
immediately before any merge call.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Dict, List

# Closes / closes / fix / fixes / fixed / resolves / resolved — with issue refs.
LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolv(?:e[sd])?)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)

CHECK_RUNS_PER_PAGE = 100
COMMENTS_PER_PAGE = 100


class GithubStateError(Exception):
    """A fresh-state fetch failed. The caller must treat this as 'do not merge'."""


def strip_proxy_env() -> Dict[str, str]:
    """GitHub traffic must not go through the host's HTTP proxies."""
    return {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def default_runner(args: List[str]) -> Dict:
    """Run ``gh api`` and return the parsed JSON body."""
    cmd = ["gh", "api"] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, env=strip_proxy_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GithubStateError("gh api failed: %s" % exc) from exc
    if proc.returncode != 0:
        raise GithubStateError(
            "gh api %s failed (exit %s): %s"
            % (" ".join(args), proc.returncode, (proc.stderr or "").strip()[:400])
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GithubStateError("gh api %s returned non-JSON output" % " ".join(args)) from exc


def parse_linked_issues(pr_body: str) -> List[int]:
    """Issue numbers referenced by closing keywords in the PR body."""
    seen, ordered = set(), []
    for match in LINKED_ISSUE_RE.finditer(pr_body or ""):
        number = int(match.group(1))
        if number not in seen:
            seen.add(number)
            ordered.append(number)
    return ordered


def _comment(c: Dict) -> Dict:
    return {
        "author": (c.get("user") or {}).get("login", ""),
        "body": c.get("body") or "",
        "created_at": c.get("created_at") or "",
    }


def _review(r: Dict) -> Dict:
    return {
        "author": (r.get("user") or {}).get("login", ""),
        "state": r.get("state") or "",
        "commit_id": r.get("commit_id") or "",
        "submitted_at": r.get("submitted_at") or "",
    }


def fetch_state(repo: str, pr_number: int, runner: Callable = default_runner) -> Dict:
    """Fetch PR + linked-issue state fresh from GitHub, shaped for decide_merge.

    Raises GithubStateError on any failure — callers must treat that as HOLD.
    """
    try:
        pr = runner(["repos/%s/pulls/%d" % (repo, pr_number)])
        pr_comments = runner([
            "repos/%s/issues/%d/comments?per_page=%d" % (repo, pr_number, COMMENTS_PER_PAGE),
        ]) or []
        pr_reviews = runner([
            "repos/%s/pulls/%d/reviews?per_page=%d" % (repo, pr_number, COMMENTS_PER_PAGE),
        ]) or []
        head_sha = (pr.get("head") or {}).get("sha") or ""
        checks = runner([
            "repos/%s/commits/%s/check-runs?per_page=%d" % (repo, head_sha, CHECK_RUNS_PER_PAGE),
        ]) or {}

        linked: List[Dict] = []
        for number in parse_linked_issues(pr.get("body") or ""):
            issue = runner(["repos/%s/issues/%d" % (repo, number)])
            issue_comments = runner([
                "repos/%s/issues/%d/comments?per_page=%d" % (repo, number, COMMENTS_PER_PAGE),
            ]) or []
            linked.append({
                "number": number,
                "body": issue.get("body") or "",
                "labels": [l.get("name") for l in (issue.get("labels") or []) if l.get("name")],
                "comments": [_comment(c) for c in issue_comments],
            })
    except GithubStateError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrap anything unexpected
        raise GithubStateError("unexpected error fetching state: %s" % exc) from exc

    return {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "pr_labels": [l.get("name") for l in (pr.get("labels") or []) if l.get("name")],
        "pr_comments": [_comment(c) for c in pr_comments],
        "pr_reviews": [_review(r) for r in pr_reviews],
        "mergeable_state": pr.get("mergeable_state") or "unknown",
        "check_runs": [
            {"name": cr.get("name"), "conclusion": cr.get("conclusion")}
            for cr in (checks.get("check_runs") or [])
        ],
        "linked_issues": linked,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


class GhMergeClient:
    """Merge client backed by ``gh api``. Raises MergeRejected on 405/403."""

    def merge(self, repo: str, pr_number: int, expected_head_sha: str) -> Dict:
        cmd = [
            "gh", "api", "--method", "POST",
            "repos/%s/pulls/%d/merge" % (repo, pr_number),
            "-f", "merge_method=merge",
            "-f", "sha=%s" % expected_head_sha,  # GitHub's expected-head-SHA field
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, env=strip_proxy_env(),
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout or "{}")
        stderr = (proc.stderr or "").strip()
        status_match = re.search(r"HTTP (\d{3})", stderr)
        status = int(status_match.group(1)) if status_match else 0
        if status in (403, 405):
            # 405: not mergeable / protected branch; 403: forbidden (branch rules).
            import merge_decision
            raise merge_decision.MergeRejected(status, stderr[:400])
        raise GithubStateError(
            "merge call failed (HTTP %s): %s" % (status or proc.returncode, stderr[:400])
        )
