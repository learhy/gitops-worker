#!/usr/bin/env python3
"""Merge-decision gate for the GitOps worker ("Cole").

Root-cause fix for the 2026-11-01 incident: PR #228 was merged two seconds
after it opened, using state cached when issue #221 was picked up — ignoring
learhy's 07:41 label removal and 07:42 "Hold: do not merge" comment. This
module forces the merge decision to be made AT MERGE TIME from fresh GitHub
reads, and never from pickup-cached labels or comments.

Three parts:

- ``decide_merge(state)`` — pure decision logic over structured GitHub state
  (no network, fully unit-testable). Returns
  ``{"decision": "MERGE"|"HOLD", "reason": str, "audit_line": str, ...}``.

- ``github_state.fetch_state(repo, pr)`` (separate module) — collects that
  state with fresh ``gh api`` calls immediately before the merge.

- ``perform_merge(client, repo, pr, expected_head_sha)`` — performs the merge
  with head-SHA protection so a push between check and merge cannot slip
  through. A GitHub-side rejection (403/405/409/422, e.g. branch protection
  or the head moved) maps to HOLD: post the held comment and wait. NEVER
  bypass, retry or use admin.

Merge rule (Dan's change order, 2026-11-01; review-lane amendments 2026-11-02):

  MERGE iff:
    1. No hold signal, where a hold signal is any of:
       - label ``cole:hold`` or ``methods`` on the PR or a linked issue
         (released by a learhy approval on the PR's CURRENT head SHA —
         that is how Claude's review approves methods-held PRs);
       - "do not merge" / "don't merge" (case-insensitive) in a linked
         issue's BODY. The automated reviews open every methods issue with
         "**Do not merge.** Open the PR and stop. Claude will review it
         before it merges." — a review hold, so it is released the same
         way as the methods label: a learhy approval on the CURRENT head,
         or a learhy "release hold" / "ok to merge" comment;
       - "do not merge" in any COMMENT by learhy on the PR or a linked
         issue — released ONLY by a later learhy comment saying "release
         hold" or "ok to merge" (an approval does not release it: a hold
         comment is a deliberate intervention, the #228 incident);
         a negated release ("not ok to merge") releases nothing;
       - a CHANGES_REQUESTED review from learhy with no newer APPROVE
         from learhy.
    2. AND one of:
       (a) ``cole:go-and-merge`` is on EVERY linked issue right now, OR
       (b) learhy submitted an APPROVE review on the PR's CURRENT head SHA
           (an approval on an older commit does not count).
    3. AND ``mergeable_state`` is ``clean`` and no check run has failed.

CLI:
    python3 merge_decision.py --repo OWNER/NAME --pr N [--merge]

    Prints one JSON object on stdout:
      {"decision": ..., "reason": ..., "audit_line": ..., "merged": ...}
    Exit codes: 0 = MERGE, 1 = HOLD, 2 = could not fetch/decide (treated as
    HOLD — when in doubt, never merge). With ``--merge``, a MERGE decision is
    executed via ``perform_merge`` with the exact head SHA that was checked;
    a GitHub-side rejection downgrades the result to HOLD.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from github_state import (  # noqa: F401 (CLI use; MergeRejected re-exported)
    fetch_state, GhMergeClient, GithubStateError, MergeRejected,
)

HOLD_LABELS = ("cole:hold", "methods")
GO_LABEL = "cole:go-and-merge"
AUTHORITY = "learhy"  # the only user whose comments/reviews carry hold/release signals

DO_NOT_MERGE_RE = re.compile(r"\b(?:do\s*not|don['’]?t|do-not)[\s-]*merge", re.IGNORECASE)
RELEASE_RE = re.compile(r"release\s+(?:the\s+)?hold|ok(?:ay)?\s+to\s+merge", re.IGNORECASE)
# A release phrase preceded by one of these is not a release ("not ok to merge").
NEGATION_RE = re.compile(r"(?:\bnot|\bnever|\bno|n['’]t)\s*(?:yet\s+)?$", re.IGNORECASE)

FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


# --------------------------------------------------------------------------
# Decision input shape (what github_state.fetch_state produces)
#
# {
#   "pr_number": int,
#   "head_sha": str,                       # 40-hex current head
#   "pr_labels": ["methods", ...],
#   "pr_comments": [{"author", "body", "created_at"}, ...],
#   "pr_reviews":  [{"author", "state", "commit_id", "submitted_at"}, ...],
#   "mergeable_state": "clean" | "dirty" | "blocked" | "unstable" | ...,
#   "check_runs": [{"name": str, "conclusion": str|None}, ...],
#   "linked_issues": [
#       {"number": int, "body": str, "labels": [str],
#        "comments": [{"author", "body", "created_at"}, ...]},
#   ],
# }
# --------------------------------------------------------------------------


def is_release(text: str) -> bool:
    """True if ``text`` contains a release phrase that isn't negated."""
    for match in RELEASE_RE.finditer(text or ""):
        if not NEGATION_RE.search((text or "")[max(0, match.start() - 24):match.start()]):
            return True
    return False


