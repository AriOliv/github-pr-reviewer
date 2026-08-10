"""Iterative loop: refine a bot PR, or answer a clarifying question on an issue.

Runs from iterate.yml on `issue_comment` (trusted base context). Two modes:

  refine  — a `/refine <feedback>` comment on a bot-generated PR: the model
            updates that PR in place from the reviewer's feedback.
  answer  — a reply on an issue that is awaiting clarification: re-run the issue
            generator with the full thread, so it can now proceed.

Both are open to anyone with repo access (they never merge — a human still
reviews and merges). Refine only touches bot branches (auto-fix/*, security-fix/*)
and skips cross-repo (fork) PRs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from google import genai

import generator
import issue_to_pr
import llm

REFINE_RE = re.compile(r"^/refine\b\s*(.*)", re.IGNORECASE | re.DOTALL)
BOT_BRANCH_PREFIXES = ("auto-fix/", "security-fix/")
MAX_DIFF_CHARS = 20000


def parse_refine(body: str) -> str | None:
    """The feedback after `/refine`, or None if the comment isn't a refine cmd."""
    m = REFINE_RE.match((body or "").strip())
    return m.group(1).strip() if m else None


def is_bot_branch(ref: str) -> bool:
    return any(ref.startswith(p) for p in BOT_BRANCH_PREFIXES)


def _gh_json(args: list[str]) -> dict | list | None:
    import json
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def _reply(issue_number: str, body: str) -> None:
    subprocess.run(["gh", "issue", "comment", issue_number, "--body", body],
                   capture_output=True, text=True)


def refine() -> int:
    pr_number = os.environ.get("PR_NUMBER", "")
    author = os.environ.get("COMMENT_AUTHOR", "")
    feedback = parse_refine(os.environ.get("COMMENT_BODY", ""))
    if feedback is None:
        print("Not a /refine command; ignoring.")
        return 0
    if not feedback:
        _reply(pr_number, "Usage: `/refine <what to change>`")
        return 0

    pr = _gh_json(["gh", "pr", "view", pr_number, "--json",
                   "headRefName,baseRefName,title,body,isCrossRepository,state"])
    if not pr:
        print("Could not read PR.", file=sys.stderr)
        return 1
    if pr.get("state") != "OPEN":
        _reply(pr_number, "`/refine` only works on open PRs.")
        return 0
    if pr.get("isCrossRepository"):
        _reply(pr_number, "`/refine` can't update PRs from forks.")
        return 0
    branch = pr.get("headRefName", "")
    if not is_bot_branch(branch):
        _reply(pr_number, "`/refine` only works on AI-generated PRs "
                          "(`auto-fix/*`, `security-fix/*`).")
        return 0

    diff = subprocess.run(["gh", "pr", "diff", pr_number], capture_output=True, text=True).stdout
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n… [diff truncated] …"

    task = (
        f"Update this pull request per the reviewer's request.\n\n"
        f"PR title: {pr.get('title')}\nPR description:\n{pr.get('body')}\n\n"
        f"Current diff:\n```diff\n{diff}\n```\n\n"
        f"Reviewer request: {feedback}\n\n"
        f"Return file_changes with the FULL new contents of every file you change."
    )
    print(f"✏️ Refining PR #{pr_number} on '{branch}' …")
    client = genai.Client() if os.environ.get("GEMINI_API_KEY") else None
    try:
        data = generator.generate_changes(
            task=task, kind="refine",
            session=llm.session_id("review", ref=pr_number),
            memory_scope="review", genai_client=client,
        )
    except ValueError as exc:
        _reply(pr_number, f"🤖 Couldn't refine: {exc}")
        return 0
    changes = data.get("file_changes", [])
    if not changes:
        _reply(pr_number, "🤖 I couldn't produce a change for that request.")
        return 0

    ok, info = generator.apply_to_branch(
        branch=branch, message=f"refine: {feedback[:60]}", file_changes=changes,
    )
    _reply(pr_number,
           (f"🤖 Updated per your request (@{author}) — {info}. Review the new diff."
            if ok else f"🤖 Refine did not apply: {info}"))
    return 0


def _issue_awaiting_clarification(repo: str, issue_number: str) -> bool:
    # Only re-run when the bot actually posted a pending clarifying question.
    res = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
         "--jq", ".[].body"], capture_output=True, text=True)
    return issue_to_pr.NEEDS_INPUT_MARKER in (res.stdout or "")


def answer() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")
    author = os.environ.get("COMMENT_AUTHOR", "")
    if author.endswith("[bot]"):
        print("Comment is from a bot; ignoring.")
        return 0
    if not _issue_awaiting_clarification(repo, issue_number):
        print("Issue is not awaiting clarification; ignoring the comment.")
        return 0
    issue = _gh_json(["gh", "issue", "view", issue_number, "--json", "title,body"])
    if not issue:
        print("Could not read issue.", file=sys.stderr)
        return 1
    print(f"💬 Reply received on issue #{issue_number}; re-running generation …")
    return issue_to_pr.handle_issue(issue_number, issue.get("title", ""), issue.get("body", ""))


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("refine", "answer"):
        print("usage: iterate.py {refine|answer}", file=sys.stderr)
        return 2
    return refine() if sys.argv[1] == "refine" else answer()


if __name__ == "__main__":
    raise SystemExit(main())
