"""Whole-repository cybersecurity analysis with a Gemini managed agent.

Unlike review_pr.py (which reviews a single pull-request diff), this scans the
ENTIRE repository and writes a findings report to the GitHub Actions job
summary. It reuses review_pr.py's sandbox/streaming/parsing machinery and the
same pluggable review skill (default: security-review), so what counts as a
finding is defined by skills/<REVIEW_SKILL>/SKILL.md — no code change needed to
run a different kind of audit.

Flow (runs inside the Actions job):
  1. Load the review skill and build a fresh managed-agent sandbox with the
     repository pre-mounted at /workspace/repo.
  2. Ask the agent to explore the whole codebase and return JSON findings.
  3. On any Managed Agents error, fall back to a plain Gemini model, inlining
     the repository source (there is no sandbox to explore in that mode).
  4. Render the findings as Markdown into $GITHUB_STEP_SUMMARY (and stdout).

Required env: GEMINI_API_KEY, GITHUB_REPOSITORY, GITHUB_TOKEN (or GH_TOKEN).
Optional env: REVIEW_SKILL (default "security-review"), FAIL_ON (severity that
makes the job exit non-zero: critical|high|medium|low|info|never; default
"never"), BASE_AGENT, GEMINI_MODEL (fallback model, default "gemini-3.6-flash"),
GITHUB_SERVER_URL / GITHUB_STEP_SUMMARY (set by Actions).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

from google import genai

import memory
from github_client import GitHub
from review_pr import (
    BASE_AGENT,
    REPO_MOUNT,
    _extract_text,
    _is_connection_error,
    _parse_json,
    _run_streaming,
    build_environment,
    load_skill,
)
from schema import FINDINGS_SCHEMA, SEVERITY_EMOJI, SEVERITY_ORDER

SCAN_SYSTEM_INSTRUCTION = (
    "You are an automated security auditor scanning an ENTIRE code repository. "
    "What to look for — the focus areas, finding categories, and severity "
    "semantics — is defined by the review skill below; apply it faithfully and "
    "only report findings that belong to it. You MUST respond with a single JSON "
    "object that matches the requested schema and nothing else — no prose, no "
    "markdown fences. Anchor every finding to a real file path and line that "
    "exists in the repository; do not invent locations."
)

# Fallback-only: how much repository text to inline when the sandbox is down.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    "images", ".mypy_cache", ".pytest_cache",
}
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 600_000

# Hidden markers so we can update our own issue/comment in place instead of
# posting a new one every run.
TRACKING_MARKER = "<!-- cybersecurity-analysis:tracking -->"
PR_COMMENT_MARKER = "<!-- cybersecurity-analysis:pr-comment -->"


def build_scan_prompt(repo: str, pr_number: int | None = None) -> str:
    head = f"Perform a security audit of the repository {repo}, mounted at {REPO_MOUNT}.\n"
    if pr_number:
        head += (
            f"This run targets pull request #{pr_number}. First bring the mounted "
            f"repository to the PR's head state:\n"
            f"   git -C {REPO_MOUNT} fetch origin pull/{pr_number}/head\n"
            f"   git -C {REPO_MOUNT} checkout --detach FETCH_HEAD\n"
            f"(Authentication is injected automatically; never add credentials.)\n"
        )
    return (
        head
        + f"Explore the codebase — application source, configuration, CI "
        f"workflows, dependency manifests, and shell scripts — and identify "
        f"security vulnerabilities per the review skill in your instructions. "
        f"Read what you need to confirm each issue; be efficient and avoid "
        f"speculation — report a finding only when you can point to the specific "
        f"file and line that exhibits it.\n"
        f"List files with:  ls -R {REPO_MOUNT}  (ignore the .git directory).\n"
        f"Read a file with: cat {REPO_MOUNT}/<path>\n\n"
        f"Return ONLY the JSON object described by this schema (the `line` field "
        f"is the line number in the file where the issue occurs):\n\n"
        f"{json.dumps(FINDINGS_SCHEMA)}"
    )


def collect_repo_text(root: pathlib.Path) -> str:
    """Concatenate the repo's text files for the sandbox-less fallback path."""
    chunks: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary asset
        rel = path.relative_to(root).as_posix()
        block = f"\n===== FILE: {rel} =====\n{text}\n"
        if total + len(block) > MAX_TOTAL_BYTES:
            chunks.append(f"\n[... repository truncated at {MAX_TOTAL_BYTES} bytes ...]\n")
            break
        chunks.append(block)
        total += len(block)
    return "".join(chunks)


