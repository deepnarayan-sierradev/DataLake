---
description: Run the full local verification suite (lint, scoped typecheck, tests, security scan) and report pass/fail
---

Run this repo's verification suite and report results concisely — summarize, don't dump full raw
tool output unless something fails and the detail is needed to explain it.

1. `.venv/bin/ruff check .`
2. Scoped mypy on the files/packages touched this session. Check `git status --porcelain` /
   `git diff --stat` first to know what to scope to. **Never run bare `.venv/bin/mypy .`** — it
   fails on pre-existing, unrelated issues (see root `CLAUDE.md`'s Verification section for why).
3. `.venv/bin/pytest --no-cov -q` for a fast pass. If it's been a while since the coverage gate
   was last checked in this session, also run the slower `.venv/bin/pytest -q` (enforces
   `--cov-fail-under=80`).
4. `.venv/bin/bandit -r . --exclude .venv,tests,dist -c pyproject.toml`
5. If any `.tf` file changed: `cd infrastructure/environments/dev && terraform init -backend=false
   && terraform validate` (only `dev` is expected to be clean — see `infrastructure/CLAUDE.md` for
   the pre-existing staging/prod errors that are not yours to fix here).

For any failure, before reporting it as a regression, check whether it's pre-existing:
`git show HEAD:<file> | .venv/bin/mypy -` (or the ruff equivalent), or `git diff` on the exact
lines involved. Report a short summary: what passed, what failed, and for each failure whether
it's pre-existing debt or something introduced in this session.
