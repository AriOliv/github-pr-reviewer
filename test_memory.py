"""Dependency-free tests for the durable memory store. Run: `python3 test_memory.py`."""

from __future__ import annotations

import pathlib
import tempfile

import memory as M


def test_add_load_roundtrip_and_index() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    p = M.add(root, title="BASE_AGENT is an agent, not a model",
              body="A model id in BASE_AGENT triggers a 400; use the fallback.",
              scope="review, scan", tags="gemini,config")
    assert p.exists()
    facts = M.load_memory(root)
    assert len(facts) == 1
    f = facts[0]
    assert f["name"] == "base-agent-is-an-agent-not-a-model"
    assert f["scope"] == "review, scan" and "400" in f["body"]
    # index regenerated and references the fact
    idx = (root / M.MEMORY_DIR / M.INDEX_FILE).read_text()
    assert f["name"] + ".md" in idx and "(review, scan)" in idx


def test_scope_filtering() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    M.add(root, title="review only", body="b", scope="review", name="r")
    M.add(root, title="issue only", body="b", scope="issue", name="i")
    M.add(root, title="everywhere", body="b", scope="all", name="a")
    facts = M.load_memory(root)
    rev = M.render_memory(facts, "review")
    assert "review only" in rev and "everywhere" in rev and "issue only" not in rev
    iss = M.render_memory(facts, "issue")
    assert "issue only" in iss and "everywhere" in iss and "review only" not in iss
    scan = M.render_memory(facts, "scan")
    assert "everywhere" in scan and "review only" not in scan and "issue only" not in scan


def test_empty_and_missing_scope_defaults_to_all() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    # a fact whose scope is blank should apply to every bot
    M.add(root, title="global", body="b", scope="all", name="g")
    (root / M.MEMORY_DIR / "blank.md").write_text(
        "---\nname: blank\ntitle: blank scope\nscope: \ntags: \n---\n\nbody\n"
    )
    facts = M.load_memory(root)
    for bot in ("review", "issue", "scan"):
        assert "blank scope" in M.render_memory(facts, bot)
    assert M.render_memory([], "review") == ""


def test_invalid_scope_rejected() -> None:
    root = pathlib.Path(tempfile.mkdtemp())
    try:
        M.add(root, title="x", body="b", scope="bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} memory tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
