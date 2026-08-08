"""Git-committed ledger of review findings — the reviewer's long-term memory.

Each finding is one Markdown file under `.pr-reviewer/findings/<fingerprint>.md`
(one file per finding to minimise merge conflicts), with YAML-ish frontmatter and
a one-paragraph detail snapshot. This module is deliberately dependency-free (no
network, no third-party YAML) so it is trivially unit-testable and safe to import
from both the review job and the trusted memory-sync job.

The ledger drives two behaviours:
  * READ (review_pr.py): findings the maintainers already resolved (`fixed`) or
    dismissed (`dismissed`/`wontfix`) are fed back to the agent so it stops
    re-raising them; still-`open` ones give continuity.
  * WRITE (ledger_sync.py, trusted contexts only): merged PRs upsert `open`
    records; `/dismiss` and `/reopen` commands flip status.

Fingerprints are computed here (not by the model) so they are stable across runs
even as line numbers drift: fp = sha1(norm_path\\ncategory\\nnorm_title)[:16].
Line numbers are intentionally excluded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
from typing import Any

LEDGER_DIR = ".pr-reviewer/findings"
STATUSES = ("open", "fixed", "dismissed", "wontfix")
SUPPRESSED = ("dismissed", "wontfix")

# Order matters: string fields first, then the free-text body after the fence.
_FRONTMATTER_FIELDS = (
    "fingerprint",
    "file",
    "category",
    "title",
    "severity",
    "status",
    "first_seen_pr",
    "first_seen",
    "last_seen",
    "last_seen_sha",
    "resolution",
)


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #
def _norm_path(path: str) -> str:
    # removeprefix (not lstrip) so hidden paths survive: lstrip("./") would eat
    # the leading dot of ".github/..." / ".pr-reviewer/...".
    return pathlib.PurePosixPath(str(path).strip().replace("\\", "/")).as_posix().removeprefix("./")


def _norm_title(title: str) -> str:
    """Lowercase, drop digits / line refs / punctuation, collapse whitespace.

    This absorbs the run-to-run wording jitter of the model's titles so the
    same underlying issue keeps the same fingerprint.
    """
    t = str(title).lower()
    t = re.sub(r"\blines?\s*\d+", " ", t)  # drop "line 42" / "lines 10-12" refs
    t = re.sub(r"[^a-z\s]+", " ", t)       # strip remaining digits & punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


def fingerprint(finding: dict[str, Any]) -> str:
    """Stable 16-hex id from file + category + normalized title (no line no.)."""
    basis = "\n".join(
        (
            _norm_path(finding.get("file", "")),
            str(finding.get("category", "")).strip().lower(),
            _norm_title(finding.get("title", "")),
        )
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Hidden state marker (shared by review_pr.py and ledger_sync.py)
# --------------------------------------------------------------------------- #
# The summary comment carries a base64-wrapped JSON marker: base64 keeps the
# payload on one line and immune to `-->` or braces appearing in model text, and
# lets the trusted harvest job read the findings without re-reviewing.
STATE_MARKER_RE = re.compile(r"<!-- github-pr-reviewer:state ([A-Za-z0-9+/=]+) -->")


def build_state_marker(state: dict[str, Any]) -> str:
    payload = base64.b64encode(
        json.dumps(state, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"<!-- github-pr-reviewer:state {payload} -->"


def parse_state_marker(text: str) -> dict[str, Any] | None:
    m = STATE_MARKER_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def marker_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """The compact per-finding record embedded in the state marker."""
    return {
        "fp": fingerprint(finding),
        "file": _norm_path(finding.get("file", "")),
        "category": str(finding.get("category", "")).strip(),
        "title": str(finding.get("title", "")).strip(),
        "severity": str(finding.get("severity", "info")).strip(),
    }


# --------------------------------------------------------------------------- #
# Record (de)serialization — a tiny frontmatter format, no YAML dependency
# --------------------------------------------------------------------------- #
def _dump(record: dict[str, Any]) -> str:
    lines = ["---"]
    for key in _FRONTMATTER_FIELDS:
        val = record.get(key, "")
        # Single-line scalars only; newlines would break the flat parser.
        val = str("" if val is None else val).replace("\n", " ").strip()
        lines.append(f"{key}: {val}")
    lines.append("---")
    body = str(record.get("detail", "")).strip()
    return "\n".join(lines) + ("\n\n" + body + "\n" if body else "\n")


def _load(text: str) -> dict[str, Any]:
    record: dict[str, Any] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return record
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            record[key.strip()] = val.strip()
        i += 1
    body = "\n".join(lines[i + 1 :]).strip() if i < len(lines) else ""
    record["detail"] = body
    # Normalize the two integer-ish fields when present.
    for k in ("first_seen_pr",):
        if str(record.get(k, "")).isdigit():
            record[k] = int(record[k])
    return record


def _record_path(root: pathlib.Path, fp: str) -> pathlib.Path:
    return root / LEDGER_DIR / f"{fp}.md"


# --------------------------------------------------------------------------- #
# Loading / querying
# --------------------------------------------------------------------------- #
def load_all(root: str | pathlib.Path) -> list[dict[str, Any]]:
    directory = pathlib.Path(root) / LEDGER_DIR
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        rec = _load(path.read_text(encoding="utf-8"))
        if rec.get("fingerprint"):
            records.append(rec)
    return records


def load_for_files(
    root: str | pathlib.Path, paths: list[str]
) -> list[dict[str, Any]]:
    """Records whose `file` is among `paths` (normalized comparison)."""
    wanted = {_norm_path(p) for p in paths}
    return [r for r in load_all(root) if _norm_path(r.get("file", "")) in wanted]


def get(root: str | pathlib.Path, fp: str) -> dict[str, Any] | None:
    path = _record_path(pathlib.Path(root), fp)
    if not path.is_file():
        return None
    return _load(path.read_text(encoding="utf-8"))


def suppressed_fingerprints(records: list[dict[str, Any]]) -> set[str]:
    return {r["fingerprint"] for r in records if r.get("status") in SUPPRESSED}


# --------------------------------------------------------------------------- #
# Mutations (used only by the trusted ledger_sync job)
# --------------------------------------------------------------------------- #
def _write(root: pathlib.Path, record: dict[str, Any]) -> pathlib.Path:
    path = _record_path(root, record["fingerprint"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(record), encoding="utf-8")
    return path


def upsert_open(
    root: str | pathlib.Path,
    finding: dict[str, Any],
    *,
    pr_number: int | None,
    sha: str,
    today: str,
) -> dict[str, Any]:
    """Create a new `open` record for a finding, or refresh an existing one.

    A finding that had been marked `fixed` and reappears is reopened; a
    `dismissed`/`wontfix` record is left untouched (the maintainer's decision
    stands, but we still bump last_seen).
    """
    root = pathlib.Path(root)
    fp = fingerprint(finding)
    existing = get(root, fp)
    if existing:
        record = dict(existing)
        record["last_seen"] = today
        record["last_seen_sha"] = sha
        if record.get("status") == "fixed":
            record["status"] = "open"
            record["resolution"] = ""
    else:
        record = {
            "fingerprint": fp,
            "file": _norm_path(finding.get("file", "")),
            "category": str(finding.get("category", "")).strip(),
            "title": str(finding.get("title", "")).strip(),
            "severity": str(finding.get("severity", "info")).strip(),
            "status": "open",
            "first_seen_pr": pr_number if pr_number is not None else "",
            "first_seen": today,
            "last_seen": today,
            "last_seen_sha": sha,
            "resolution": "",
            "detail": str(finding.get("detail", "")).strip(),
        }
    _write(root, record)
    return record


def mark_fixed(
    root: str | pathlib.Path,
    fp: str,
    *,
    sha: str,
    today: str,
    resolution: str = "no longer reported",
) -> dict[str, Any] | None:
    """Flip an `open` record to `fixed`. No-op for dismissed/wontfix/absent."""
    root = pathlib.Path(root)
    record = get(root, fp)
    if not record or record.get("status") != "open":
        return None
    record["status"] = "fixed"
    record["last_seen"] = today
    record["last_seen_sha"] = sha
    record["resolution"] = resolution
    _write(root, record)
    return record


def set_status(
    root: str | pathlib.Path,
    fp: str,
    status: str,
    *,
    today: str,
    resolution: str = "",
) -> dict[str, Any] | None:
    """Set an explicit status (used by /dismiss and /reopen)."""
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {STATUSES}")
    root = pathlib.Path(root)
    record = get(root, fp)
    if not record:
        return None
    record["status"] = status
    record["last_seen"] = today
    if resolution:
        record["resolution"] = resolution
    elif status == "open":
        record["resolution"] = ""
    _write(root, record)
    return record


# --------------------------------------------------------------------------- #
# Prompt rendering (read path)
# --------------------------------------------------------------------------- #
def render_prompt_context(records: list[dict[str, Any]]) -> str:
    """Two blocks for the review prompt: suppressed decisions and open history.

    Returns "" when there is no relevant memory, so the caller can skip the
    section entirely.
    """
    suppressed = [r for r in records if r.get("status") in SUPPRESSED]
    open_recs = [r for r in records if r.get("status") == "open"]
    if not suppressed and not open_recs:
        return ""

    parts: list[str] = ["## Review memory for the changed files\n"]
    if suppressed:
        parts.append(
            "The maintainers have already DECIDED these and do NOT want them "
            "reported again. Do not raise them under any wording:"
        )
        for r in suppressed:
            reason = f" — {r['resolution']}" if r.get("resolution") else ""
            parts.append(
                f"- [{r.get('status')}] `{r.get('file')}` · {r.get('category')} · "
                f"{r.get('title')}{reason} (id {r.get('fingerprint')})"
            )
        parts.append("")
    if open_recs:
        parts.append(
            "These were raised in earlier reviews of these files. If an issue is "
            "still present, report it (it keeps its id); if it is now resolved, "
            "say so in the summary instead of re-listing it:"
        )
        for r in open_recs:
            parts.append(
                f"- `{r.get('file')}` · {r.get('category')} · {r.get('title')} "
                f"(id {r.get('fingerprint')})"
            )
        parts.append("")
    return "\n".join(parts)
