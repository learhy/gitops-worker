#!/usr/bin/env python3
"""Tests for the merge-decision gate (merge_decision.py + github_state parsing).

Pure in-memory tests: no network, no gh subprocess. Each case maps to one
line of Dan's change order (2026-11-01), plus the #221/#228 timeline replay.
"""
import unittest
from datetime import datetime, timezone

import github_state
import merge_decision
from merge_decision import (
    MergeRejected,
    decide_merge,
    held_comment,
    perform_merge,
)

LEARNY = "learhy"
HEAD = "a" * 40
OLD_SHA = "b" * 40
NEW_SHA = "c" * 40


def comment(author, body, at):
    return {"author": author, "body": body, "created_at": at}


def review(author, state, commit_id, at):
    return {"author": author, "state": state, "commit_id": commit_id, "submitted_at": at}


def issue(number=221, body="Do the thing", labels=None, comments=None):
    return {
        "number": number,
        "body": body,
        "labels": labels if labels is not None else ["agent:cole"],
        "comments": comments or [],
    }


def make_state(**overrides):
    """Baseline: clean PR with cole:go-and-merge on the linked issue."""
    state = {
        "pr_number": 228,
        "head_sha": HEAD,
        "pr_labels": [],
        "pr_comments": [],
        "pr_reviews": [],
        "mergeable_state": "clean",
        "check_runs": [{"name": "ci", "conclusion": "success"}],
        "linked_issues": [issue(labels=["agent:cole", "cole:go-and-merge"])],
    }
    state.update(overrides)
    return state


def iso(hour, minute, second=0):
    return datetime(2026, 11, 1, hour, minute, second, tzinfo=timezone.utc).isoformat()


class GoAndMergeRemovedBeforePrOpens(unittest.TestCase):
    """Case 1: go-and-merge present at pickup, removed before the PR opens."""

    def test_holds(self):
        state = make_state(linked_issues=[issue(labels=["agent:cole"])])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("no cole:go-and-merge", decision["reason"])
        self.assertIn("no learhy approval on head", decision["reason"])


