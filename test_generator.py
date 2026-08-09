"""Dependency-free tests for generator's pure helpers. Run: `python3 test_generator.py`."""

from __future__ import annotations

import os
import sys
import types

for _n in ("google", "google.genai", "requests"):
    sys.modules[_n] = types.ModuleType(_n)
sys.modules["google"].genai = sys.modules["google.genai"]
sys.modules["google.genai"].Client = object

import generator as G  # noqa: E402


def test_is_workflow_path() -> None:
    assert G.is_workflow_path(".github/workflows/ci.yml")
    assert G.is_workflow_path("./.github/workflows/ci.yml")
    assert not G.is_workflow_path("src/.github/workflows/x.yml")
    assert not G.is_workflow_path("README.md")


def test_is_workflow_path_backslash_and_traversal() -> None:
    # Backslashes must not slip a workflow file past the filter.
    assert G.is_workflow_path(".github\\workflows\\ci.yml")
    # Traversal that resolves into workflows is still caught.
    assert G.is_workflow_path("foo/../.github/workflows/ci.yml")


def test_is_unsafe_path() -> None:
    assert G.is_unsafe_path("/etc/passwd")
    assert G.is_unsafe_path("../outside.txt")
    assert G.is_unsafe_path("a/../../escape")
    assert not G.is_unsafe_path("src/app.ts")
    assert not G.is_unsafe_path("./a/b.py")


def test_filter_workflow_files() -> None:
    kept, skipped = G.filter_workflow_files(
        [{"path": "a.py"}, {"path": ".github/workflows/x.yml"}, {"path": "b.ts"}]
    )
    assert [c["path"] for c in kept] == ["a.py", "b.ts"]
    assert skipped == [".github/workflows/x.yml"]


def test_default_base() -> None:
    os.environ.pop("GITHUB_DEFAULT_BRANCH", None)
    assert G.default_base() == "main"
    os.environ["GITHUB_DEFAULT_BRANCH"] = "trunk"
    assert G.default_base() == "trunk"
    os.environ.pop("GITHUB_DEFAULT_BRANCH")


def test_open_pr_branches_off_base() -> None:
    # Regression: each PR branch must be created from `base`, not current HEAD,
    # so looped fix PRs stay isolated.
    import os as _os
    import tempfile
    cmds = []

    def fake_run(cmd, check=True):
        cmds.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="https://pr/1", stderr="")

    orig = G.run_cmd
    G.run_cmd = fake_run
    cwd = _os.getcwd()
    try:
        _os.chdir(tempfile.mkdtemp())
        ok, _ = G.open_pr(
            branch="security-fix/x", title="t", body="b",
            file_changes=[{"path": "a.py", "content": "x=1"}], base="main",
        )
    finally:
        G.run_cmd = orig
        _os.chdir(cwd)
    assert ok
    checkout = next(c for c in cmds if c[:3] == ["git", "checkout", "-B"])
    assert checkout == ["git", "checkout", "-B", "security-fix/x", "main"], checkout


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} generator tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
