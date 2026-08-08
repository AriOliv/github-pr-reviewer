"""Dependency-free tests for the findings ledger. Run: `python3 test_ledger.py`.

No pytest, no network, no google-genai — just asserts, matching the repo's
lightweight style. Exits non-zero on the first failure.
"""

from __future__ import annotations

import pathlib
import tempfile

import ledger as L


def test_fingerprint_stability() -> None:
    a = {"file": "security_scan.py", "category": "correctness",
         "title": "Uncaught exceptions during event payload parsing", "line": 257}
    # Same issue, different case / line-number-in-title / path prefix → same id.
    b = {"file": "./security_scan.py", "category": "Correctness",
         "title": "Uncaught exceptions during event payload parsing (line 42)", "line": 42}
    assert L.fingerprint(a) == L.fingerprint(b)
    # Different category → different id.
    assert L.fingerprint(a) != L.fingerprint({**a, "category": "security"})


def test_dotfile_paths_are_preserved() -> None:
    # Regression: _norm_path must not eat the leading dot of hidden paths.
    root = pathlib.Path(tempfile.mkdtemp())
    f = {"file": ".github/workflows/review-memory.yml", "category": "security",
         "title": "workflow perm"}
    rec = L.upsert_open(root, f, pr_number=1, sha="s", today="d")
    assert rec["file"] == ".github/workflows/review-memory.yml", rec["file"]
    # ...and it is still findable by its real path.
    found = L.load_for_files(root, ["./.github/workflows/review-memory.yml"])
    assert len(found) == 1 and found[0]["fingerprint"] == rec["fingerprint"]


def test_marker_roundtrip_is_brace_and_arrow_safe() -> None:
    state = {"v": 1, "skill": "security-review", "head_sha": "abc",
             "findings": [{"fp": "a1", "file": "x.py", "category": "c",
                           "title": "t --> weird } brace", "severity": "low"}]}
    marker = L.build_state_marker(state)
    assert L.parse_state_marker("noise " + marker + " tail") == state
    assert L.parse_state_marker("no marker here") is None


def test_upsert_load_and_filter() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    a = {"file": "security_scan.py", "category": "correctness", "title": "A"}
    r = L.upsert_open(root, a, pr_number=6, sha="s1", today="2026-08-08")
    assert r["status"] == "open" and r["first_seen_pr"] == 6
    assert L.get(root, r["fingerprint"])["title"] == "A"
    L.upsert_open(root, {"file": "README.md", "category": "docs", "title": "typo"},
                  pr_number=1, sha="s", today="2026-08-08")
    subset = L.load_for_files(root, ["security_scan.py"])
    assert len(subset) == 1 and subset[0]["fingerprint"] == r["fingerprint"]


def test_mark_fixed_and_reopen() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    a = {"file": "f.py", "category": "c", "title": "A"}
    fp = L.upsert_open(root, a, pr_number=1, sha="s", today="d1")["fingerprint"]
    assert L.mark_fixed(root, fp, sha="s", today="d2")["status"] == "fixed"
    assert L.mark_fixed(root, fp, sha="s", today="d2") is None  # already fixed
    assert L.upsert_open(root, a, pr_number=2, sha="s", today="d3")["status"] == "open"


def test_dismiss_sticks_and_is_suppressed() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    a = {"file": "f.py", "category": "c", "title": "A"}
    fp = L.upsert_open(root, a, pr_number=1, sha="s", today="d1")["fingerprint"]
    L.set_status(root, fp, "dismissed", today="d2", resolution="by @ari: fp")
    # A dismissed finding must not be reopened by a later upsert.
    assert L.upsert_open(root, a, pr_number=2, sha="s", today="d3")["status"] == "dismissed"
    recs = L.load_for_files(root, ["f.py"])
    assert fp in L.suppressed_fingerprints(recs)
    try:
        L.set_status(root, fp, "bogus", today="d4")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_render_prompt_context() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    a = {"file": "f.py", "category": "c", "title": "Open one"}
    b = {"file": "f.py", "category": "c", "title": "Dismissed one"}
    L.upsert_open(root, a, pr_number=1, sha="s", today="d")
    fpb = L.upsert_open(root, b, pr_number=1, sha="s", today="d")["fingerprint"]
    L.set_status(root, fpb, "dismissed", today="d", resolution="nope")
    ctx = L.render_prompt_context(L.load_for_files(root, ["f.py"]))
    assert "already DECIDED" in ctx and "earlier reviews" in ctx and fpb in ctx
    assert L.render_prompt_context([]) == ""


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} ledger tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