def _short(sha: Optional[str]) -> str:
    return (sha or "unknown")[:12]


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _comments_at(issue_or_pr: Dict) -> List[Dict]:
    return issue_or_pr.get("comments", [])


def _collect_learhy_comment_events(state: Dict) -> List[tuple]:
    """All learhy comments on the PR and linked issues, chronological.

    Returns [(created_at, where, comment_dict)] sorted by created_at.
    """
    events = []
    pr_where = "PR #%s" % state["pr_number"]
    for c in state.get("pr_comments", []):
        events.append((c.get("created_at", ""), pr_where, c))
    for issue in state.get("linked_issues", []):
        where = "#%s" % issue["number"]
        for c in _comments_at(issue):
            events.append((c.get("created_at", ""), where, c))
    events.sort(key=lambda e: e[0])
    return events


def _learhy_released(state: Dict) -> bool:
    """True if any learhy comment (PR or linked issue) is a non-negated
    release that doesn't itself re-assert 'do not merge'."""
    for _ts, _where, comment in _collect_learhy_comment_events(state):
        if comment.get("author") != AUTHORITY:
            continue
        body = comment.get("body") or ""
        if is_release(body) and not DO_NOT_MERGE_RE.search(body):
            return True
    return False


def _classified_holds(state: Dict):
    """Hold signals in the freshly-fetched state, split by how they release.

    Returns (review_holds, absolute_holds):
      - review holds (hold labels, 'do not merge' in a linked issue body):
        released by a learhy APPROVE on the current head SHA;
      - absolute holds (unreleased learhy 'do not merge' comment,
        CHANGES_REQUESTED): an approval does not release them.
    """
    review: List[str] = []
    absolute: List[str] = []

    # 1. Hold labels on the PR or any linked issue.
    for label in HOLD_LABELS:
        if label in (state.get("pr_labels") or []):
            review.append("%s label on PR #%s" % (label, state["pr_number"]))
    for issue in state.get("linked_issues", []):
        for label in HOLD_LABELS:
            if label in (issue.get("labels") or []):
                review.append("%s label on #%s" % (label, issue["number"]))

    # 2. "do not merge" in a linked issue body (any author). The automated
    #    reviews write it into every methods issue as a review hold; a
    #    learhy release comment clears it outright, an approval clears it
    #    in decide_merge.
    if not _learhy_released(state):
        for issue in state.get("linked_issues", []):
            if DO_NOT_MERGE_RE.search(issue.get("body") or ""):
                review.append("do-not-merge in #%s body, not released" % issue["number"])

    # 2b. "do not merge" in a learhy comment on the PR or a linked issue,
    #     unless a later learhy comment releases it.
    last_dnm = None
    for _ts, where, comment in _collect_learhy_comment_events(state):
        if comment.get("author") != AUTHORITY:
            continue  # only learhy's comments carry hold/release weight
        body = comment.get("body") or ""
        if DO_NOT_MERGE_RE.search(body):
            last_dnm = where  # a re-assertion re-arms the hold
        elif is_release(body) and last_dnm is not None:
            last_dnm = None  # released
    if last_dnm:
        absolute.append("do-not-merge from learhy on %s, not released" % last_dnm)

    # 3. CHANGES_REQUESTED from learhy with no newer APPROVE from learhy.
    latest = None
    for review_ in state.get("pr_reviews", []):
        if review_.get("author") != AUTHORITY:
            continue
        if review_.get("state") not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        key = (review_.get("submitted_at") or "", review_.get("state"))
        if latest is None or key >= (latest.get("submitted_at") or "", latest.get("state")):
            latest = review_
    if latest and latest.get("state") == "CHANGES_REQUESTED":
        absolute.append("learhy CHANGES_REQUESTED with no newer learhy approval")

    return review, absolute


