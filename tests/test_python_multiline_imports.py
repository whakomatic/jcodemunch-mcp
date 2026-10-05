"""A `from x import (...)` that spans lines records every name it imports.

`_PY_FROM` matches one line, so a parenthesised import kept only the names on
the line holding `import (`. On a repo that wraps its imports this way
`find_references` returned no importer for a name on a continuation line, and
every tool that reads an edge's `names` saw a shorter list than the file
imports.

A second `from x import ...` for a module the file already imported lost its
names the same way: the edge is keyed on the specifier and the first statement
won.
"""

from jcodemunch_mcp.parser.imports import _extract_python_imports


def _names(content: str, specifier: str) -> list[str]:
    edges = [e for e in _extract_python_imports(content) if e["specifier"] == specifier]
    assert len(edges) == 1, edges
    return edges[0]["names"]


def test_parenthesised_import_records_the_names_on_every_line():
    content = (
        "from validate import (judge_repo_fails, attempt_fails,\n"
        "    designer_conditions_count_fails, check_summary)  # noqa: F401\n"
        "x = 1\n"
    )
    assert _names(content, "validate") == [
        "judge_repo_fails", "attempt_fails", "designer_conditions_count_fails", "check_summary",
    ]


def test_parenthesis_alone_on_the_first_line():
    content = "from checks import (\n    watch,\n    tainted as bad,\n)\n"
    assert _names(content, "checks") == ["watch", "tainted"]


def test_a_comment_inside_the_parentheses_adds_no_name_and_does_not_close_them():
    content = (
        "from checks import (watch,  # the watcher (see run_checks), kept\n"
        "    tainted)\n"
    )
    assert _names(content, "checks") == ["watch", "tainted"]


def test_backslash_continuation_records_the_next_line():
    content = "from checks import watch, \\\n    tainted\ny = 2\n"
    assert _names(content, "checks") == ["watch", "tainted"]


def test_the_statement_after_a_multiline_import_is_not_read_as_names():
    content = "from checks import (watch,\n    tainted)\nfrom layout import round_cards\n"
    assert _names(content, "checks") == ["watch", "tainted"]
    assert _names(content, "layout") == ["round_cards"]


def test_an_unclosed_parenthesis_stops_at_the_first_line_that_is_not_a_name_list():
    content = "from checks import (watch,\n    tainted\ndef f():\n    return g(1)\n"
    assert _names(content, "checks") == ["watch", "tainted"]


def test_a_second_import_of_one_module_adds_its_names():
    content = "from checks import watch\nfrom checks import tainted, watch\n"
    assert _names(content, "checks") == ["watch", "tainted"]


def test_each_name_on_a_continuation_line_gets_its_submodule_edge():
    content = "from pkg import (alpha,\n    beta)\n"
    specifiers = [e["specifier"] for e in _extract_python_imports(content)]
    assert specifiers == ["pkg", "pkg.alpha", "pkg.beta"]


def test_single_line_import_is_unchanged():
    assert _names("from checks import (watch, tainted)\n", "checks") == ["watch", "tainted"]
    assert _names("from checks import watch as w, tainted\n", "checks") == ["watch", "tainted"]
