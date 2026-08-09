"""Shared machinery for AI-generated PRs (issue→PR and finding fixes).

Both `issue_to_pr.py` and `fix_findings.py` ask a model for a set of file
changes and open a PR from them. This module factors out that flow — the JSON
contract, the model call (routed through the LiteLLM proxy for cost tracking),
the workflow-file guard, and the git/PR plumbing — so the two callers stay thin
and behave identically where it matters (never pushing `.github/workflows/`,
never auto-merging).

Dependency-free beyond `requests`/`google-genai` (already required), so the
pure helpers are unit-testable without network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import llm
import memory

CHANGES_SCHEMA_HINT = (
    "You MUST respond with a single JSON object and nothing else:\n"
    "- 'pr_title': string (short, conventional-commit style)\n"
    "- 'pr_description': string (what changed and why)\n"
    "- 'file_changes': array of {'path': relative file path, 'content': the "
    "FULL new file contents}. Only include files you actually change."
)


def run_cmd(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, surfacing stderr on failure instead of swallowing it."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res


def is_workflow_path(path: str) -> bool:
    """True for files under .github/workflows/ — the Actions GITHUB_TOKEN cannot
    push these (needs a PAT with the 'workflow' scope), and auto-committing
    runnable CI is a security risk."""
    norm = os.path.normpath(str(path)).replace(os.sep, "/").lstrip("/")
    return norm.startswith(".github/workflows/")


def filter_workflow_files(
    file_changes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split changes into (kept, skipped_workflow_paths)."""
    kept, skipped = [], []
    for c in file_changes:
        (skipped.append(c.get("path", "")) if is_workflow_path(c.get("path", ""))
         else kept.append(c))
    return kept, skipped


def default_base() -> str:
    return os.environ.get("GITHUB_DEFAULT_BRANCH", "main")


def commit_and_push_paths(paths: list[str], message: str, branch: str | None = None) -> bool:
    """Stage `paths`, commit, and push to `branch` (rebase-retry once). Returns
    False (no error) when there is nothing to commit."""
    branch = branch or default_base()
    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    for p in paths:
        run_cmd(["git", "add", p], check=False)
    if run_cmd(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        return False
    run_cmd(["git", "commit", "-m", message])
    for _ in (1, 2):
        if run_cmd(["git", "push", "origin", f"HEAD:{branch}"], check=False).returncode == 0:
            return True
        run_cmd(["git", "fetch", "origin", branch], check=False)
        if run_cmd(["git", "rebase", f"origin/{branch}"], check=False).returncode != 0:
            run_cmd(["git", "rebase", "--abort"], check=False)
            break
    raise SystemExit("❌ Could not push ledger update after a rebase retry.")


def generate_changes(
    *,
    task: str,
    kind: str,
    session: str,
    memory_scope: str,
    extra_system: str = "",
    genai_client: Any | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Ask the model for {pr_title, pr_description, file_changes}.

    Routes through llm (LiteLLM proxy when configured). Raises ValueError if the
    reply is not parseable JSON.
    """
    system = (
        "You are an expert AI software engineer. Given a task, produce the "
        "precise file changes that satisfy it, editing as few files as "
        f"possible. {CHANGES_SCHEMA_HINT}"
    )
    mem = memory.render_memory(memory.load_memory("."), memory_scope)
    if mem:
        system += "\n\n" + mem
    if extra_system:
        system += "\n\n" + extra_system

    raw = llm.generate_json(
        prompt=task,
        system_instruction=system,
        kind=kind,
        session=session,
        genai_client=genai_client,
        model=model,
    ) or "{}"
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model did not return valid JSON: {exc}\n{raw[:500]}") from exc


def open_pr(
    *,
    branch: str,
    title: str,
    body: str,
    file_changes: list[dict[str, Any]],
    base: str | None = None,
    draft: bool = False,
    labels: list[str] | None = None,
) -> tuple[bool, str]:
    """Write the changes to a fresh branch and open a PR. Returns (ok, url_or_err).

    Skips .github/workflows/ files and notes the skip in the body. Opens the PR
    as a draft when `draft=True` (used by the fix flow so review/CI don't run
    until a human promotes it).
    """
    base = base or default_base()
    kept, skipped = filter_workflow_files(file_changes)
    if not kept:
        return False, f"only workflow files proposed ({', '.join(skipped)}); nothing pushable"
    if skipped:
        body += (
            f"\n\n> ⚠️ Skipped workflow file(s) {', '.join(skipped)} — the Actions "
            f"token cannot push to `.github/workflows/`."
        )

    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    run_cmd(["git", "checkout", "-B", branch])
    for change in kept:
        path = change["path"]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(change.get("content", ""))
        run_cmd(["git", "add", path])
        print(f"  ✓ {path}")
    run_cmd(["git", "commit", "-m", title])
    run_cmd(["git", "push", "-u", "origin", branch, "--force"])

    cmd = ["gh", "pr", "create", "--title", title, "--body", body,
           "--base", base, "--head", branch]
    if draft:
        cmd.append("--draft")
    for lab in labels or []:
        cmd += ["--label", lab]
    res = run_cmd(cmd, check=False)
    if res.returncode == 0:
        return True, res.stdout.strip()
    return False, (res.stderr.strip() or res.stdout.strip())