def _hold_signals(state: Dict) -> List[str]:
    """Every hold signal present in the freshly-fetched state."""
    review, absolute = _classified_holds(state)
    return review + absolute


def _current_head_approval(state: Dict, head_sha: str) -> Optional[str]:
    """Full SHA of learhy's latest APPROVE review on the CURRENT head, else None."""
    best = None
    for review in state.get("pr_reviews", []):
        if review.get("author") != AUTHORITY:
            continue
        if review.get("state") != "APPROVED":
            continue
        if (review.get("commit_id") or "") != (head_sha or ""):
            continue  # an approval on an older commit does not count
        key = review.get("submitted_at") or ""
        if best is None or key >= (best.get("submitted_at") or ""):
            best = review
    return (best or {}).get("commit_id")


def _failed_checks(state: Dict) -> List[str]:
    return [
        c.get("name") or "unnamed check"
        for c in (state.get("check_runs") or [])
        if (c.get("conclusion") or "").lower() in FAILED_CONCLUSIONS
    ]


def _all_labels(state: Dict) -> List[str]:
    labels = list(state.get("pr_labels") or [])
    for issue in state.get("linked_issues", []):
        labels.extend(issue.get("labels") or [])
    return labels


def build_audit_line(state: Dict, decision: str, reason: str,
                     approval_sha: Optional[str]) -> str:
    """One-line audit record: labels, hold signals, approval SHA, decision."""
    return _one_line(
        "merge-audit PR #{n} decision={dec} head={head} approval={appr} "
        "labels=[{labels}] holds=[{holds}] reason={reason}".format(
            n=state.get("pr_number"),
            dec=decision,
            head=_short(state.get("head_sha")),
            appr=_short(approval_sha) if approval_sha else "none",
            labels=", ".join(_all_labels(state)),
            holds="; ".join(_hold_signals(state)) or "none",
            reason=reason,
        )
    )


def _decision(state: Dict, decision: str, reason: str,
              approval_sha: Optional[str]) -> Dict:
    return {
        "decision": decision,
        "reason": reason,
        "audit_line": build_audit_line(state, decision, reason, approval_sha),
        "head_sha": state.get("head_sha"),
        "approval_sha": approval_sha,
    }


