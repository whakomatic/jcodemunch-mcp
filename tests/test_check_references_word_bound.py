"""check_references' content match is a whole word, not a substring.

`check_summary` returned every file holding `roundcheck_summary`, so a name
with a longer sibling was reported as referenced by files that never used it.
"""

from jcodemunch_mcp.tools.check_references import check_references
from jcodemunch_mcp.tools.index_folder import index_folder


def _files(tmp_path, identifier):
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "a.py").write_text("def roundcheck_summary():\n    return 1\n")
    (src / "b.py").write_text("x = roundcheck_summary()\ny = check_summary_fails\n")
    (src / "c.py").write_text("v = mod.Check_Summary(1)\n")
    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    r = check_references(repo=result["repo"], identifier=identifier, storage_path=str(store))
    return {c["file"] for c in r["content_references"]}


def test_a_longer_name_containing_the_identifier_is_not_a_match(tmp_path):
    assert _files(tmp_path, "check_summary") == {"c.py"}


def test_the_match_stays_case_insensitive(tmp_path):
    assert "c.py" in _files(tmp_path, "CHECK_SUMMARY")
