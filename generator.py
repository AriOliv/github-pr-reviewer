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


def _norm(path: str) -> str:
    # Normalize backslashes first so a Windows-style path can't slip past the
    # posix checks below.
    return os.path.normpath(str(path).replace("\\", "/")).replace(os.sep, "/")


def is_workflow_path(path: str) -> bool:
    """True for files under .github/workflows/ — the Actions GITHUB_TOKEN cannot
    push these (needs a PAT with the 'workflow' scope), and auto-committing
    runnable CI is a security risk."""
    return _norm(path).lstrip("/").startswith(".github/workflows/")


def is_unsafe_path(path: str) -> bool:
    """True for paths that escape the repo root — absolute, or traversing up via
    '..'. AI-generated file_changes must never write outside the working tree."""
    norm = _norm(path)
    if norm.startswith("/") or os.path.isabs(path):
        return True
    return norm == ".." or norm.startswith("../")


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


def _select_writable(
    file_changes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(kept, skipped_workflow, unsafe) — drops repo-escaping and workflow paths."""
    unsafe = [c.get("path", "") for c in file_changes if is_unsafe_path(c.get("path", ""))]
    for p in unsafe:
        print(f"⛔ Refusing unsafe path outside the repo: {p}", file=sys.stderr)
    safe = [c for c in file_changes if not is_unsafe_path(c.get("path", ""))]
    kept, skipped = filter_workflow_files(safe)
    return kept, skipped, unsafe


def _write_changes(kept: list[dict[str, Any]]) -> None:
    for change in kept:
        path = change["path"]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(change.get("content", ""))
        run_cmd(["git", "add", path])
        print(f"  ✓ {path}")


def _git_identity() -> None:
    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])


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

    Skips .github/workflows/ and repo-escaping files. Opens the PR as a draft
    when `draft=True` (fix flow, so review/CI wait for a human to promote it).
    """
    base = base or default_base()
    kept, skipped, unsafe = _select_writable(file_changes)
    if not kept:
        note = ", ".join(skipped + unsafe) or "none"
        return False, f"no pushable files (workflow/unsafe only: {note})"
    if skipped:
        body += (
            f"\n\n> ⚠️ Skipped workflow file(s) {', '.join(skipped)} — the Actions "
            f"token cannot push to `.github/workflows/`."
        )

    _git_identity()
    # Branch off `base`, not the current HEAD: when open_pr runs in a loop
    # (fix_findings), each PR must be isolated, not stacked on the previous fix.
    run_cmd(["git", "checkout", "-B", branch, base])
    _write_changes(kept)
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


def apply_to_branch(
    *, branch: str, message: str, file_changes: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Update an EXISTING branch in place (no PR): fetch it, write the changes,
    commit, and push. Used by /refine to update an open PR. Returns (ok, info)."""
    kept, skipped, unsafe = _select_writable(file_changes)
    if not kept:
        note = ", ".join(skipped + unsafe) or "none"
        return False, f"no writable changes (workflow/unsafe only: {note})"

    _git_identity()
    fetched = run_cmd(["git", "fetch", "origin", branch], check=False)
    if fetched.returncode != 0:
        return False, f"branch '{branch}' not found on origin"
    run_cmd(["git", "checkout", "-B", branch, f"origin/{branch}"])
    _write_changes(kept)
    if run_cmd(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        return False, "the requested change produced no diff"
    run_cmd(["git", "commit", "-m", message])
    pushed = run_cmd(["git", "push", "origin", f"HEAD:{branch}"], check=False)
    if pushed.returncode != 0:
        return False, f"push failed: {pushed.stderr.strip()}"
    extra = f" (skipped {', '.join(skipped + unsafe)})" if (skipped or unsafe) else ""
    return True, f"updated {len(kept)} file(s){extra}"
