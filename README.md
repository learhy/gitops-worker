# gitops-worker

The Hermes cron worker ("Cole") that runs the GitOps workflow on `learhy/*` repos:
GitHub issues in, PRs out, auto-deploy on merge.

Files here are the live sources:
- `gitops-worker-prompt.md` — the worker's prompt (deployed via `hermes cron edit`)
- `gitops-github-filter.py` — webhook payload filter (routes label/comment/push events)
- `gitops-ledger-sync.py` — run ledger sync + status page data
- `gh-app-token.sh` — GitHub App installation-token mint/cache
- `gitops-repos.txt` — repo allowlist

Deploy: edit the file here, then `hermes cron edit f42f6ffda76b --prompt "$(cat gitops-worker-prompt.md)"`.
The live copies in `~/.hermes/scripts/` are deployment targets, not sources of truth.
