"""Trusted writer for the findings ledger (runs from review-memory.yml only).

Two modes, both invoked in a trusted context (base branch, never a fork PR):

  harvest  — on a merged pull request: read the findings payload from the
             review's summary-comment marker, upsert `open` records, and mark
             `fixed` any previously-open record on the changed files that the
             latest review no longer reports.

  comment  — on an issue/PR comment: if it is `/dismiss <id> [reason]` or
             `/reopen <id>` from a user with write access, flip that record's
             status. Anyone can comment, so authorization is checked here.

Ledger changes are committed and pushed to the default branch. Requires
GITHUB_REPOSITORY, GITHUB_TOKEN (or GH_TOKEN), GITHUB_DEFAULT_BRANCH, and the
per-mode env described below.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

import ledger
from github_client import GitHub

CMD_RE = re.compile(r"^/(dismiss|reopen)\s+([0-9a-fA-F]{6,40})\s*(.*)$")
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"❌ Missing required env variable: {name}", file=sys.stderr)
        raise SystemExit(1)
    return val


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise subprocess.CalledProcessError(res.returncode, ["git", *args], res.stdout, res.stderr)
    return res


def commit_and_push(message: str, branch: str) -> bool:
    """Commit ledger changes and push to `branch`, rebasing once on conflict."""
    _git("config", "user.name", "github-actions[bot]")
    _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    _git("add", ledger.LEDGER_DIR)
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("No ledger changes to commit.")
        return False
    _git("commit", "-m", message)
    for attempt in (1, 2):
        push = _git("push", "origin", f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            print("Ledger pushed.")
            return True
        print(f"push rejected (try {attempt}); rebasing on origin/{branch} …", file=sys.stderr)
        _git("fetch", "origin", branch, check=False)
        if _git("rebase", f"origin/{branch}", check=False).returncode != 0:
            # A conflicted rebase would leave the tree mid-rebase and make every
            # retry fail; abort cleanly and surface it.
            _git("rebase", "--abort", check=False)
            raise SystemExit("❌ Ledger rebase hit conflicts; aborted — re-run the action.")
    raise SystemExit("❌ Could not push ledger updates after a rebase retry.")


# --------------------------------------------------------------------------- #
def _latest_markers_by_skill(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest state marker per review skill, from oldest→newest comment order."""
    by_skill: dict[str, dict[str, Any]] = {}
    for c in comments:
        state = ledger.parse_state_marker(c.get("body") or "")
        if state and state.get("findings") is not None:
            by_skill[state.get("skill", "")] = state
    return list(by_skill.values())


def harvest(gh: GitHub, root: pathlib.Path, branch: str) -> None:
    pr_number = int(_env("PR_NUMBER"))
    print(f"Harvesting findings from merged PR #{pr_number} …")
    comments = gh.list_issue_comments(pr_number)
    markers = _latest_markers_by_skill(comments)
    if not markers:
        print("No review marker found on this PR; nothing to harvest.")
        return

    sha = ""
    harvested: set[str] = set()
    for state in markers:
        sha = state.get("head_sha", sha)
        for mf in state.get("findings", []):
            record = ledger.upsert_open(
                root, mf, pr_number=pr_number, sha=state.get("head_sha", ""), today=_today()
            )
            harvested.add(record["fingerprint"])
    print(f"Upserted {len(harvested)} finding(s) as open.")

    # Anything still `open` on the changed files but no longer reported is fixed.
    changed = [f.get("filename", "") for f in gh.get_pr_files(pr_number)]
    fixed = 0
    for rec in ledger.load_for_files(root, changed):
        if rec.get("status") == "open" and rec["fingerprint"] not in harvested:
            if ledger.mark_fixed(root, rec["fingerprint"], sha=sha, today=_today()):
                fixed += 1
    if fixed:
        print(f"Marked {fixed} previously-open finding(s) as fixed.")

    commit_and_push(f"chore(ledger): harvest findings from merged PR #{pr_number}", branch)


def handle_comment(gh: GitHub, root: pathlib.Path, branch: str) -> None:
    body = os.environ.get("COMMENT_BODY", "")
    author = os.environ.get("COMMENT_AUTHOR", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")

    m = CMD_RE.match(body.strip().splitlines()[0] if body.strip() else "")
    if not m:
        print("Comment is not a ledger command; ignoring.")
        return
    action, fp, reason = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()

    perm = gh.get_user_permission(author) if author else "none"
    if perm not in WRITE_PERMISSIONS:
        print(f"⛔ @{author} lacks write access (permission={perm}); ignoring '{action}'.")
        if issue_number:
            gh.post_issue_comment(
                int(issue_number),
                f"@{author} `/{action}` ignored — it requires write access to this repo.",
            )
        return

    if ledger.get(root, fp) is None:
        print(f"No ledger record for id {fp}; ignoring.")
        if issue_number:
            gh.post_issue_comment(int(issue_number), f"No finding with id `{fp}` in the ledger.")
        return

    if action == "dismiss":
        resolution = f"dismissed by @{author}" + (f": {reason}" if reason else "")
        ledger.set_status(root, fp, "dismissed", today=_today(), resolution=resolution)
        confirm = f"✅ Finding `{fp}` dismissed — it won't be raised again."
    else:  # reopen
        ledger.set_status(root, fp, "open", today=_today(), resolution="")
        confirm = f"✅ Finding `{fp}` reopened."

    pushed = commit_and_push(f"chore(ledger): {action} finding {fp} by @{author}", branch)
    if issue_number and pushed:
        gh.post_issue_comment(int(issue_number), confirm)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("harvest", "comment"):
        print("usage: ledger_sync.py {harvest|comment}", file=sys.stderr)
        return 2
    mode = sys.argv[1]

    repo = _env("GITHUB_REPOSITORY")
    gh_token = os.environ.get("GITHUB_TOKEN") or _env("GH_TOKEN")
    branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    root = pathlib.Path.cwd()

    gh = GitHub(api_url, repo, gh_token)
    if mode == "harvest":
        harvest(gh, root, branch)
    else:
        handle_comment(gh, root, branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
