"""Dependency-free tests for the fix-flow logic. Run: `python3 test_fix_findings.py`."""

from __future__ import annotations

import sys
import types

# Stub google/requests so imports succeed without the real deps.
for _n in ("google", "google.genai", "requests"):
    sys.modules[_n] = types.ModuleType(_n)
sys.modules["google"].genai = sys.modules["google.genai"]
sys.modules["google.genai"].Client = object

import fix_findings as F  # noqa: E402


def _rec(fp, file, sev, status="open", **extra):
    return {"fingerprint": fp, "file": file, "severity": sev, "status": status,
            "category": "security", "title": f"t-{fp}", **extra}


def test_group_key_and_severity_floor() -> None:
    assert F.group_key({"file": "a.py"}) == "a.py"
    assert F.group_key({"category": "authz"}) == "category:authz"
    assert F.severity_ok({"severity": "critical"}, "high")
    assert F.severity_ok({"severity": "high"}, "high")
    assert not F.severity_ok({"severity": "medium"}, "high")
    assert F.severity_ok({"severity": "low"}, "low")


def test_select_groups_filters_groups_caps_and_orders() -> None:
    recs = [
        _rec("a1", "auth.py", "high"),
        _rec("a2", "auth.py", "low"),          # same group, kept as company of a1
        _rec("b1", "util.py", "medium"),        # below floor -> excluded
        _rec("c1", "db.py", "critical"),
        _rec("d1", "old.py", "high", status="dismissed"),  # not open -> excluded
    ]
    groups = F.select_groups(recs, floor="high", max_drafts=5)
    # util.py has only a medium (below floor) -> its group is absent
    assert set(groups) == {"auth.py", "db.py"}, groups
    # most-severe group first: db.py (critical) before auth.py (high)
    assert list(groups)[0] == "db.py"
    # auth.py group includes the high finding (the low rode along via grouping only
    # if it were open+... actually low is below floor, so excluded from the group)
    assert [r["fingerprint"] for r in groups["auth.py"]] == ["a1"]


def test_select_groups_cap() -> None:
    recs = [_rec(f"f{i}", f"file{i}.py", "critical") for i in range(10)]
    assert len(F.select_groups(recs, "high", 3)) == 3
    assert len(F.select_groups(recs, "high", 0)) == 0


def test_branch_for_slug() -> None:
    assert F.branch_for("src/Auth/Login.ts") == "security-fix/src-auth-login-ts"
    assert F.branch_for("category:authz") == "security-fix/category-authz"


def test_fixes_marker_roundtrip() -> None:
    ids = ["9f2a3c1122334455", "deadbeefdeadbeef"]
    marker = F.build_fixes_marker(ids)
    assert F.parse_fixes_marker("body\n" + marker + "\ntail") == ids
    assert F.parse_fixes_marker("no marker") == []


def test_build_fix_task_mentions_ids() -> None:
    task = F.build_fix_task("auth.py", [_rec("a1", "auth.py", "high", detail="leak")])
    assert "auth.py" in task and "id a1" in task and "leak" in task


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} fix-flow tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
