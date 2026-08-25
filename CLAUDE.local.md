# jcodemunch-mcp (local fork)

## This checkout
- Downstream of jgravelle/jcodemunch-mcp (origin). We pull from it.
- NEVER edit CHANGELOG.md or version numbers. Origin owns releases.
  Origin's own CLAUDE.md instructs updating them every release; that is
  written from the upstream maintainer's seat and does not apply here.
- Local divergence: UnrealScript support, junction indexing, and the
  per-file linked-worktree guard (`linked_worktree_between`, called from
  `index_file`'s containment loop).
- The top-level cli/ is origin's. Keep it byte-identical to origin/main
  even though its get_symbol import is broken upstream; a local fix would
  turn every future upstream edit there into a merge conflict. The sibling
  jcodemunch-cli project extends this checkout, it does not replace cli/.
- This file is kept on merge via .gitattributes (CLAUDE.md merge=ours),
  because origin rewrites its version every release. If a merge ever
  clobbers it, run: git config merge.ours.driver true

## Gotchas (not derivable from the code)
- index_folder is SYNC, dispatched via asyncio.to_thread in server.py.
- index_repo is async (httpx).
- has_index() distinguishes "no file on disk" from "file exists but
  version rejected".
- Symbol lookup is O(1) via the __post_init__ id dict in CodeIndex.
- Custom regex parsers exist where tree-sitter lacks clean named fields:
  Erlang (multi-clause merge by name/arity), Fortran (module as container),
  SQL (Jinja/dbt strip), Razor (@functions to C#).
- INDEX_VERSION is 17. Bumping it invalidates cache keys.
- Containment does not imply the worktree rule. `<repo>/.worktrees/<x>/f.py`
  is genuinely inside `<repo>`, and `_independent_repo_between` passes it
  through on purpose (a worktree shares the parent's history). Every path
  that resolves ownership by containment needs its own worktree test:
  #372 added one to the walk and the watcher fast path, and the per-file
  entry point kept admitting worktree files for want of the same check.
- Junction-mediated files are admitted by LOGICAL path. The one admission
  rule is _junction_logical_rel_path in tools/index_folder.py; containment
  re-checks that resolve the path (validate_path) will reject them.

## Environment reality
- Editable install: src/ is live, no reinstall needed after an edit. But the
  RUNNING MCP server still holds the old code, so restart it before trusting
  index_folder via MCP. (Verified: a fresh process indexes a junction tree at
  2 files while the running server returns 1.)
- The uv tool `jcodemunch-cli` SHARES this source: ../jcodemunch-cli declares
  jcodemunch-mcp = { path = "../jcodemunch-mcp", editable = true } under
  [tool.uv.sources], and uv tool install honors it (its venv has an
  _editable_impl_jcodemunch_mcp.pth pointing at this src/). So the CLI always
  runs current code with no reinstall, and it is the fresh-process escape
  hatch when the running MCP server is stale.
- `python -m pytest tests/ -q` leaves ~59 pre-existing environmental failures
  (semantic-search ONNX model, git subprocess under py3.14). Diff against a
  stashed baseline before blaming your own change.

## Reference
Key files, CLI subcommands, and env vars are derivable. Ask jcodemunch
(search_symbols / search_text) or run `jcodemunch-mcp <cmd> --help`.