class DoNotMergeCommentAfterPickup(unittest.TestCase):
    """Case 2: 'Do not merge' comment from learhy added after pickup."""

    def test_holds_even_with_go_label(self):
        state = make_state(
            pr_comments=[comment(LEARNY, "Hold: do not merge. Open the PR and stop.",
                                 iso(7, 42))],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("do-not-merge", decision["reason"])

    def test_comment_from_other_user_is_not_a_hold(self):
        state = make_state(
            pr_comments=[comment("someoneelse", "please do not merge yet", iso(7, 42))],
        )
        self.assertEqual(decide_merge(state)["decision"], "MERGE")


class GoAndMergeGreenChecks(unittest.TestCase):
    """Case 3: go-and-merge present, no hold signals, checks green."""

    def test_merges(self):
        decision = decide_merge(make_state())
        self.assertEqual(decision["decision"], "MERGE")
        self.assertIn("go-and-merge", decision["reason"])


class MethodsLabelNoApproval(unittest.TestCase):
    """Case 4: methods label, no approval."""

    def test_holds(self):
        state = make_state(pr_labels=["methods"])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("methods label", decision["reason"])

    def test_go_label_does_not_override_methods(self):
        state = make_state(pr_labels=["methods"])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")


class MethodsLabelApprovalOnOlderSha(unittest.TestCase):
    """Case 5: methods label, learhy APPROVE on an older SHA, new commits since."""

    def test_holds(self):
        state = make_state(
            pr_labels=["methods"],
            pr_reviews=[review(LEARNY, "APPROVED", OLD_SHA, iso(9, 0))],
            head_sha=NEW_SHA,
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("methods label", decision["reason"])


class MethodsLabelApprovalOnCurrentSha(unittest.TestCase):
    """Case 6: methods label, learhy APPROVE on the current head SHA, checks green."""

    def test_merges(self):
        state = make_state(
            pr_labels=["methods"],
            pr_reviews=[review(LEARNY, "APPROVED", HEAD, iso(9, 30))],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "MERGE")
        self.assertIn("learhy approval on head", decision["reason"])


class ChangesRequestedThenNewCommits(unittest.TestCase):
    """Case 7: learhy CHANGES_REQUESTED, then new commits, no new approval."""

    def test_holds(self):
        state = make_state(
            head_sha=NEW_SHA,
            pr_reviews=[review(LEARNY, "CHANGES_REQUESTED", OLD_SHA, iso(9, 0))],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("CHANGES_REQUESTED", decision["reason"])


class MergeRejectionIsAHold(unittest.TestCase):
    """Case 8: GitHub rejects the merge (protected branch) -> HOLD, one
    comment, no bypass, no retry."""

    def test_rejection_maps_to_hold(self):
        calls = []

        class RejectingClient:
            def merge(self, repo, pr_number, expected_head_sha):
                calls.append((repo, pr_number, expected_head_sha))
                raise MergeRejected(405, "Pull Request is not mergeable: protected branch")

        outcome = perform_merge(RejectingClient(), "learhy/x", 228, HEAD)
        self.assertFalse(outcome["merged"])
        self.assertEqual(outcome["decision"], "HOLD")
        self.assertIn("GitHub rejected the merge", outcome["reason"])
        self.assertIn("Held for review.", outcome["held_comment"])
        self.assertIn(HEAD, calls[0][2])
        self.assertEqual(len(calls), 1, "no retry after a rejection")

    def test_success(self):
        calls = []

        class OkClient:
            def merge(self, repo, pr_number, expected_head_sha):
                calls.append(expected_head_sha)
                return {"merged": True, "sha": HEAD}

        outcome = perform_merge(OkClient(), "learhy/x", 228, HEAD)
        self.assertTrue(outcome["merged"])
        self.assertEqual(outcome["decision"], "MERGE")
        self.assertEqual(calls, [HEAD])

    def test_held_comment_format(self):
        now = datetime(2026, 11, 1, 9, 30, 0, tzinfo=timezone.utc)
        text = held_comment("methods label on #221", now)
        self.assertEqual(
            text,
            "Held for review. Merge check at 2026-11-01T09:30:00Z: methods label on #221.",
        )


class ReleaseHoldComment(unittest.TestCase):
    """Case 9: learhy 'do not merge', later learhy 'release hold', plus
    go-and-merge: MERGE."""

    def test_release_then_merge(self):
        state = make_state(
            pr_comments=[
                comment(LEARNY, "do not merge yet", iso(7, 42)),
                comment(LEARNY, "release hold", iso(8, 15)),
            ],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "MERGE")

    def test_ok_to_merge_also_releases(self):
        state = make_state(
            pr_comments=[
                comment(LEARNY, "do not merge yet", iso(7, 42)),
                comment(LEARNY, "ok to merge", iso(8, 15)),
            ],
        )
        self.assertEqual(decide_merge(state)["decision"], "MERGE")

    def test_hold_after_release_still_holds(self):
        state = make_state(
            pr_comments=[
                comment(LEARNY, "do not merge yet", iso(7, 42)),
                comment(LEARNY, "release hold", iso(8, 15)),
                comment(LEARNY, "actually do not merge, bug found", iso(8, 30)),
            ],
        )
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_release_on_issue_releases_pr_hold(self):
        # The hold was posted on the PR, the release on the linked issue.
        state = make_state(
            pr_comments=[comment(LEARNY, "do not merge", iso(7, 42))],
            linked_issues=[issue(comments=[comment(LEARNY, "release hold", iso(8, 15))],
                                  labels=["agent:cole", "cole:go-and-merge"])],
        )
        self.assertEqual(decide_merge(state)["decision"], "MERGE")


class IncidentTimelineReplay(unittest.TestCase):
    """Replay of the #221/#228 incident (2026-11-01 UTC):

    01:16 issue #221 picked up with cole:go-and-merge
    07:41 learhy removed cole:go-and-merge from #221
    07:42 learhy commented: "Hold: do not merge. Open the PR and stop."
    09:22:45 PR #228 opened
    09:22:47 PR #228 was merged from pickup-cached state  <- the bug

    At merge time the fresh state must now produce HOLD.
    """

    def test_incident_now_holds(self):
        state = make_state(
            pr_number=228,
            head_sha=HEAD,
            # At merge time: the label is GONE (removed 07:41)...
            linked_issues=[issue(
                number=221,
                labels=["agent:cole"],
                # ...and the 07:42 hold comment is on the issue.
                comments=[comment(LEARNY, "Hold: do not merge. Open the PR and stop.",
                                  iso(7, 42))],
            )],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("do-not-merge", decision["reason"])
        # The audit line must record what the decider saw, for post-hoc review.
        self.assertEqual(decision["audit_line"], " ".join(decision["audit_line"].split()))
        self.assertIn("decision=HOLD", decision["audit_line"])
        self.assertIn("approval=none", decision["audit_line"])

    def test_pickup_time_state_would_have_merged(self):
        # Sanity: with the 01:16 pickup state (label present, no hold yet),
        # the decision WAS mergeable — the bug was deciding at pickup, not
        # the state itself.
        state = make_state()
        self.assertEqual(decide_merge(state)["decision"], "MERGE")


class GateDetails(unittest.TestCase):
    """Mergeability, checks, and edge cases from the spec's conditions."""

    def test_failing_check_holds(self):
        state = make_state(check_runs=[{"name": "ci", "conclusion": "failure"}])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("check run failed: ci", decision["reason"])

    def test_mergeable_state_blocked_holds(self):
        state = make_state(mergeable_state="blocked")
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_unreleased_dnm_in_issue_body_holds(self):
        state = make_state(linked_issues=[issue(body="do not merge until Q3")])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_cole_hold_label_holds(self):
        state = make_state(linked_issues=[issue(labels=["agent:cole", "cole:hold"])])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("cole:hold label on #221", decision["reason"])

    def test_no_linked_issue_no_approval_holds(self):
        state = make_state(linked_issues=[])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_audit_line_is_single_line_and_complete(self):
        state = make_state(
            pr_labels=["methods"],
            pr_reviews=[review(LEARNY, "APPROVED", HEAD, iso(9, 30))],
            linked_issues=[issue(body="line one\nline two",
                                 labels=["agent:cole", "cole:go-and-merge"])],
        )
        decision = decide_merge(state)
        line = decision["audit_line"]
        self.assertNotIn("\n", line)
        self.assertIn("merge-audit PR #228", line)
        self.assertIn("decision=MERGE", line)
        self.assertIn("approval=%s" % HEAD[:12], line)
        self.assertIn("methods", line)

    def test_changes_requested_cleared_by_newer_approval(self):
        state = make_state(
            pr_reviews=[
                review(LEARNY, "CHANGES_REQUESTED", OLD_SHA, iso(9, 0)),
                review(LEARNY, "APPROVED", HEAD, iso(9, 30)),
            ],
        )
        self.assertEqual(decide_merge(state)["decision"], "MERGE")


class GithubStateParsing(unittest.TestCase):
    """github_state shaping logic (regex + shapers only; no network)."""

    def test_parse_linked_issues(self):
        body = "Fixes #221, closes #230 and RESOLVES:#7 but not #999."
        self.assertEqual(github_state.parse_linked_issues(body), [221, 230, 7])

    def test_parse_linked_issues_empty(self):
        self.assertEqual(github_state.parse_linked_issues("no refs here"), [])

    def test_comment_and_review_shapers(self):
        c = github_state._comment({"user": {"login": LEARNY}, "body": "hi",
                                   "created_at": "2026-11-01T07:42:00Z"})
        self.assertEqual(c, {"author": LEARNY, "body": "hi",
                             "created_at": "2026-11-01T07:42:00Z"})
        r = github_state._review({"user": {"login": LEARNY}, "state": "APPROVED",
                                  "commit_id": HEAD, "submitted_at": iso(9, 30)})
        self.assertEqual(r["commit_id"], HEAD)

    def test_fetch_state_via_fake_runner(self):
        pr = {
            "head": {"sha": HEAD}, "body": "Closes #221",
            "labels": [{"name": "methods"}], "mergeable_state": "clean",
            "user": {"login": "project-stdio-orchestrator[bot]"},
        }
        responses = {
            "repos/learhy/x/pulls/228": pr,
            "repos/learhy/x/issues/228/comments?per_page=100": [],
            "repos/learhy/x/pulls/228/reviews?per_page=100": [],
            "repos/learhy/x/commits/%s/check-runs?per_page=100" % HEAD:
                {"check_runs": [{"name": "ci", "conclusion": "success"}]},
            "repos/learhy/x/issues/221": {"body": "work", "labels": [{"name": "cole:go-and-merge"}]},
            "repos/learhy/x/issues/221/comments?per_page=100": [],
        }
        state = github_state.fetch_state("learhy/x", 228,
                                         runner=lambda args: responses[args[0]])
        self.assertEqual(state["head_sha"], HEAD)
        self.assertEqual(state["pr_labels"], ["methods"])
        self.assertEqual(state["linked_issues"][0]["labels"], ["cole:go-and-merge"])
        self.assertEqual(state["check_runs"], [{"name": "ci", "conclusion": "success"}])
        # The shaped state feeds the decision directly.
        self.assertEqual(decide_merge(state)["decision"], "HOLD")  # methods + no approval


if __name__ == "__main__":
    unittest.main()
