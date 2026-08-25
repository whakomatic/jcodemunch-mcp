"""Reaping a code index whose source tree is provably gone.

The twin of jdocmunch's reaper, and the predicate is the same design: absence
is a THREE-way question. ``Path.exists()`` returns False both for a path that
is provably absent and for one that could not be read, and every corpus on the
machine this was written for lives under OneDrive, where a placeholder or
offline root raises instead of answering. A reaper built on ``exists()``
mass-deletes live indexes the first time the sync client is mid-flight.
"""

import os
from pathlib import Path

import pytest

from jcodemunch_mcp.tools.reap_indexes import (
    FINDING_NO_SOURCE_ROOT,
    FINDING_ROOT_GONE_OUTSIDE_TEMP,
    FINDING_ROOT_UNREADABLE,
    REASON_TEMP_ROOT_GONE,
    classify,
)


class TestClassify:
    def test_a_live_root_is_neither_reaped_nor_reported(self, tmp_path):
        assert classify(str(tmp_path)) == (None, None)

    def test_a_gone_root_under_temp_is_reaped(self):
        import tempfile
        gone = Path(tempfile.gettempdir()) / "jcm-reap-fixture-that-never-existed"
        assert not gone.exists()
        assert classify(str(gone)) == (REASON_TEMP_ROOT_GONE, None)

    def test_a_gone_root_outside_temp_is_reported_not_reaped(self):
        reason, finding = classify("D:/jcm-reap-fixture/nowhere-at-all")
        assert reason is None
        assert finding == FINDING_ROOT_GONE_OUTSIDE_TEMP

    def test_an_index_with_no_recorded_root_is_reported_not_reaped(self):
        assert classify("") == (None, FINDING_NO_SOURCE_ROOT)

    def test_an_unreadable_root_survives(self, monkeypatch):
        """THE ONE THAT MATTERS: OSError is not proof of absence.

        Every other condition for reaping is satisfied here (the path is under
        the temp directory and stat fails), so only the three-way predicate keeps
        this index alive. Written against EIO because that is what a synced
        placeholder surfaces as.
        """
        import tempfile
        target = Path(tempfile.gettempdir()) / "jcm-reap-unreadable"
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            if str(path) == str(target):
                raise OSError(5, "Input/output error")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(os, "stat", fake_stat)
        reason, finding = classify(str(target))
        assert reason is None
        assert finding == FINDING_ROOT_UNREADABLE

    def test_exists_would_have_reaped_the_unreadable_root(self, monkeypatch):
        """Pins the bug the predicate exists to avoid, so a 'simplification'
        back to Path.exists() reds this test rather than passing quietly."""
        import tempfile
        target = Path(tempfile.gettempdir()) / "jcm-reap-unreadable-2"
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            if str(path) == str(target):
                raise OSError(5, "Input/output error")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(os, "stat", fake_stat)
        assert target.exists() is False        # what the naive predicate sees
        assert classify(str(target))[0] is None  # what this one does


class TestReapPass:
    def test_dry_run_by_default_deletes_nothing(self, tmp_path, monkeypatch):
        from jcodemunch_mcp.tools import reap_indexes as mod

        gone = Path(os.environ.get("TEMP", "/tmp")) / "jcm-reap-not-there"
        rows = [{"repo": "local/dead", "source_root": str(gone)}]
        deleted: list = []

        class FakeStore:
            def __init__(self, *a, **kw):
                pass

            def list_repos(self):
                return rows

            def delete_index(self, owner, name, force=False):
                deleted.append((owner, name))
                return True

        monkeypatch.setattr(mod, "IndexStore", FakeStore)
        result = mod.reap_indexes()
        assert result["applied"] is False
        assert result["reaped_count"] == 1
        assert deleted == []

    def test_apply_deletes_and_reports_what_went(self, tmp_path, monkeypatch):
        from jcodemunch_mcp.tools import reap_indexes as mod

        gone = Path(os.environ.get("TEMP", "/tmp")) / "jcm-reap-not-there"
        rows = [
            {"repo": "local/dead", "source_root": str(gone)},
            {"repo": "local/alive", "source_root": str(tmp_path)},
        ]
        deleted: list = []

        class FakeStore:
            def __init__(self, *a, **kw):
                pass

            def list_repos(self):
                return rows

            def delete_index(self, owner, name, force=False):
                deleted.append((owner, name))
                return True

        monkeypatch.setattr(mod, "IndexStore", FakeStore)
        result = mod.reap_indexes(apply=True)
        assert deleted == [("local", "dead")]
        assert [r["repo"] for r in result["reaped"]] == ["local/dead"]

    def test_one_failing_delete_does_not_stop_the_pass(self, monkeypatch):
        from jcodemunch_mcp.tools import reap_indexes as mod

        gone = Path(os.environ.get("TEMP", "/tmp")) / "jcm-reap-not-there"
        rows = [
            {"repo": "local/dead1", "source_root": str(gone)},
            {"repo": "local/dead2", "source_root": str(gone)},
        ]

        class FakeStore:
            def __init__(self, *a, **kw):
                pass

            def list_repos(self):
                return rows

            def delete_index(self, owner, name, force=False):
                if name == "dead1":
                    raise PermissionError("held open")
                return True

        monkeypatch.setattr(mod, "IndexStore", FakeStore)
        result = mod.reap_indexes(apply=True)
        assert [r["repo"] for r in result["failed"]] == ["local/dead1"]
        assert [r["repo"] for r in result["reaped"]] == ["local/dead2"]
