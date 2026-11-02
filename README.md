# gitops-worker

The Hermes cron worker ("Cole") that runs the GitOps workflow on `learhy/*` repos:
GitHub issues in, PRs out, auto-deploy on merge.

Files here are the live sources:
- `gitops-worker-prompt.md` — the worker's prompt (deployed via `hermes cron edit`)
- `merge_decision.py` — the merge-decision gate: decides merge rights at merge time from fresh GitHub state (HOLD/MERGE + one-line audit record); `--merge` executes the merge with expected-head-SHA protection and maps a GitHub-side rejection to HOLD
  - Lanes: `cole:go-and-merge` builds and merges on sight; an issue with `agent:cole` + `methods` builds on sight (no "go" needed) and merges only after learhy approves the PR's current head (Claude's review); everything else waits for Dan's "go". Add `cole:hold` to keep a methods issue in plan mode.
  - An approval carries over a conflict-only update from master (the PR's net change, outside CHANGELOG.md / DECISIONS.md positions, is identical to what learhy approved), so an approved batch drains in one worker cycle.
  - Deploys: one deploy per burst of merges (10-minute quiet period, never more than 30 minutes behind); a deploy that fails verify is rolled back to the last good commit.
- `github_state.py` — fetches that fresh state via `gh api` and shapes it for the gate
- `test_merge_decision.py` — unit tests for the gate (in-memory GitHub state, incl. the #221/#228 incident replay)
- `gitops-github-filter.py` — webhook payload filter (routes label/comment/push events)
- `gitops-ledger-sync.py` — run ledger sync + status page data
- `gh-app-token.sh` — GitHub App installation-token mint/cache
- `gitops-repos.txt` — repo allowlist

Deploy: edit the file here, then `hermes cron edit f42f6ffda76b --prompt "$(cat gitops-worker-prompt.md)"`.
The live copies in `~/.hermes/scripts/` are deployment targets, not sources of truth.
