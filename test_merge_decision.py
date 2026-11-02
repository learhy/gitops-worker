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


COPY_REVIEW_BODY = (
    "**Do not merge.** Open the PR and stop. Claude will review it before it merges.\n\n"
    "**Priority:** Top-3\n\n**Location:** National Pulse..."
)


class ReviewLaneMethodsIssue(unittest.TestCase):
    """2026-11-02: methods issues from the automated reviews build without a
    'go' and merge once learhy (Claude's review) approves the current head.
    Their bodies open with 'Do not merge.', a review hold."""

    def methods_state(self, **overrides):
        base = dict(linked_issues=[issue(number=250, body=COPY_REVIEW_BODY,
                                         labels=["agent:cole", "copy-review", "methods"])])
        base.update(overrides)
        return make_state(**base)

    def test_holds_before_review(self):
        decision = decide_merge(self.methods_state())
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("methods label on #250", decision["reason"])
        self.assertIn("do-not-merge in #250 body", decision["audit_line"])

    def test_approval_on_current_head_merges(self):
        state = self.methods_state(pr_reviews=[review(LEARNY, "APPROVED", HEAD, iso(10, 0))])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "MERGE")
        self.assertIn("learhy approval on head", decision["reason"])

    def test_approval_on_older_head_still_holds(self):
        state = self.methods_state(head_sha=NEW_SHA,
                                   pr_reviews=[review(LEARNY, "APPROVED", OLD_SHA, iso(10, 0))])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_approval_does_not_release_a_hold_comment(self):
        # A learhy "do not merge" comment is a deliberate intervention: only a
        # release comment clears it, not an approval.
        state = self.methods_state(
            pr_comments=[comment(LEARNY, "Hold: do not merge, numbers are off", iso(9, 0))],
            pr_reviews=[review(LEARNY, "APPROVED", HEAD, iso(10, 0))],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("do-not-merge from learhy on PR #228", decision["reason"])

    def test_body_dnm_released_by_release_comment_with_go_label(self):
        state = make_state(linked_issues=[issue(
            body="Do not merge until the Q3 data lands.",
            labels=["agent:cole", "cole:go-and-merge"],
            comments=[comment(LEARNY, "Q3 data is in, release hold", iso(10, 0))],
        )])
        self.assertEqual(decide_merge(state)["decision"], "MERGE")

    def test_body_dnm_without_release_or_approval_holds(self):
        state = make_state(linked_issues=[issue(body="Don't merge this before Friday",
                                                labels=["agent:cole", "cole:go-and-merge"])])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("body", decision["reason"])


class ReleasePhrases(unittest.TestCase):
    def test_negated_release_does_not_release(self):
        state = make_state(pr_comments=[
            comment(LEARNY, "do not merge", iso(7, 0)),
            comment(LEARNY, "This is not ok to merge yet, the 2036 numbers are off", iso(8, 0)),
        ])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_is_release(self):
        self.assertTrue(merge_decision.is_release("ok to merge"))
        self.assertTrue(merge_decision.is_release("Looks right now. Okay to merge."))
        self.assertTrue(merge_decision.is_release("release the hold"))
        self.assertFalse(merge_decision.is_release("not ok to merge"))
        self.assertFalse(merge_decision.is_release("it isn't ok to merge"))
        self.assertFalse(merge_decision.is_release("don't release hold yet"))
        self.assertFalse(merge_decision.is_release("never okay to merge on red CI"))

    def test_dont_merge_variants_hold(self):
        for text in ("don't merge", "Dont merge yet", "do-not-merge", "DO NOT MERGE",
                     "don’t merge"):
            state = make_state(pr_comments=[comment(LEARNY, text, iso(7, 0))])
            self.assertEqual(decide_merge(state)["decision"], "HOLD", text)


class MultiIssuePrs(unittest.TestCase):
    def test_go_label_must_be_on_every_linked_issue(self):
        state = make_state(linked_issues=[
            issue(number=255, labels=["agent:cole", "cole:go-and-merge"]),
            issue(number=300, labels=["agent:cole"]),  # plain "go": Dan merges
        ])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("every linked issue", decision["reason"])

    def test_methods_on_second_issue_holds(self):
        state = make_state(linked_issues=[
            issue(number=255, labels=["agent:cole", "cole:go-and-merge"]),
            issue(number=257, labels=["agent:cole", "cole:go-and-merge", "methods"]),
        ])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_parse_closing_keyword_lists(self):
        parse = github_state.parse_linked_issues
        self.assertEqual(parse("Closes #241, #245 and #250"), [241, 245, 250])
        self.assertEqual(parse("Fixes #1 & #2, closes #3"), [1, 2, 3])
        self.assertEqual(parse("Closes #241, see also #999"), [241])


class CheckRunsForbidden(unittest.TestCase):
    """A GitHub App token without checks:read gets HTTP 403 on check-runs.

    The fetch must degrade to no check data (mergeable_state still carries
    the CI signal), not fail the whole state fetch.
    """
    FORBIDDEN = ("gh api repos/learhy/x/commits/%s/check-runs?per_page=100 "
                 "failed (exit 1): gh: Resource not accessible by integration "
                 "(HTTP 403)" % HEAD)

    def test_403_on_check_runs_degrades_to_empty(self):
        def runner(args):
            path = args[0]
            if path == "repos/learhy/x/pulls/228":
                return {"head": {"sha": HEAD}, "body": "Closes #221",
                        "labels": [], "mergeable_state": "clean"}
            if "check-runs" in path:
                raise github_state.GithubStateError(self.FORBIDDEN)
            if path == "repos/learhy/x/issues/221":
                return {"body": "work", "labels": [{"name": "cole:go-and-merge"}]}
            return []

        state = github_state.fetch_state("learhy/x", 228, runner=runner)
        self.assertEqual(state["check_runs"], [])
        self.assertEqual(state["mergeable_state"], "clean")

    def test_non_403_check_runs_failure_still_raises(self):
        def runner(args):
            path = args[0]
            if path == "repos/learhy/x/pulls/228":
                return {"head": {"sha": HEAD}, "body": "Closes #221",
                        "labels": [], "mergeable_state": "clean"}
            if "check-runs" in path:
                raise github_state.GithubStateError("gh api failed: timeout")
            return []

        with self.assertRaises(github_state.GithubStateError):
            github_state.fetch_state("learhy/x", 228, runner=runner)


class MergeableStateRetry(unittest.TestCase):
    def test_unknown_mergeable_state_is_reread(self):
        reads = {"n": 0}
        sleeps = []

        def runner(args):
            path = args[0]
            if path == "repos/learhy/x/pulls/228":
                reads["n"] += 1
                state = "unknown" if reads["n"] == 1 else "clean"
                return {"head": {"sha": HEAD}, "body": "Closes #221", "labels": [],
                        "mergeable_state": state}
            if "check-runs" in path:
                return {"check_runs": []}
            if path == "repos/learhy/x/issues/221":
                return {"body": "work", "labels": [{"name": "cole:go-and-merge"}]}
            return []

        state = github_state.fetch_state("learhy/x", 228, runner=runner,
                                         sleep=lambda s: sleeps.append(s))
        self.assertEqual(state["mergeable_state"], "clean")
        self.assertEqual(reads["n"], 2)
        self.assertEqual(len(sleeps), 1)


class CliRejectionPath(unittest.TestCase):
    """Regression: run as a script, merge_decision is __main__, and the old
    github_state raised merge_decision.MergeRejected from a SECOND import of
    the module, so `except MergeRejected` missed it and the CLI died with a
    traceback instead of printing HOLD. Exercise the real CLI with a fake gh."""

    FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(os.environ.get("GH_TOKEN", "<none>") + "\n")
if os.environ.get("GH_TOKEN") != "app-token-123":
    sys.stderr.write("gh: Bad credentials (HTTP 401)\n")
    sys.exit(1)
a = sys.argv[1:]
if "--method" in a:
    sys.stderr.write("gh: %s (HTTP %s)\n" % ("refused", os.environ["FAKE_MERGE_STATUS"]))
    sys.exit(1)
p = a[1]
if p.endswith("/pulls/7"):
    print(json.dumps({"head": {"sha": "a" * 40}, "body": "Closes #3", "labels": [],
                      "mergeable_state": "clean"}))
elif "/check-runs" in p:
    print(json.dumps({"check_runs": []}))
elif p.endswith("/issues/3"):
    print(json.dumps({"body": "fix a typo", "labels": [{"name": "cole:go-and-merge"}]}))
else:
    print("[]")
'''

    def run_cli(self, status, token_script="#!/bin/sh\nprintf app-token-123\n"):
        import json as _json
        import os
        import subprocess
        import sys
        import tempfile
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmp:
            gh = os.path.join(tmp, "gh")
            with open(gh, "w") as fh:
                fh.write(self.FAKE_GH)
            os.chmod(gh, 0o755)
            mint = os.path.join(tmp, "gh-app-token.sh")
            with open(mint, "w") as fh:
                fh.write(token_script)
            os.chmod(mint, 0o755)
            log = os.path.join(tmp, "gh.log")
            open(log, "w").close()
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""),
                       FAKE_MERGE_STATUS=str(status), FAKE_GH_LOG=log,
                       GITOPS_GH_TOKEN_CMD=mint,
                       GH_TOKEN="personal-login-token")  # must NOT be used
            proc = subprocess.run(
                [sys.executable, os.path.join(here, "merge_decision.py"),
                 "--repo", "learhy/x", "--pr", "7", "--merge"],
                capture_output=True, text=True, env=env, cwd=here, timeout=60,
            )
            self.gh_tokens = open(log).read().split()
        self.assertNotIn("Traceback", proc.stderr)
        lines = [_json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        return proc.returncode, lines

    def test_every_gh_call_uses_the_app_token(self):
        # 2026-11-02: PR #260 was merged by the gate on Dan's personal login
        # because the App token didn't reach the gate's shell.
        self.run_cli(405)
        self.assertTrue(self.gh_tokens)
        self.assertEqual(set(self.gh_tokens), {"app-token-123"})

    def test_token_mint_failure_holds_without_calling_gh(self):
        code, lines = self.run_cli(405, token_script="#!/bin/sh\necho no pem >&2\nexit 1\n")
        self.assertEqual(code, 2)
        self.assertEqual(lines[-1]["decision"], "HOLD")
        self.assertIn("GitHub App token", lines[-1]["reason"])
        self.assertEqual(self.gh_tokens, [])

    def test_rejections_print_hold(self):
        for status in (403, 405, 409, 422):
            code, lines = self.run_cli(status)
            self.assertEqual(lines[0]["decision"], "MERGE")  # the gate allowed it
            self.assertEqual(lines[-1]["decision"], "HOLD", status)
            self.assertFalse(lines[-1]["merged"])
            self.assertIn("Held for review.", lines[-1]["held_comment"])
            self.assertEqual(code, 1)

    def test_other_errors_print_hold_not_traceback(self):
        code, lines = self.run_cli(500)
        self.assertEqual(lines[-1]["decision"], "HOLD")
        self.assertIn("merge state unknown", lines[-1]["reason"])
        self.assertEqual(code, 2)


# A PR's own change, and the same change after master moved and the PR was
# updated from master: the hunk line numbers shift, the lines don't.
JS_PATCH_OLD = ("@@ -120,7 +120,7 @@ function headline(c) {\n"
                "   var cap = c.ceiling;\n"
                "-  return 'levels off at 60%';\n"
                "+  return 'levels off at ' + cap + '%';\n")
JS_PATCH_NEW = JS_PATCH_OLD.replace("@@ -120,7 +120,7 @@", "@@ -131,7 +131,7 @@")
LOG_PATCH_OLD = "@@ -1,3 +1,6 @@\n # Changelog\n+## 2026-11-02 fix #241\n+- headline cap\n+\n"
LOG_PATCH_NEW = ("@@ -1,9 +1,12 @@\n # Changelog\n+## 2026-11-02 fix #241\n+- headline cap\n+\n"
                 " ## 2026-11-02 fix #255\n")


def carry(old_files=None, new_files=None, status="ahead", sha=OLD_SHA, error=None):
    c = {
        "approved_sha": sha,
        "ancestry_status": status,
        "old_files": old_files if old_files is not None else [
            {"filename": "web/js/np.js", "status": "modified", "patch": JS_PATCH_OLD},
            {"filename": "CHANGELOG.md", "status": "modified", "patch": LOG_PATCH_OLD},
        ],
        "new_files": new_files if new_files is not None else [
            {"filename": "web/js/np.js", "status": "modified", "patch": JS_PATCH_NEW},
            {"filename": "CHANGELOG.md", "status": "modified", "patch": LOG_PATCH_NEW},
        ],
    }
    if error:
        c["error"] = error
    return c


class ApprovalCarryOver(unittest.TestCase):
    """2026-11-02: every cole PR adds a CHANGELOG entry, so merging one PR of
    an approved batch conflicts the rest; Cole merges master into each, which
    makes a new head. An approval carries over iff the PR's own change is
    identical, so a batch drains in one cycle instead of one review run per PR."""

    def held_state(self, **overrides):
        base = dict(
            head_sha=NEW_SHA,
            linked_issues=[issue(number=241, body=COPY_REVIEW_BODY,
                                 labels=["agent:cole", "copy-review", "methods"])],
            pr_reviews=[review(LEARNY, "APPROVED", OLD_SHA, iso(10, 0))],
            approval_carryover=carry(),
        )
        base.update(overrides)
        return make_state(**base)

    def test_conflict_only_update_keeps_approval(self):
        decision = decide_merge(self.held_state())
        self.assertEqual(decision["decision"], "MERGE", decision["reason"])
        self.assertIn("carried over a conflict-only update", decision["reason"])

    def test_changed_code_needs_a_new_approval(self):
        changed = JS_PATCH_NEW.replace("cap + '%'", "cap + ' percent'")
        state = self.held_state(approval_carryover=carry(new_files=[
            {"filename": "web/js/np.js", "status": "modified", "patch": changed},
            {"filename": "CHANGELOG.md", "status": "modified", "patch": LOG_PATCH_NEW},
        ]))
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("web/js/np.js changed since approval", decision["reason"])

    def test_added_file_needs_a_new_approval(self):
        files = carry()["new_files"] + [
            {"filename": "web/js/extra.js", "status": "added", "patch": "@@ -0,0 +1 @@\n+x\n"}]
        decision = decide_merge(self.held_state(approval_carryover=carry(new_files=files)))
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("files differ since approval", decision["reason"])

    def test_edited_log_entry_needs_a_new_approval(self):
        edited = LOG_PATCH_NEW.replace("headline cap", "headline cap and more")
        state = self.held_state(approval_carryover=carry(new_files=[
            {"filename": "web/js/np.js", "status": "modified", "patch": JS_PATCH_NEW},
            {"filename": "CHANGELOG.md", "status": "modified", "patch": edited},
        ]))
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_approved_commit_not_an_ancestor(self):
        state = self.held_state(approval_carryover=carry(status="diverged"))
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("not an ancestor", decision["reason"])

    def test_missing_patch_refuses(self):
        files = [dict(f, patch=None) if f["filename"] == "web/js/np.js" else f
                 for f in carry()["new_files"]]
        self.assertEqual(decide_merge(self.held_state(
            approval_carryover=carry(new_files=files)))["decision"], "HOLD")

    def test_fetch_error_refuses(self):
        self.assertEqual(decide_merge(self.held_state(
            approval_carryover=carry(error="compare failed (HTTP 502)")))["decision"], "HOLD")

    def test_newer_changes_requested_still_holds(self):
        state = self.held_state(pr_reviews=[
            review(LEARNY, "APPROVED", OLD_SHA, iso(10, 0)),
            review(LEARNY, "CHANGES_REQUESTED", OLD_SHA, iso(10, 30)),
        ])
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "HOLD")
        self.assertIn("CHANGES_REQUESTED", decision["reason"])

    def test_carry_needs_a_real_learhy_approval(self):
        state = self.held_state(pr_reviews=[review("someoneelse", "APPROVED", OLD_SHA, iso(10, 0))])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_hold_comment_still_holds(self):
        state = self.held_state(pr_comments=[comment(LEARNY, "do not merge, wait for me", iso(10, 5))])
        self.assertEqual(decide_merge(state)["decision"], "HOLD")

    def test_still_needs_clean(self):
        self.assertEqual(decide_merge(self.held_state(mergeable_state="dirty"))["decision"], "HOLD")

    def test_fetch_state_gathers_carryover(self):
        pr = {"head": {"sha": NEW_SHA}, "base": {"ref": "master"}, "body": "Closes #241",
              "labels": [], "mergeable_state": "clean"}
        reviews = [{"user": {"login": LEARNY}, "state": "APPROVED", "commit_id": OLD_SHA,
                    "submitted_at": iso(10, 0)}]
        c = carry()
        responses = {
            "repos/learhy/x/pulls/228": pr,
            "repos/learhy/x/issues/228/comments?per_page=100": [],
            "repos/learhy/x/pulls/228/reviews?per_page=100": reviews,
            "repos/learhy/x/commits/%s/check-runs?per_page=100" % NEW_SHA: {"check_runs": []},
            "repos/learhy/x/issues/241": {"body": COPY_REVIEW_BODY,
                                          "labels": [{"name": "methods"}]},
            "repos/learhy/x/issues/241/comments?per_page=100": [],
            "repos/learhy/x/compare/%s...%s" % (OLD_SHA, NEW_SHA): {"status": "ahead"},
            "repos/learhy/x/compare/master...%s" % OLD_SHA: {"files": c["old_files"]},
            "repos/learhy/x/compare/master...%s" % NEW_SHA: {"files": c["new_files"]},
        }
        state = github_state.fetch_state("learhy/x", 228, runner=lambda a: responses[a[0]])
        self.assertEqual(state["approval_carryover"]["approved_sha"], OLD_SHA)
        self.assertEqual(decide_merge(state)["decision"], "MERGE")

    def test_no_carryover_fetch_when_head_is_approved(self):
        calls = []
        pr = {"head": {"sha": HEAD}, "body": "Closes #221", "labels": [], "mergeable_state": "clean"}

        def runner(args):
            calls.append(args[0])
            if args[0].endswith("/pulls/228"):
                return pr
            if "reviews" in args[0]:
                return [{"user": {"login": LEARNY}, "state": "APPROVED", "commit_id": HEAD,
                         "submitted_at": iso(10, 0)}]
            if "check-runs" in args[0]:
                return {"check_runs": []}
            if args[0].endswith("/issues/221"):
                return {"body": "x", "labels": []}
            return []

        state = github_state.fetch_state("learhy/x", 228, runner=runner)
        self.assertIsNone(state["approval_carryover"])
        self.assertFalse([c for c in calls if "/compare/" in c])


class AuditWording(unittest.TestCase):
    """2026-11-02: a MERGE audit line listed 'do-not-merge in #242 body, not
    released' next to decision=MERGE, which read as a bypass. Lifted review
    holds now say what lifted them."""

    def test_merge_line_says_what_released_the_holds(self):
        state = make_state(
            linked_issues=[issue(number=242, body=COPY_REVIEW_BODY,
                                 labels=["agent:cole", "copy-review", "methods"])],
            pr_reviews=[review(LEARNY, "APPROVED", HEAD, iso(11, 50))],
        )
        decision = decide_merge(state)
        self.assertEqual(decision["decision"], "MERGE")
        line = decision["audit_line"]
        self.assertNotIn("not released", line)
        self.assertIn("do-not-merge in #242 body (review hold), released by learhy approval on %s"
                      % HEAD[:12], line)
        self.assertIn("methods label on #242, released by learhy approval", line)

    def test_hold_line_keeps_holds_plain(self):
        state = make_state(linked_issues=[issue(number=242, body=COPY_REVIEW_BODY,
                                                labels=["agent:cole", "methods"])])
        line = decide_merge(state)["audit_line"]
        self.assertIn("decision=HOLD", line)
        self.assertNotIn("released by", line)
        self.assertIn("do-not-merge in #242 body (review hold)", line)


if __name__ == "__main__":
    unittest.main()
