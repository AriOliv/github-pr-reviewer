import os
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_AGENT = "antigravity-preview-05-2026"


def _norm(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(p.anchor)
        except ValueError:
            pass
    normalized = Path(os.path.normpath(p))
    parts = [part for part in normalized.parts if part != ".."]
    return Path(*parts) if parts else Path(".")


def _run_git(args: List[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def apply_changes(repo_dir: str | Path, file_changes: List[Dict[str, Any]]) -> None:
    repo_path = Path(repo_dir).resolve()
    for change in file_changes:
        rel_path = _norm(change["path"])
        file_path = repo_path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(change["content"], encoding="utf-8")


def open_pr(
    repo_dir: str | Path,
    branch_name: str,
    pr_title: str,
    pr_description: str,
    file_changes: List[Dict[str, Any]],
) -> None:
    repo_path = Path(repo_dir).resolve()
    _run_git(["checkout", "-b", branch_name], cwd=repo_path)
    for change in file_changes:
        rel_path = _norm(change["path"])
        file_path = repo_path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(change["content"], encoding="utf-8")
        _run_git(["add", str(rel_path)], cwd=repo_path)
    _run_git(["commit", "-m", pr_title, "-m", pr_description], cwd=repo_path)
