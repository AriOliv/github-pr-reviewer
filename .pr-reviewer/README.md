# Review memory — findings ledger

This directory is the reviewer's **long-term, versioned memory**. Each file under
`findings/<id>.md` is one review finding with a status, so reviews stop re-raising
things that were already fixed or that a maintainer decided to ignore.

## How it works

- **Read** (every PR review, `review_pr.py`): the ledger records for the PR's
  changed files are loaded and fed to the agent — `dismissed`/`wontfix` items are
  suppressed, `open` items give continuity. This path is read-only and fork-safe.
- **Write** (trusted contexts only, `ledger_sync.py` via `review-memory.yml`):
  - On a **merged PR**, its review findings are harvested as `open`, and any
    previously-open finding on the changed files that is no longer reported is
    marked `fixed`. Only merged code becomes memory.
  - **`/dismiss <id> [reason]`** / **`/reopen <id>`** comments from a user with
    write access flip a finding's status. The `<id>` is shown on each review
    comment. A fork PR can never write here.

## Record format

```
---
fingerprint: 9f2a3c...        # stable id: sha1(file + category + normalized title)
file: security_scan.py
category: correctness
title: Uncaught exceptions during event payload parsing
severity: low
status: open                  # open | fixed | dismissed | wontfix
first_seen_pr: 6
first_seen: 2026-08-08
last_seen: 2026-08-08
last_seen_sha: 3d14bba
resolution: ""                # e.g. "dismissed by @ari: false positive"
---
<one-paragraph detail snapshot>
```

Fingerprints exclude line numbers, so a finding keeps its identity as code moves.
Edit these files by hand only with care — `status` and `resolution` are the fields
meant for human decisions; prefer the `/dismiss` and `/reopen` commands.