def held_comment(reason: str, now: Optional[datetime] = None) -> str:
    """The one-time 'Held for review' comment body (never repeated per poll)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "Held for review. Merge check at %s: %s." % (stamp, _one_line(reason))


def decide_merge(state: Dict) -> Dict:
    """Pure merge-rights decision from fresh, structured GitHub state."""
    head = state.get("head_sha") or ""
    approval_sha = _current_head_approval(state, head)
    review_holds, absolute_holds = _classified_holds(state)
    linked = state.get("linked_issues", [])
    # go-and-merge authorizes only if EVERY linked issue carries it: a PR
    # that also closes a plain-"go" issue must not ride another issue's label.
    go_label = bool(linked) and all(
        GO_LABEL in (issue.get("labels") or []) for issue in linked
    )

    # 1. Absolute holds: unreleased learhy do-not-merge / changes-requested.
    if absolute_holds:
        return _decision(state, "HOLD", absolute_holds[0], approval_sha)

    # 2. Review holds (hold labels, do-not-merge in an issue body) block
    #    path (a); path (b), a learhy approval on the current head, is
    #    their release.
    if review_holds and approval_sha is None:
        return _decision(state, "HOLD", review_holds[0], approval_sha)

    # 3. Authorization: go-and-merge label now, or current-head approval.
    if not (go_label or approval_sha):
        return _decision(
            state, "HOLD",
            "no cole:go-and-merge on every linked issue and no learhy approval on head %s"
            % _short(head),
            approval_sha,
        )

    # 4. Mergeability and checks, from the same fresh read.
    if state.get("mergeable_state") != "clean":
        return _decision(
            state, "HOLD",
            "mergeable_state is %s (need clean)" % (state.get("mergeable_state") or "unknown"),
            approval_sha,
        )
    failed = _failed_checks(state)
    if failed:
        return _decision(state, "HOLD", "check run failed: %s" % failed[0], approval_sha)

    return _decision(state, "MERGE", "authorized (%s), clean, checks green"
                     % ("learhy approval on head %s" % _short(approval_sha)
                        if approval_sha else "cole:go-and-merge on linked issue"),
                     approval_sha)


# --------------------------------------------------------------------------
# Merge execution (runtime property; unit-tested with a fake client)
# --------------------------------------------------------------------------

def perform_merge(client, repo: str, pr_number: int, expected_head_sha: str) -> Dict:
    """Merge with head-SHA protection; map a GitHub rejection to HOLD.

    ``client`` must expose ``merge(repo, pr_number, expected_head_sha)`` and
    raise ``MergeRejected`` on a GitHub-side refusal. On rejection we do NOT
    retry, bypass, or use admin: we return a HOLD decision with the text for
    the one-time held comment.
    """
    try:
        client.merge(repo, pr_number, expected_head_sha)
    except MergeRejected as exc:
        reason = "GitHub rejected the merge (HTTP %s): %s" % (exc.status, _one_line(exc.message))
        return {
            "merged": False,
            "decision": "HOLD",
            "reason": reason,
            "held_comment": held_comment(reason),
        }
    return {
        "merged": True,
        "decision": "MERGE",
        "reason": "merged with expected head %s" % _short(expected_head_sha),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Merge-rights decision at merge time.")
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--merge", action="store_true",
                        help="execute the merge when the decision is MERGE "
                             "(sends the checked head SHA as expected head)")
    args = parser.parse_args(argv)

    try:
        state = fetch_state(args.repo, args.pr)
    except Exception as exc:  # noqa: BLE001 — any failure must not merge
        out = {
            "decision": "HOLD",
            "reason": "could not fetch fresh GitHub state: %s" % _one_line(str(exc)),
            "audit_line": "merge-audit PR #%s decision=HOLD head=unknown approval=none "
                          "labels=[] holds=[] reason=state fetch failed" % args.pr,
        }
        print(json.dumps(out))
        return 2

    result = decide_merge(state)
    print(json.dumps(result))

    if args.merge and result["decision"] == "MERGE":
        try:
            outcome = perform_merge(GhMergeClient(), args.repo, args.pr, result["head_sha"])
        except Exception as exc:  # noqa: BLE001 — an unexpected error must not look like a merge
            reason = ("merge call errored, merge state unknown; re-read the PR before "
                      "doing anything else: %s" % _one_line(str(exc)))
            outcome = {"merged": False, "decision": "HOLD", "reason": reason,
                       "held_comment": held_comment(reason)}
            print(json.dumps(outcome))
            return 2
        print(json.dumps(outcome))
        return 0 if outcome["merged"] else 1

    return 0 if result["decision"] == "MERGE" else 1


if __name__ == "__main__":
    sys.exit(main())