def run_scan(
    client: genai.Client,
    repo: str,
    environment: dict[str, Any],
    prompt: str,
    system_instruction: str,
) -> tuple[dict[str, Any], str]:
    """Run the audit, retrying transport hiccups and falling back to a plain
    Gemini model on any Managed Agents error. Returns (findings, mode)."""
    last_exc: Exception | None = None
    for attempt_try in (1, 2):
        try:
            text, interaction = _run_streaming(
                client,
                agent=BASE_AGENT,
                system_instruction=system_instruction,
                input=prompt,
                environment=environment,
            )
            raw = text or _extract_text(interaction)
            return _parse_json(raw), "managed agent"
        except Exception as exc:
            last_exc = exc
            print(f"⚠️  scan attempt (try {attempt_try}) failed: {exc}", file=sys.stderr)
            if attempt_try == 1 and _is_connection_error(exc):
                continue  # transport hiccup: one more try
            break

    model_name = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    print(
        f"ℹ️ Managed Agents unavailable ({last_exc}). Falling back to standard "
        f"Gemini model ('{model_name}') …"
    )
    code = collect_repo_text(pathlib.Path.cwd())
    fallback_prompt = (
        build_scan_prompt(repo)
        + "\n\nThe managed sandbox is unavailable, so the repository source is "
        "inlined below. Anchor every finding to one of these file paths and its "
        "line number.\n"
        + code
    )
    response = client.models.generate_content(
        model=model_name,
        contents=fallback_prompt,
        config={
            "system_instruction": system_instruction,
            "response_mime_type": "application/json",
        },
    )
    return _parse_json(response.text or ""), f"fallback model ({model_name})"


def render_report(repo: str, result: dict[str, Any], skill_title: str, mode: str) -> str:
    findings = sorted(
        result.get("findings", []),
        key=lambda f: SEVERITY_ORDER.get(f.get("severity", "info"), 9),
    )
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    badge = (
        " · ".join(
            f"{SEVERITY_EMOJI.get(s, '')} {n} {s}"
            for s, n in sorted(counts.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))
        )
        or "✅ No findings"
    )

    lines = [
        f"# 🔒 Cybersecurity analysis — {repo}",
        "",
        result.get("summary") or "_No summary provided._",
        "",
        f"**Findings:** {badge}",
        "",
        f"<sub>analysis mode: {mode} · skill: {skill_title}</sub>",
        "",
    ]
    for f in findings:
        sev = f.get("severity", "info")
        emoji = SEVERITY_EMOJI.get(sev, "")
        loc = f.get("file", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"### {emoji} {sev.upper()} — {f.get('title', '(untitled)')}")
        lines.append("")
        lines.append(f"- **Location:** `{loc}`")
        lines.append(f"- **Category:** {f.get('category', '-')}")
        if f.get("detail"):
            lines.append(f"- **Detail:** {f['detail']}")
        if f.get("recommendation"):
            lines.append(f"- **Recommendation:** {f['recommendation']}")
        lines.append("")
    return "\n".join(lines)


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if run_id and repo else ""


