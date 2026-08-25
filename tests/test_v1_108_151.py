"""Tests for v1.108.151 — nested-worktree exclusion + worktree identity (#372).

A linked `git worktree` under the indexed root (e.g. Claude Code's
`<repo>/.claude/worktrees/<name>`) must be treated as a working-tree
boundary: the discovery walk prunes it, the watcher fast path refuses
files inside it, and identity resolution never lets a worktree claim
(or match into) the parent repo's index slot.
"""

import subprocess
from pathlib import Path

from jcodemunch_mcp.storage.git_root import (
    _existing_git_identity,
    detect_git_root,
    is_linked_worktree,
    linked_worktree_between,
)
from jcodemunch_mcp.tools.index_folder import (
    _build_index_filters,
    _build_skip_dirs_regex,
    _should_index_file,
    discover_local_files,
    get_filtered_files,
)


def _fake_worktree(parent_root: Path, rel: str) -> Path:
    """Create a directory shaped like a linked worktree (no real git needed)."""
    wt = parent_root / rel
    wt.mkdir(parents=True)
    (wt / ".git").write_text(
        f"gitdir: {parent_root / '.git' / 'worktrees' / wt.name}\n",
        encoding="utf-8",
    )
    return wt


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )


class TestIsLinkedWorktree:
    def test_worktree_git_file_matches(self, tmp_path):
        wt = _fake_worktree(tmp_path, "wt")
        assert is_linked_worktree(wt) is True

    def test_submodule_git_file_does_not_match(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".git").write_text(
            "gitdir: ../.git/modules/sub\n", encoding="utf-8"
        )
        assert is_linked_worktree(sub) is False

    def test_git_directory_does_not_match(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        assert is_linked_worktree(repo) is False

    def test_plain_directory_does_not_match(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert is_linked_worktree(d) is False


class TestWalkPrunesNestedWorktrees:
    def _layout(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "real.py").write_text("x = 1\n", encoding="utf-8")
        wt = _fake_worktree(root, ".claude/worktrees/feature")
        (wt / "dupe.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def test_discover_local_files_skips_worktree(self, tmp_path):
        root = self._layout(tmp_path)
        files, _warnings, skip_counts = discover_local_files(root)
        rels = {f.name for f in files}
        assert "real.py" in rels
        assert "dupe.py" not in rels
        assert skip_counts["nested_worktree"] == 1

    def test_get_filtered_files_skips_worktree(self, tmp_path):
        root = self._layout(tmp_path)
        names = {Path(p).name for p in get_filtered_files(str(root))}
        assert "real.py" in names
        assert "dupe.py" not in names

    def test_fast_path_refuses_worktree_file(self, tmp_path):
        root = self._layout(tmp_path)
        cfg = _build_index_filters(
            root=root.resolve(),
            skip_dirs_regex=_build_skip_dirs_regex(),
        )
        target = root / ".claude" / "worktrees" / "feature" / "dupe.py"
        ok, reason, _rel, _warning = _should_index_file(target, cfg)
        assert ok is False
        assert reason == "nested_worktree"

    def test_fast_path_still_accepts_parent_file(self, tmp_path):
        root = self._layout(tmp_path)
        cfg = _build_index_filters(
            root=root.resolve(),
            skip_dirs_regex=_build_skip_dirs_regex(),
        )
        ok, _reason, rel, _warning = _should_index_file(root / "real.py", cfg)
        assert ok is True
        assert rel == "real.py"


class TestWorktreeIdentity:
    def test_real_worktree_gets_own_local_identity(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", cwd=repo)
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git("add", ".", cwd=repo)
        _git("commit", "-m", "init", cwd=repo)
        _git(
            "remote", "add", "origin",
            "https://github.com/acme/widget.git", cwd=repo,
        )
        wt = repo / ".claude" / "worktrees" / "feature"
        _git("worktree", "add", "-b", "feat", str(wt), cwd=repo)

        parent = detect_git_root(str(repo))
        assert (parent.owner, parent.name) == ("acme", "widget")

        ident = detect_git_root(str(wt))
        assert ident is not None
        assert ident.owner == "local"
        assert ident.name.startswith("feature-")
        assert ident.name != parent.name
        assert Path(ident.git_root).resolve() == wt.resolve()

    def test_existing_git_identity_stops_at_worktree_boundary(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        (root / "pkg").mkdir()
        wt = _fake_worktree(root, ".claude/worktrees/feature")
        (wt / "inner").mkdir()

        class _FakeStore:
            def list_repos(self):
                return [{"repo": "acme/widget", "git_root": str(root)}]

        store = _FakeStore()
        # A plain subdirectory of the parent still matches the parent slot.
        hit = _existing_git_identity((root / "pkg").resolve(), store)
        assert hit is not None
        assert (hit.owner, hit.name) == ("acme", "widget")
        # A path inside the nested worktree must NOT match the parent slot.
        assert _existing_git_identity(wt.resolve(), store) is None
        assert _existing_git_identity((wt / "inner").resolve(), store) is None


class TestLinkedWorktreeBetween:
    """The containment resolver's second question (see `index_file`).

    Containment alone answers "is the file under this root"; a worktree makes
    that true while the answer to "does this root own it" is still no.
    """

    def test_worktree_between_root_and_file(self, tmp_path):
        wt = _fake_worktree(tmp_path, ".worktrees/d3-pkg")
        assert linked_worktree_between(tmp_path, wt / "src" / "a.py") == wt

    def test_plain_file_under_root(self, tmp_path):
        assert linked_worktree_between(tmp_path, tmp_path / "src" / "a.py") is None

    def test_root_is_the_worktree(self, tmp_path):
        """An index rooted AT the worktree owns its own files."""
        wt = _fake_worktree(tmp_path, "wt")
        assert linked_worktree_between(wt, wt / "src" / "a.py") is None

    def test_submodule_is_not_a_worktree(self, tmp_path):
        """#372's boundary: submodule content IS indexed into the parent."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".git").write_text(
            "gitdir: ../.git/modules/sub\n", encoding="utf-8"
        )
        assert linked_worktree_between(tmp_path, sub / "a.py") is None

    def test_path_outside_root(self, tmp_path):
        outside = tmp_path.parent / "elsewhere" / "a.py"
        assert linked_worktree_between(tmp_path / "root", outside) is None
