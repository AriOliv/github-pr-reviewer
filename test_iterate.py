"""Dependency-free tests for the iterative-loop helpers. Run: `python3 test_iterate.py`."""

from __future__ import annotations

import sys
import types

for _n in ("google", "google.genai", "requests"):
    sys.modules[_n] = types.ModuleType(_n)
sys.modules["google"].genai = sys.modules["google.genai"]
sys.modules["google.genai"].Client = object

import iterate as I  # noqa: E402


def test_parse_refine() -> None:
    assert I.parse_refine("/refine also handle empty input") == "also handle empty input"
    assert I.parse_refine("/refine\nuse a guard clause") == "use a guard clause"
    assert I.parse_refine("/REFINE fix casing") == "fix casing"
    assert I.parse_refine("/refine") == ""            # command, no feedback
    assert I.parse_refine("just a comment") is None   # not a command
    assert I.parse_refine("please /refine later") is None  # only at start


def test_is_bot_branch() -> None:
    assert I.is_bot_branch("auto-fix/issue-12")
    assert I.is_bot_branch("security-fix/auth-py")
    assert not I.is_bot_branch("main")
    assert not I.is_bot_branch("feature/x")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} iterate tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
