"""Open draft fix PRs for open security findings, grouped by file/theme.

Runs in a trusted context after a scan (see security-fix.yml). For each group
of open findings at or above FIX_DRAFT_MIN_SEVERITY (default: high), it asks the
model for a fix and opens a DRAFT PR — draft so review/CI don't run until a
human promotes it (marks it ready or comments `/fix <id>`). Security fixes never
auto-merge.

The PR body carries a `fixes-findings:` marker with the finding ids, so when it
merges the ledger harvest closes exactly those ids.

Env: FIX_DRAFT_MIN_SEVERITY (default high), FIX_MAX_DRAFTS (default 5),
GEMINI_API_KEY or LITELLM_* (model), GITHUB_REPOSITORY, GITHUB_TOKEN.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from typing import Any

from google import genai

import generator
import ledger
import llm
from schema import SEVERITY_ORDER

FIX_BRANCH_PREFIX = "security-fix/"

# Finding-id markers live in ledger (shared with the harvest side).
build_fixes_marker = ledger.build_fixes_marker
parse_fixes_marker = ledger.parse_fixes_marker


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def group_key(finding: dict[str, Any]) -> str:
    """Group by file; findings with no file fall back to their category."""
    return finding.get("file") or f"category:{finding.get('category', 'misc')}"


def severity_ok(finding: dict[str, Any], floor: str) -> bool:
    floor_rank = SEVERITY_ORDER.get(floor, SEVERITY_ORDER["high"])
    return SEVERITY_ORDER.get(finding.get("severity", "info"), 9) <= floor_rank


def select_groups(
    records: list[dict[str, Any]], floor: str, max_drafts: int
) -> "dict[str, list[dict[str, Any]]]":
    """Open findings at/above `floor`, grouped by file/theme, capped to
    `max_drafts` groups (most-severe group first)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("status") != "open" or not severity_ok(r, floor):
            continue
        groups.setdefault(group_key(r), []).append(r)

    def group_rank(items: list[dict[str, Any]]) -> int:
        return min(SEVERITY_ORDER.get(i.get("severity", "info"), 9) for i in items)

    ordered = sorted(groups.items(), key=lambda kv: group_rank(kv[1]))
    return dict(ordered[: max(0, max_drafts)])


def branch_for(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:50] or "misc"
    return f"{FIX_BRANCH_PREFIX}{slug}"


def build_fix_task(key: str, findings: list[dict[str, Any]]) -> str:
    lines = [
        f"Fix the following security findings in `{key}`. Make the smallest "
        f"change that resolves each one; do not refactor unrelated code.\n"
    ]
    for f in findings:
        lines.append(
            f"- [{f.get('severity')}] {f.get('title')} (id {f.get('fingerprint')})\n"
            f"  {f.get('detail', '')}\n"
            f"  Recommended: {f.get('resolution') or f.get('body') or 'apply a correct fix'}"
        )
    lines.append(
        "\nThe pr_description must briefly explain each fix and list the finding ids."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def _draft_exists(branch: str) -> bool:
    res = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
        capture_output=True, text=True,
    )
    return res.returncode == 0 and res.stdout.strip() not in ("", "[]")


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("❌ GITHUB_REPOSITORY required", file=sys.stderr)
        return 1
    if not os.environ.get("GEMINI_API_KEY") and not llm.proxy_enabled():
        print("❌ Set GEMINI_API_KEY or LITELLM_BASE_URL", file=sys.stderr)
        return 1

    floor = os.environ.get("FIX_DRAFT_MIN_SEVERITY", "high").lower()
    try:
        max_drafts = int(os.environ.get("FIX_MAX_DRAFTS", "5"))
    except ValueError:
        max_drafts = 5

    root = pathlib.Path.cwd()
    groups = select_groups(ledger.load_all(root), floor, max_drafts)
    if not groups:
        print(f"No open findings at/above '{floor}' severity — no draft PRs.")
        return 0
    print(f"Opening draft fix PRs for {len(groups)} group(s) (floor={floor}, cap={max_drafts}) …")

    client = genai.Client() if os.environ.get("GEMINI_API_KEY") else None
    opened = 0
    for key, findings in groups.items():
        branch = branch_for(key)
        if _draft_exists(branch):
            print(f"  ↷ draft already open for {key} ({branch}); skipping.")
            continue
        ids = [f["fingerprint"] for f in findings]
        session = llm.session_id("fix", repo=repo, ref=ids[0])
        try:
            data = generator.generate_changes(
                task=build_fix_task(key, findings),
                kind="fix",
                session=session,
                memory_scope="scan",
                genai_client=client,
            )
        except ValueError as exc:
            print(f"  ⚠️ generation failed for {key}: {exc}", file=sys.stderr)
            continue
        changes = data.get("file_changes", [])
        if not changes:
            print(f"  ⚠️ no changes generated for {key}; skipping.")
            continue
        title = data.get("pr_title") or f"fix(security): {key}"
        sev_list = ", ".join(sorted({f.get("severity", "?") for f in findings}))
        body = (
            (data.get("pr_description") or f"Automated security fix for `{key}`.")
            + f"\n\nResolves {len(findings)} finding(s) [{sev_list}] in `{key}`.\n\n"
            + "> ⚠️ AI-generated security fix — **draft**. Review carefully, then mark "
            + "ready (or comment `/fix " + ids[0] + "`) to run review + CI. Never auto-merged.\n\n"
            + build_fixes_marker(ids)
        )
        ok, result = generator.open_pr(
            branch=branch, title=title, body=body, file_changes=changes,
            draft=True, labels=["security-fix"],
        )
        if ok:
            opened += 1
            print(f"  ✓ draft PR for {key}: {result}")
        else:
            print(f"  ⚠️ could not open PR for {key}: {result}", file=sys.stderr)

    print(f"Done: {opened} draft PR(s) opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
