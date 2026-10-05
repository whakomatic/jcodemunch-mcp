"""`alias.name()` through a module that re-exports `name` is a call to `name`.

`vulcan.py` does `from validate import check_summary`, and a step script does
`import vulcan as v` then `v.check_summary(...)`. The callers query only read
files that import the defining module, so the step script, which imports
`vulcan` and not `validate`, was never a candidate: `get_call_hierarchy`
callers and `get_blast_radius` at depth 1 both missed it, while depth 2 found
it only by walking the importers of `vulcan.py`.

A direct `import validate as x` already resolved (the importer is a candidate
and the call records the bare name); the re-export is the one-hop gap.
"""

from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius
from jcodemunch_mcp.tools.get_call_hierarchy import get_call_hierarchy
from jcodemunch_mcp.tools.index_folder import index_folder

_TARGET = "validate.py::check_summary#function"


def _build(tmp_path):
    src = tmp_path / "src"
    store = tmp_path / "store"
    (src / "steps").mkdir(parents=True)
    store.mkdir()
    (src / "validate.py").write_text("def check_summary(path):\n    return path\n")
    (src / "other.py").write_text("def unrelated():\n    return 1\n")
    (src / "vulcan.py").write_text(
        "from validate import check_summary  # noqa: F401\n\n"
        "def dispatch(p):\n    return check_summary(p)\n"
    )
    (src / "direct.py").write_text(
        "import validate as val\n\ndef via_direct(p):\n    return val.check_summary(p)\n"
    )
    (src / "steps" / "plan.py").write_text(
        "import vulcan as v\n\ndef preflight(p):\n    return v.check_summary(p)\n"
    )
    (src / "steps" / "triage.py").write_text(
        "import vulcan\n\ndef run(p):\n    return vulcan.check_summary(p)\n"
    )
    (src / "steps" / "bystander.py").write_text(
        "import vulcan as v\n\ndef run(p):\n    return v.dispatch(p)\n"
    )
    (src / "steps" / "noexport.py").write_text(
        "import other\n\ndef run(p):\n    return other.unrelated()\n"
    )
    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store)


def _caller_files(tmp_path):
    repo, store = _build(tmp_path)
    r = get_call_hierarchy(repo=repo, symbol_id=_TARGET, direction="callers", depth=1, storage_path=store)
    return {c["file"] for c in r["callers"]}


def test_a_call_through_a_module_that_re_exports_the_name_is_a_caller(tmp_path):
    files = _caller_files(tmp_path)
    assert {"steps/plan.py", "steps/triage.py"} <= files


def test_a_direct_module_import_is_still_a_caller(tmp_path):
    files = _caller_files(tmp_path)
    assert {"vulcan.py", "direct.py"} <= files


def test_an_importer_of_the_re_exporter_that_never_names_the_symbol_is_not_a_caller(tmp_path):
    assert "steps/bystander.py" not in _caller_files(tmp_path)


def test_a_file_that_imports_an_unrelated_module_is_not_a_caller(tmp_path):
    assert "steps/noexport.py" not in _caller_files(tmp_path)


def test_blast_radius_at_depth_one_confirms_the_re_export_importers(tmp_path):
    repo, store = _build(tmp_path)
    r = get_blast_radius(repo=repo, symbol=_TARGET, depth=1, storage_path=store)
    files = {c["file"] for c in r["confirmed"]}
    assert {"vulcan.py", "direct.py", "steps/plan.py", "steps/triage.py"} <= files
    assert "steps/bystander.py" not in files
    assert "steps/noexport.py" not in files


def test_blast_radius_counts_the_name_in_a_re_export_importer(tmp_path):
    repo, store = _build(tmp_path)
    r = get_blast_radius(repo=repo, symbol=_TARGET, depth=1, storage_path=store)
    refs = {c["file"]: c["references"] for c in r["confirmed"]}
    assert refs["steps/plan.py"] == 1
