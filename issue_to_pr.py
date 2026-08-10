"""Issue → PR generator with a clarify step (part of the iterative loop).

On an opened issue (or a reply while awaiting clarification) the model either
opens/updates a PR, or — when the request is too ambiguous to implement safely —
asks ONE clarifying question on the issue and waits. A reply then re-runs this
with the full thread as context (see iterate.py / iterate.yml).
"""

from __future__ import annotations

import os
import subprocess
import sys

from google import genai

import generator
import llm

# Marker on the bot's clarifying question, so a reply knows to re-run generation.
NEEDS_INPUT_MARKER = "<!-- issue-to-pr:needs-input -->"

CLARIFY_RULE = (
    "If — and ONLY if — the request is too ambiguous to implement safely, do "
    "NOT guess: instead return a JSON object {\"clarifying_question\": \"...\"} "
    "with no file_changes. Ask at most ONE concise question, in plain language. "
    "If the thread below already answers your question, proceed and state your "
    "assumption in the pr_description instead of asking again."
)


def comment_on_issue(issue_number: str, body: str) -> None:
    subprocess.run(["gh", "issue", "comment", issue_number, "--body", body],
                   capture_output=True, text=True)


def fetch_thread(issue_number: str) -> str:
    """Prior comments on the issue, oldest first, as plain text (may be empty)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    res = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
         "--jq", '.[] | "@\(.user.login): \(.body)"'],
        capture_output=True, text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def handle_issue(issue_number: str, title: str, body: str) -> int:
    if not os.environ.get("GEMINI_API_KEY") and not llm.proxy_enabled():
        print("Error: set GEMINI_API_KEY or LITELLM_BASE_URL", file=sys.stderr)
        return 1

    print(f"🚀 Processing issue #{issue_number}: {title}")
    client = genai.Client() if os.environ.get("GEMINI_API_KEY") else None
    thread = fetch_thread(issue_number)

    task = f"Issue #{issue_number}: {title}\n\nDescription:\n{body}\n"
    if thread:
        task += f"\nDiscussion so far:\n{thread}\n"
    task += (
        f"\nGenerate the file changes to resolve this issue. The pr_description "
        f"must end with 'Fixes #{issue_number}'."
    )

    try:
        data = generator.generate_changes(
            task=task, kind="issue",
            session=llm.session_id("issue", ref=issue_number),
            memory_scope="issue", extra_system=CLARIFY_RULE, genai_client=client,
        )
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    question = (data.get("clarifying_question") or "").strip()
    file_changes = data.get("file_changes", [])

    # Ambiguous → ask once and wait for a reply.
    if question and not file_changes:
        print(f"❓ Asking for clarification: {question}")
        comment_on_issue(
            issue_number,
            f"🤖 Preciso de um detalhe pra resolver isso:\n\n> {question}\n\n"
            f"Responda aqui nesta issue e eu sigo com a correção.\n{NEEDS_INPUT_MARKER}",
        )
        return 0

    if not file_changes:
        print("⚠️ No file changes generated.")
        return 0

    kept, skipped = generator.filter_workflow_files(file_changes)
    if not kept:
        note = (
            f"🤖 As mudanças geradas tocaram só arquivos de workflow "
            f"({', '.join(skipped)}), que este bot não pode dar push. Nenhum PR "
            f"aberto — adicione manualmente ou use um PAT com escopo `workflow`."
        )
        print(f"⚠️ {note}")
        comment_on_issue(issue_number, note)
        return 0

    pr_title = data.get("pr_title") or f"fix: resolve issue #{issue_number}"
    pr_body = data.get("pr_description") or f"Automated PR for issue #{issue_number}."
    if f"#{issue_number}" not in pr_body:
        pr_body += f"\n\nFixes #{issue_number}"

    print(f"🌿 Opening/updating PR for issue #{issue_number} …")
    ok, result = generator.open_pr(
        branch=f"auto-fix/issue-{issue_number}",
        title=pr_title, body=pr_body, file_changes=file_changes,
    )
    print(f"🎉 {result}" if ok else f"⚠️ PR note: {result}")
    return 0


def main() -> int:
    issue_number = os.environ.get("ISSUE_NUMBER")
    if not issue_number:
        print("Error: ISSUE_NUMBER is required", file=sys.stderr)
        return 1
    return handle_issue(
        issue_number,
        os.environ.get("ISSUE_TITLE", ""),
        os.environ.get("ISSUE_BODY", ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
