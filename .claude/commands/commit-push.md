---
description: Stage, commit, and push the current changes with a proper message following repo conventions
---

Commit and push the working-tree changes. Follow these steps exactly and report a concise summary
(branch, commit subject, push result) at the end.

1. **Survey what's changing.** Run `git status --porcelain` and `git diff --stat` (and
   `git diff`/`git diff --staged` as needed) to understand the full set of changes. If the working
   tree is clean, stop and say so — nothing to commit.

2. **Confirm the target branch.** Run `git branch --show-current`.
   - If it is `main` (the default branch), **do not commit to it directly** — create a topic
     branch first (`git switch -c <short-kebab-name>` describing the change, e.g.
     `add-redshift-serving-store`), then continue on that branch.
   - Otherwise, commit on the current branch.

3. **Stage deliberately.** Stage the files that belong to this change with explicit paths
   (`git add <paths>`), not a blanket `git add -A`, unless every pending change is clearly part of
   the same unit of work. Do **not** stage stray/unrelated artifacts (build outputs, generated
   `.pptx`, scratch files) unless they are genuinely part of the change.

4. **Write the commit message** from the actual diff, not assumptions:
   - A `type: summary` subject line (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`),
     ≤ ~72 chars, imperative mood.
   - A short body explaining *what* and *why* when the change is non-trivial.
   - End the message with this trailer exactly:

     ```
     Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
     ```

   Commit with a heredoc to preserve formatting:

   ```bash
   git commit -m "$(cat <<'EOF'
   <type>: <subject>

   <body>

   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   EOF
   )"
   ```

5. **Push.** Push the current branch, setting upstream if it has none:
   `git push -u origin HEAD`.
   - **Never** use `git push --force` / `-f` — it is hard-blocked by a `.claude/settings.json`
     PreToolUse hook. If a non-fast-forward rejects the push, stop and report it; tell me to
     reconcile (rebase/pull) rather than forcing.

6. **Report** the branch name, the commit subject, and the push outcome. If we're on a new topic
   branch (not `main`), mention that a PR can be opened next (don't open one unless I ask).

Only run destructive or history-rewriting git operations if I explicitly ask; this command
stages, commits, and pushes only.
