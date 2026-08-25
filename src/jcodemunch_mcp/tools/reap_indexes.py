"""Remove code indexes whose source tree is provably gone.

Nothing else ever removes an index. A test run that indexes a fixture project
under the OS temp directory leaves its index behind forever, and a dead handle
is not free: ``list_repos`` reads every index in the directory, dead handles are
the population a wrong ``resolve_repo`` answer can be drawn from, and each keeps
its content mirror on disk.

⚠⚠ THE PREDICATE IS THE WHOLE DESIGN, and "the root does not exist" is NOT it.

``Path.exists()`` answers False both for a path that is provably absent and for
one it could not read. Every corpus on the machine this was written for lives
under OneDrive, where a placeholder or offline root raises rather than
answering, so a reaper built on ``exists()`` would mass-delete live indexes the
first time the sync client was mid-flight. Every absence test here goes through
``os.stat`` and treats ONLY ``FileNotFoundError`` / ``NotADirectoryError`` as
proof; any other ``OSError`` means "cannot prove absent", which is not a reason
to delete anything.

The second narrowing applies the same instinct to scope: a provably-absent root
is reaped only under the OS temp directory, which no sync client manages. A root
that is provably absent elsewhere is REPORTED, never deleted, because "gone" and
"gone and never coming back" are different claims.

NO LOCK-WAIT ARM HERE, and that is a decision rather than an omission. The doc
twin passes ``lock_wait=False`` so a held lock (evidence the index is in use, and
so evidence against it being dead) makes the pass decline. ``IndexStore.delete_index``
takes no such parameter, and adding one would buy nothing: this pass only ever
deletes an index whose SOURCE TREE IS PROVABLY GONE, and nothing can be actively
indexing a tree that is not there. The liveness question the doc side answers
with a lock is already answered here by the predicate.

WHAT THIS DOES NOT COVER. An index rooted at a linked worktree that was removed
while its parent repo remains, which is the class the cross-repo plan describes.
Once the worktree directory is gone, ``is_linked_worktree`` cannot be called on
it: the evidence it reads (a ``.git`` FILE carrying ``gitdir:``) went with the
directory, leaving only the path string, and detecting a worktree by path
convention is what the sibling cards forbid. ``worktree_lineage_key`` is the
lead worth following and its semantics were not verified here.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

from ..storage.index_store import IndexStore

# Reap reasons, so a caller can tell WHY each index went.
REASON_TEMP_ROOT_GONE = "temp_root_gone"

# Report-only findings: a dead index this pass deliberately does not delete.
FINDING_ROOT_GONE_OUTSIDE_TEMP = "root_gone_outside_temp"
FINDING_ROOT_UNREADABLE = "root_unreadable"
FINDING_NO_SOURCE_ROOT = "no_source_root"


def _absence(path: Path) -> str:
    """``"present"``, ``"absent"`` (proven), or ``"unknown"`` (could not tell).

    The three-way answer is the point. Collapsing ``unknown`` into ``absent`` is
    what turns a reaper into a data-loss bug on any synced filesystem.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _under_temp(path: Path) -> bool:
    """True when ``path`` is inside the OS temp directory.

    Compared through ``realpath`` on both sides: on Windows the temp directory
    is routinely reached by an 8.3 short name, and a plain prefix test misses
    that and silently declines to reap.
    """
    try:
        temp = Path(os.path.realpath(tempfile.gettempdir())).resolve()
        # The stored root no longer exists, so realpath cannot canonicalise it;
        # normalise lexically instead, which is all that is available.
        candidate = Path(os.path.normpath(os.path.abspath(str(path))))
    except (OSError, ValueError):
        return False
    try:
        return candidate.is_relative_to(temp)
    except (OSError, ValueError):
        return False


def classify(source_root: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(reap_reason, finding)``; at most one is set.

    A ``reap_reason`` means delete it. A ``finding`` means something worth
    reporting that this pass will NOT delete.
    """
    if not source_root:
        # An index with no recorded root cannot be judged, and an unjudgeable
        # index is not a dead one.
        return None, FINDING_NO_SOURCE_ROOT
    root = Path(source_root).expanduser()
    state = _absence(root)
    if state == "present":
        return None, None
    if state == "unknown":
        return None, FINDING_ROOT_UNREADABLE
    if _under_temp(root):
        return REASON_TEMP_ROOT_GONE, None
    return None, FINDING_ROOT_GONE_OUTSIDE_TEMP


def reap_indexes(apply: bool = False, storage_path: Optional[str] = None) -> dict:
    """Find (and with ``apply``, delete) indexes whose source tree is gone.

    ⚠ Dry run by DEFAULT. A pass that deletes on its first invocation gives the
    operator no way to read the predicate's answer before trusting it, and this
    predicate is the entire risk.

    Deletion goes through ``IndexStore.delete_index``, which already removes the
    SQLite database, its WAL/SHM siblings, the meta and checksum sidecars and the
    content directory. ``force=True`` is passed because the corpus is gone: the
    unmigrated-JSON guard exists to protect data that a later ``index_folder``
    would rewrite, and there is no later ``index_folder`` for a tree that no
    longer exists.
    """
    store = IndexStore(base_path=storage_path) if storage_path else IndexStore()
    reaped, findings, failed = [], [], []

    for row in store.list_repos():
        repo = row.get("repo", "")
        if "/" not in repo:
            continue
        owner, _, name = repo.partition("/")
        source_root = row.get("source_root") or ""
        reason, finding = classify(source_root)
        if finding:
            findings.append({"repo": repo, "source_root": source_root,
                             "finding": finding})
            continue
        if not reason:
            continue
        entry = {"repo": repo, "source_root": source_root, "reason": reason}
        if apply:
            try:
                if not store.delete_index(owner, name, force=True):
                    failed.append({**entry, "error": "delete_index returned False"})
                    continue
            except Exception as exc:  # noqa: BLE001 - one bad index must not stop the pass
                failed.append({**entry, "error": f"{type(exc).__name__}: {exc}"})
                continue
        reaped.append(entry)

    return {
        "success": True,
        "applied": apply,
        "reaped": reaped,
        "reaped_count": len(reaped),
        "findings": findings,
        "failed": failed,
    }
