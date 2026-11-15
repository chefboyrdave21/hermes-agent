# sk fork CI

`.github/workflows/sk-fork-ci.yml` is the merge gate for PRs into this fork's
`sk/*` runtime lines and `card/*` branches.

**Why a separate workflow.** Upstream's workflows run on paid runners and
upstream-only checks (contributor attribution, Windows lanes, the code-health
ratchet). In this fork they sit pending or fail whatever a PR changes, so they
cannot tell us whether our change is good. This one runs on GitHub's own
`ubuntu-24.04`, using the same pinned `setup-uv` and `uv sync --locked` as
upstream's `tests.yml`.

**What it runs.**
- `ruff check` on the Python files the PR changes.
- `pytest` on the files in `tests.txt`: every test file the fork adds or
  changes versus upstream, plus suites next to code our patches touch.

**Rules.**
- A fork PR that adds a test file adds it to `tests.txt`, in the same PR.
- Every job is gated on `github.repository == 'chefboyrdave21/hermes-agent'`,
  so the file is inert anywhere else. It never edits or disables upstream's
  workflows, which keeps rebases onto upstream conflict-free.
- Upstream's red checks on fork PRs are expected. The gate is
  `sk gate (ruff + fork suites)`.
