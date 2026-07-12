"""Junction indexing: index files reached through a Windows junction inside
the indexed root, keyed by logical path (spec: docs/superpowers/specs/
2026-07-12-junction-indexing-design.md)."""
import os

import pytest

from pathlib import Path

from jcodemunch_mcp.security import is_junction

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows junctions")


def _make_junction(src: Path, dst: Path) -> None:
    """Create a junction at src pointing to dst (no admin needed)."""
    import _winapi
    _winapi.CreateJunction(str(dst), str(src))


class TestIsJunction:
    def test_false_on_posix_or_regular_dir(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert is_junction(d) is False

    def test_false_for_missing_path(self, tmp_path):
        assert is_junction(tmp_path / "nope") is False

    @windows_only
    def test_true_for_junction(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        junc = tmp_path / "junc"
        _make_junction(junc, target)
        assert is_junction(junc) is True

    @windows_only
    def test_false_for_symlink_dir(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted (no Developer Mode)")
        assert is_junction(link) is False


from jcodemunch_mcp.tools.index_folder import (
    _build_index_filters,
    _should_index_file,
)


def _cfg(root: Path):
    return _build_index_filters(root=root.resolve())


@windows_only
class TestJunctionFilter:
    def _layout(self, tmp_path):
        """root/src/shared/Service is a junction -> outside/Service."""
        outside = tmp_path / "outside" / "Service"
        outside.mkdir(parents=True)
        (outside / "util.ts").write_text("export const x = 1;\n")
        root = tmp_path / "root"
        (root / "src" / "shared").mkdir(parents=True)
        _make_junction(root / "src" / "shared" / "Service", outside)
        return root, outside

    def test_junction_escape_accepted_with_logical_rel_path(self, tmp_path):
        root, _ = self._layout(tmp_path)
        f = root / "src" / "shared" / "Service" / "util.ts"
        ok, reason, rel_path, warning = _should_index_file(f, _cfg(root), [])
        assert ok is True
        assert rel_path == "src/shared/Service/util.ts"
        assert warning is None

    def test_junction_target_inside_root_unchanged(self, tmp_path):
        root = tmp_path / "root"
        target = root / "real"
        target.mkdir(parents=True)
        (target / "mod.py").write_text("def f():\n    pass\n")
        _make_junction(root / "alias", target)
        f = root / "alias" / "mod.py"
        ok, reason, rel_path, _ = _should_index_file(f, _cfg(root), [])
        # Resolved path is under root -> current behavior: resolved rel_path.
        assert ok is True
        assert rel_path == "real/mod.py"

    def test_plain_traversal_still_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "elsewhere.py"
        outside.write_text("x = 1\n")
        ok, reason, rel_path, warning = _should_index_file(outside, _cfg(root), [])
        assert ok is False
        assert reason == "path_traversal"

    def test_symlink_dir_escape_still_rejected(self, tmp_path):
        outside = tmp_path / "outside2"
        outside.mkdir()
        (outside / "a.py").write_text("x = 1\n")
        root = tmp_path / "root2"
        root.mkdir()
        link = root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted (no Developer Mode)")
        ok, reason, rel_path, _ = _should_index_file(link / "a.py", _cfg(root), [])
        assert ok is False
        assert reason == "path_traversal"


from jcodemunch_mcp.tools.index_folder import discover_local_files


@windows_only
class TestJunctionCycleGuard:
    def test_ancestor_junction_terminates_and_dedupes(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.py").write_text("x = 1\n")
        # Junction inside root pointing back AT root -> today: infinite walk.
        _make_junction(root / "loop", root)
        files, warnings, skip_counts = discover_local_files(root.resolve())
        names = sorted(str(f) for f in files)
        # Terminates, and a.py appears exactly once (real location wins).
        assert len(names) == 1
        assert names[0].endswith("a.py")


@windows_only
class TestJunctionEndToEnd:
    def test_discover_local_files_finds_escape_junction_content(self, tmp_path):
        """root/src/shared/Service is a junction -> outside/Service; the full
        discover_local_files walk (not just _should_index_file directly) must
        descend past the cycle guard into the junction subtree and return the
        file at its logical rel_path, usable through the returned Path."""
        outside = tmp_path / "outside" / "Service"
        outside.mkdir(parents=True)
        (outside / "util.ts").write_text("export const x = 1;\n")
        root = tmp_path / "root"
        (root / "src" / "shared").mkdir(parents=True)
        _make_junction(root / "src" / "shared" / "Service", outside)

        resolved_root = root.resolve()
        files, warnings, skip_counts = discover_local_files(resolved_root)

        rel_paths = {
            f: str(f.relative_to(resolved_root)).replace("\\", "/") for f in files
        }
        matches = [f for f, rel in rel_paths.items() if rel == "src/shared/Service/util.ts"]
        assert len(matches) == 1, f"expected exactly one match, found rel paths: {list(rel_paths.values())}"

        # The returned Path genuinely traverses the junction (not just
        # correctly labeled) — its content is readable.
        assert matches[0].read_text() == "export const x = 1;\n"
        assert skip_counts.get("path_traversal", 0) == 0


from jcodemunch_mcp.tools.index_file import index_file
from jcodemunch_mcp.tools.index_folder import index_folder


@windows_only
class TestJunctionReachesSavedIndex:
    """Discovery admitting a junction file is not enough — it must survive the
    rest of the pipeline and land in the saved index. The discovery-only test
    above passed while index_folder silently dropped every junction file at its
    redundant post-discovery validate_path gate."""

    def _tree(self, tmp_path):
        outside = tmp_path / "outside" / "Service"
        outside.mkdir(parents=True)
        (outside / "util.ts").write_text("export function shared() { return 1; }\n")
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        (root / "src" / "main.ts").write_text("export function local() { return 2; }\n")
        (root / "src" / "shared").mkdir()
        _make_junction(root / "src" / "shared" / "Service", outside)
        return root

    def test_index_folder_indexes_junction_content(self, tmp_path):
        root = self._tree(tmp_path)
        result = index_folder(
            str(root), use_ai_summaries=False, storage_path=str(tmp_path / "store")
        )
        assert result["success"] is True
        assert "src/shared/Service/util.ts" in result["files"]
        assert result["file_count"] == 2

    def test_index_file_updates_junction_file(self, tmp_path):
        root = self._tree(tmp_path)
        store = str(tmp_path / "store")
        index_folder(str(root), use_ai_summaries=False, storage_path=store)
        result = index_file(
            str(root / "src" / "shared" / "Service" / "util.ts"),
            use_ai_summaries=False,
            storage_path=store,
        )
        assert result["success"] is True, result.get("error")

    def test_index_folder_still_rejects_symlink_dir_escape(self, tmp_path):
        """The junction carve-out must not become a symlink carve-out."""
        outside = tmp_path / "outside" / "Service"
        outside.mkdir(parents=True)
        (outside / "util.ts").write_text("export const x = 1;\n")
        root = tmp_path / "root"
        (root / "src" / "shared").mkdir(parents=True)
        (root / "src" / "main.ts").write_text("export function local() { return 2; }\n")
        try:
            (root / "src" / "shared" / "Service").symlink_to(
                outside, target_is_directory=True
            )
        except OSError:
            pytest.skip("symlink creation not permitted (no Developer Mode)")
        result = index_folder(
            str(root),
            use_ai_summaries=False,
            storage_path=str(tmp_path / "store"),
            follow_symlinks=True,
        )
        assert result["success"] is True
        assert "src/shared/Service/util.ts" not in result["files"]

    def test_index_file_rejects_symlink_escape_under_junction(self, tmp_path):
        """A symlink FILE inside the junction target must not ride the junction
        carve-out into the index under a logical in-root path."""
        secret = tmp_path / "secret.ts"
        secret.write_text("export const creds = 'hunter2';\n")
        root = self._tree(tmp_path)
        evil = tmp_path / "outside" / "Service" / "evil.ts"
        try:
            evil.symlink_to(secret)
        except OSError:
            pytest.skip("symlink creation not permitted (no Developer Mode)")
        store = str(tmp_path / "store")
        index_folder(str(root), use_ai_summaries=False, storage_path=store)
        result = index_file(
            str(root / "src" / "shared" / "Service" / "evil.ts"),
            use_ai_summaries=False,
            storage_path=store,
        )
        assert result["success"] is False
        assert "security validation" in result["error"]
