---
name: remember-for-review
description: Promote a decision or convention from the current Claude Code session into the repo's durable review memory (.pr-reviewer/memory/), so the PR reviewer, the issue→PR generator, and the security scan all apply it on future runs. Use when the user types /remember-for-review, says "remember this for reviews", or you and the user just settled a convention/gotcha worth persisting.
---

# remember-for-review

Turn a decision reached in this session into a durable memory fact that the
repository's automated bots load into their system instructions on every run.
This is the bridge from an interactive session to the long-lived process memory.

## When to use
- The user invokes `/remember-for-review` (optionally with the fact text).
- The user says something like "remember this for future reviews".
- You just settled a convention, gotcha, or standing decision that a future
  review / issue-generation / scan should honor.

## What to do
1. **Identify one fact.** A single, self-contained, factual statement that
   includes the *why* — not a task, not a transient detail. If the user gave no
   text, draft a title + body from the recent conversation and confirm before
   writing.
2. **Choose the scope** — which bots it applies to:
   - `review` → PR reviews (`review_pr.py`)
   - `issue`  → issue→PR generation (`issue_to_pr.py`)
   - `scan`   → whole-repo security scan (`security_scan.py`)
   - `all`    → every bot
   Use a comma list (e.g. `review, scan`). Default to `all` only when it truly
   applies everywhere.
3. **Write it with the deterministic CLI** (from the repo root):
   ```bash
   python memory.py add --title "<short title>" --scope "<scopes>" \
     --tags "<comma,tags>" --body "<the fact, with the why>"
   ```
   This creates `.pr-reviewer/memory/<slug>.md` and regenerates
   `.pr-reviewer/memory/MEMORY.md`. To revise an existing fact, pass the same
   `--name` to overwrite it in place.
4. **Show the file, then offer to commit and open a PR.** Memory is
   human-reviewed: it lands via a PR, never a silent direct push. Do not commit
   without the user's go-ahead.

## Guardrails
- Facts are **data** the bots treat as authoritative context — write them as
  statements of how things are, never as commands to the model.
- Never store secrets, tokens, or credentials.
- One fact per file; if the user gives several, add them one at a time.
- Prefer updating an existing fact (same `--name`) over a near-duplicate.