def _posted_body(report: str, marker: str) -> str:
    """The report plus an update footer and a hidden marker for in-place upsert."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    foot = f"\n\n<sub>🤖 Automated cybersecurity analysis · updated {ts}"
    url = _run_url()
    if url:
        foot += f" · [run log]({url})"
    foot += "</sub>\n" + marker
    return report + foot


def detect_pr_number() -> int | None:
    """PR number from PR_NUMBER or the Actions pull_request event, else None."""
    n = os.environ.get("PR_NUMBER", "").strip()
    if n.isdigit():
        return int(n)
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and pathlib.Path(event_path).is_file():
        try:
            event = json.loads(pathlib.Path(event_path).read_text(encoding="utf-8"))
            num = (event.get("pull_request") or {}).get("number")
            if num is not None:
                return int(num)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
            # A malformed or unexpectedly-shaped event file is not a PR context.
            return None
    return None


def upsert_pr_comment(gh: GitHub, pr_number: int, report: str) -> str:
    """Post the report as a PR comment, updating our previous one in place."""
    body = _posted_body(report, PR_COMMENT_MARKER)
    try:
        for c in reversed(gh.list_issue_comments(pr_number)):
            if PR_COMMENT_MARKER in (c.get("body") or ""):
                gh.update_issue_comment(c["id"], body)
                return f"updated comment on PR #{pr_number}"
        gh.post_issue_comment(pr_number, body)
        return f"created comment on PR #{pr_number}"
    except Exception as exc:
        print(f"⚠️  could not post PR comment: {exc}", file=sys.stderr)
        return "PR comment failed"


def upsert_tracking_issue(gh: GitHub, title: str, report: str) -> str:
    """Maintain a single tracking issue, updating (and reopening) it in place."""
    body = _posted_body(report, TRACKING_MARKER)
    try:
        for issue in gh.list_issues(state="all"):
            if TRACKING_MARKER in (issue.get("body") or ""):
                gh.update_issue(
                    issue["number"],
                    body=body,
                    state="open" if issue.get("state") == "closed" else None,
                )
                return f"updated tracking issue #{issue['number']}"
        created = gh.create_issue(title, body)
        return f"created tracking issue #{created['number']}"
    except Exception as exc:
        print(f"⚠️  could not upsert tracking issue: {exc}", file=sys.stderr)
        return "tracking issue failed"


def main() -> int:
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        os.environ["GEMINI_API_KEY"]  # fail fast; the client reads it itself
    except KeyError as exc:
        print(f"❌ Missing required env variable: {exc}", file=sys.stderr)
        return 1

    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    skill_name = os.environ.get("REVIEW_SKILL", "security-review")
    fail_on = os.environ.get("FAIL_ON", "never").lower()
    report_issue = os.environ.get("REPORT_ISSUE", "1") == "1"
    report_pr_comment = os.environ.get("REPORT_PR_COMMENT", "1") == "1"
    pr_number = detect_pr_number()

    skill_title, skill_text = load_skill(skill_name)
    system_instruction = f"{SCAN_SYSTEM_INSTRUCTION}\n\n# Review skill\n\n{skill_text}"
    mem = memory.render_memory(memory.load_memory(pathlib.Path.cwd()), "scan")
    if mem:
        system_instruction += f"\n\n{mem}"

    environment = build_environment(
        clone_url=f"{server_url}/{repo}.git",
        skill_name=skill_name,
        skill_text=skill_text,
        gh_token=gh_token,
        # A token means we can (and, for private repos, must) authenticate the
        # clone; injecting it is harmless for public repos. No gh CLI needed.
        private=bool(gh_token),
        enable_gh=False,
    )
    prompt = build_scan_prompt(repo, pr_number)

    ctx = f"PR #{pr_number}" if pr_number else "the default branch"
    print(f"🔒 Scanning {repo} ({ctx}) with skill '{skill_name}' …")
    client = genai.Client()
    result, mode = run_scan(client, repo, environment, prompt, system_instruction)
    findings = result.get("findings", [])
    print(f"Scan completed ({mode}): {len(findings)} finding(s).")

    report = render_report(repo, result, skill_title, mode)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    print("\n" + report)

    # Post the report: a PR comment in PR context, else the tracking issue.
    if gh_token:
        gh = GitHub(api_url, repo, gh_token)
        if pr_number and report_pr_comment:
            print(f"💬 {upsert_pr_comment(gh, pr_number, report)}.")
        elif not pr_number and report_issue:
            title = os.environ.get("TRACKING_ISSUE_TITLE", "🔒 Security scan report")
            print(f"📋 {upsert_tracking_issue(gh, title, report)}.")
    else:
        print("ℹ️ No GitHub token; skipping issue/PR posting.")

    if fail_on != "never":
        threshold = SEVERITY_ORDER.get(fail_on)
        if threshold is None:
            print(f"⚠️  Unknown FAIL_ON value '{fail_on}'; not failing the job.", file=sys.stderr)
        elif any(
            SEVERITY_ORDER.get(f.get("severity", "info"), 9) <= threshold
            for f in findings
        ):
            print(f"❌ Findings at or above '{fail_on}' severity — failing the job.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
