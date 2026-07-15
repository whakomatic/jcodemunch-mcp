"""Tests for UnrealScript (.uc) language support.

UnrealScript has no tree-sitter grammar. Symbol extraction is regex-based,
following the same pattern as Verse and Blade. Covered constructs:

    class, function, event, state, const, struct, enum

Deferred to docs/future.md: var, delegate, operator, replication,
defaultproperties, call graph, import graph.

The inline ``SAMPLE_CLASS`` string is the primary test surface (same pattern
as ``test_sql_language.py``). ``tests/fixtures/unrealscript/Weapon.uc``
mirrors the same constructs on disk so that integration-style tests can
exercise the ``index_folder`` path as well.
"""
from pathlib import Path

import pytest

from jcodemunch_mcp.parser import parse_file
from jcodemunch_mcp.parser.imports import extract_imports, resolve_specifier
from jcodemunch_mcp.parser.languages import (
    LANGUAGE_EXTENSIONS,
    LANGUAGE_REGISTRY,
)

FIXTURE = Path(__file__).parent / "fixtures" / "unrealscript" / "Weapon.uc"


SAMPLE_CLASS = """\
// Engine actor example.
class TestActor extends Actor
    within Pawn
    config(Game)
    abstract
    native;

const MAX_HEALTH = 100;

enum EWeaponType
{
    WT_Pistol,
    WT_Rifle,
    WT_Shotgun
};

struct PlayerInfo
{
    var string Name;
    var int Score;
};

/** Compute damage from a base amount. */
simulated function int CalculateDamage(int BaseDamage, optional float Modifier)
{
    return BaseDamage * Modifier;
}

event PostBeginPlay()
{
    super.PostBeginPlay();
}

auto state Idle
{
    function Tick(float DeltaTime)
    {
        // idle tick
    }
}

state Attacking extends Idle
{
}

defaultproperties
{
    Health=100
    Name="Ignore this class=Fake inside defaults"
}
"""


@pytest.fixture(scope="module")
def symbols():
    return parse_file(SAMPLE_CLASS, "TestActor.uc", "unrealscript")


def _by_name(syms, name):
    return [s for s in syms if s.name == name]


def test_extension_mapping():
    assert LANGUAGE_EXTENSIONS[".uc"] == "unrealscript"


def test_registry_entry():
    assert "unrealscript" in LANGUAGE_REGISTRY


def test_class_extracted(symbols):
    matches = _by_name(symbols, "TestActor")
    assert len(matches) == 1, [s.name for s in symbols]
    cls = matches[0]
    assert cls.kind == "class"
    assert cls.language == "unrealscript"
    assert cls.line == 2
    assert "extends Actor" in cls.signature


def test_const_extracted(symbols):
    matches = _by_name(symbols, "MAX_HEALTH")
    assert len(matches) == 1
    assert matches[0].kind == "constant"


def test_enum_extracted(symbols):
    matches = _by_name(symbols, "EWeaponType")
    assert len(matches) == 1
    assert matches[0].kind == "type"
    # enum body spans multiple lines
    assert matches[0].end_line > matches[0].line


def test_struct_extracted(symbols):
    matches = _by_name(symbols, "PlayerInfo")
    assert len(matches) == 1
    assert matches[0].kind == "type"
    assert matches[0].end_line > matches[0].line


def test_function_with_modifiers(symbols):
    matches = _by_name(symbols, "CalculateDamage")
    assert len(matches) == 1
    fn = matches[0]
    assert fn.kind == "function"
    assert "simulated" in fn.signature
    assert "int" in fn.signature
    assert fn.param_count == 2  # BaseDamage, Modifier


def test_event_extracted(symbols):
    matches = _by_name(symbols, "PostBeginPlay")
    assert len(matches) == 1
    assert matches[0].kind == "function"
    assert matches[0].signature.lower().startswith("event")


def test_auto_state_extracted(symbols):
    matches = _by_name(symbols, "Idle")
    assert len(matches) == 1
    assert matches[0].kind == "class"
    # auto state with a method inside: end_line must cover method
    assert matches[0].end_line > matches[0].line + 2


def test_state_with_extends(symbols):
    matches = _by_name(symbols, "Attacking")
    assert len(matches) == 1
    assert matches[0].kind == "class"
    assert "extends Idle" in matches[0].signature


def test_method_inside_state_extracted(symbols):
    # Tick lives inside the Idle state. It should surface as a function.
    matches = _by_name(symbols, "Tick")
    assert len(matches) == 1
    assert matches[0].kind == "function"
    assert matches[0].param_count == 1


def test_defaultproperties_ignored(symbols):
    # Keys inside defaultproperties must NOT produce symbols.
    # If they did, "Health" or "Name" would show up as a symbol.
    assert _by_name(symbols, "Health") == []
    assert _by_name(symbols, "Name") == []
    # And the fake `class=Fake` string inside defaults must not extract.
    assert _by_name(symbols, "Fake") == []


def test_comment_contents_ignored():
    src = """\
// class CommentedOut extends Foo;
/* function FakeFn() {} */
class RealClass extends Object;
"""
    syms = parse_file(src, "R.uc", "unrealscript")
    names = {s.name for s in syms}
    assert "RealClass" in names
    assert "CommentedOut" not in names
    assert "FakeFn" not in names


def test_symbol_ids_unique(symbols):
    ids = [s.id for s in symbols]
    assert len(ids) == len(set(ids)), "duplicate symbol IDs"


def test_symbols_sorted_by_line(symbols):
    lines = [s.line for s in symbols]
    assert lines == sorted(lines)


def test_parent_link_for_method_in_state(symbols):
    tick = _by_name(symbols, "Tick")[0]
    idle = _by_name(symbols, "Idle")[0]
    assert tick.parent == idle.id


def test_empty_file_returns_empty():
    assert parse_file("", "Empty.uc", "unrealscript") == []


def test_class_only_file():
    syms = parse_file("class Foo extends Bar;\n", "Foo.uc", "unrealscript")
    assert len(syms) == 1
    assert syms[0].name == "Foo"
    assert syms[0].kind == "class"


def test_docstring_preceding_comment(symbols):
    # CalculateDamage has a /** Compute damage from a base amount. */ above.
    fn = _by_name(symbols, "CalculateDamage")[0]
    assert "Compute damage" in fn.docstring


# ---------------------------------------------------------------------------
# Fixture-backed integration test. Exercises the same parser against a real
# .uc file on disk rather than an inline string, so it catches regressions
# in extension dispatch (LANGUAGE_EXTENSIONS[".uc"]) as well as parser logic.
# ---------------------------------------------------------------------------


def test_fixture_file_parses():
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"
    src_bytes = FIXTURE.read_bytes()
    syms = parse_file(
        src_bytes.decode("utf-8"), "Weapon.uc", "unrealscript",
        source_bytes=src_bytes,
    )
    by_name = {s.name: s for s in syms}
    # Every construct in the fixture must surface as a symbol.
    for expected in (
        "Weapon", "MAX_AMMO", "DEFAULT_NAME", "EFireMode",
        "FireInfo", "GetDamage", "FindClosest", "PostBeginPlay",
        "Idle", "Firing", "Tick", "BeginState",
    ):
        assert expected in by_name, f"{expected!r} not extracted from fixture"
    # The weapon class header must include its extends clause.
    assert "extends Actor" in by_name["Weapon"].signature
    # Tick is nested inside Idle state.
    assert by_name["Tick"].parent == by_name["Idle"].id
    # defaultproperties must not leak symbols even for `class=...` text.
    assert "Trap" not in by_name
    assert "Pistol" not in by_name  # appears only inside a name literal


# ---------------------------------------------------------------------------
# v2: var declarations
# ---------------------------------------------------------------------------

def test_var_simple():
    src = "class Foo extends Object;\nvar int Health;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    matches = [s for s in syms if s.name == "Health"]
    assert len(matches) == 1
    assert matches[0].kind == "constant"


def test_var_with_category():
    src = "class Foo extends Object;\nvar(Weapon) int Health;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    m = next((s for s in syms if s.name == "Health"), None)
    assert m is not None, "Health var not extracted"
    assert "(Weapon)" in m.signature


def test_var_with_modifiers():
    src = "class Foo extends Object;\nvar config transient int Health;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    m = next((s for s in syms if s.name == "Health"), None)
    assert m is not None, "Health var not extracted"
    assert "config" in m.signature
    assert "transient" in m.signature


def test_var_multiname():
    src = "class Foo extends Object;\nvar int Health, Ammo;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    names = {s.name for s in syms}
    assert "Health" in names, f"Health missing from {names}"
    assert "Ammo" in names, f"Ammo missing from {names}"


def test_var_array_subscript():
    src = "class Foo extends Object;\nvar int Slots[16];\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    m = next((s for s in syms if s.name == "Slots"), None)
    assert m is not None, "Slots var not extracted"


def test_var_generic_type():
    src = "class Foo extends Object;\nvar array<Vector> Points;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    m = next((s for s in syms if s.name == "Points"), None)
    assert m is not None, "Points var not extracted"
    assert "array<Vector>" in m.signature


def test_local_var_not_extracted():
    src = (
        "class Foo extends Object;\n"
        "function void Bar() {\n"
        "    local int X;\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    assert all(s.name != "X" for s in syms), "local var X should not be extracted"


def test_var_parent_is_class():
    src = "class Foo extends Object;\nvar int Health;\n"
    syms = parse_file(src, "Foo.uc", "unrealscript")
    cls = next(s for s in syms if s.kind == "class")
    var = next((s for s in syms if s.name == "Health"), None)
    assert var is not None, "Health var not extracted"
    assert var.parent == cls.id


def test_var_inside_state():
    src = (
        "class Foo extends Object;\n"
        "auto state MyState {\n"
        "    var int StateHealth;\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    cls = next(s for s in syms if s.kind == "class")
    var = next((s for s in syms if s.name == "StateHealth"), None)
    assert var is not None, "StateHealth var not extracted"
    assert var.parent == cls.id, "var inside state must be class-scoped"


# ---------------------------------------------------------------------------
# v2: call graph
# ---------------------------------------------------------------------------

def test_call_simple():
    src = (
        "class Foo extends Object;\n"
        "function void Bar() {\n"
        "    Baz();\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    bar = next((s for s in syms if s.name == "Bar"), None)
    assert bar is not None, "Bar not extracted"
    assert "Baz" in bar.call_references


def test_call_super():
    src = (
        "class Foo extends Object;\n"
        "event PostBeginPlay() {\n"
        "    super.PostBeginPlay();\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    fn = next((s for s in syms if s.name == "PostBeginPlay"), None)
    assert fn is not None, "PostBeginPlay not extracted"
    assert "PostBeginPlay" in fn.call_references


def test_call_outer():
    src = (
        "class Foo extends Object;\n"
        "function void Bar() {\n"
        "    Outer.Notify();\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    bar = next((s for s in syms if s.name == "Bar"), None)
    assert bar is not None, "Bar not extracted"
    assert "Notify" in bar.call_references


def test_call_keyword_excluded():
    src = (
        "class Foo extends Object;\n"
        "function void Bar() {\n"
        "    if (x) { while (y) { foreach (z, it) {} } }\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    bar = next((s for s in syms if s.name == "Bar"), None)
    assert bar is not None, "Bar not extracted"
    for kw in ("if", "while", "foreach"):
        assert kw not in bar.call_references, f"keyword '{kw}' should be excluded"


def test_call_deduplicated():
    src = (
        "class Foo extends Object;\n"
        "function void Bar() {\n"
        "    Baz();\n"
        "    Baz();\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    bar = next((s for s in syms if s.name == "Bar"), None)
    assert bar is not None, "Bar not extracted"
    assert bar.call_references.count("Baz") == 1, "duplicate call reference"


def test_call_attributed_to_enclosing_function():
    src = (
        "class Foo extends Object;\n"
        "auto state MyState {\n"
        "    function void StateMethod() {\n"
        "        DoThing();\n"
        "    }\n"
        "}\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    method = next((s for s in syms if s.name == "StateMethod"), None)
    state = next((s for s in syms if s.name == "MyState"), None)
    assert method is not None, "StateMethod not extracted"
    assert state is not None, "MyState not extracted"
    assert "DoThing" in method.call_references
    assert "DoThing" not in state.call_references


def test_empty_body_no_calls():
    src = (
        "class Foo extends Object;\n"
        "native function void NativeFn();\n"
    )
    syms = parse_file(src, "Foo.uc", "unrealscript")
    fn = next((s for s in syms if s.name == "NativeFn"), None)
    assert fn is not None, "NativeFn not extracted"
    assert fn.call_references == []


# ---------------------------------------------------------------------------
# v3: import / dependency graph (#8)
# ---------------------------------------------------------------------------

def _specifiers(edges):
    return {e["specifier"] for e in edges}


def test_import_extends():
    src = "class Weapon extends Actor\n    config(Game);\n"
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    assert "Actor" in _specifiers(edges)


def test_import_within():
    src = "class MyActor extends Actor within Pawn;\n"
    edges = extract_imports(src, "MyActor.uc", "unrealscript")
    specs = _specifiers(edges)
    assert "Actor" in specs
    assert "Pawn" in specs


def test_import_dependson_single():
    src = "class Foo extends Object dependson(Bar);\n"
    edges = extract_imports(src, "Foo.uc", "unrealscript")
    assert "Bar" in _specifiers(edges)


def test_import_dependson_multiple():
    src = "class Foo extends Object dependson(Bar, Baz);\n"
    edges = extract_imports(src, "Foo.uc", "unrealscript")
    specs = _specifiers(edges)
    assert "Bar" in specs
    assert "Baz" in specs


def test_import_class_literal_bare():
    src = (
        "class Weapon extends Actor;\n"
        "function void Fire() { Spawn(class'Pistol'); }\n"
    )
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    assert "Pistol" in _specifiers(edges)


def test_import_class_literal_qualified():
    src = (
        "class Weapon extends Actor;\n"
        "function void Fire() { Spawn(class'Game.Pistol'); }\n"
    )
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    assert "Pistol" in _specifiers(edges)


def test_import_deduplication():
    src = (
        "class Weapon extends Actor;\n"
        "function void A() { Spawn(class'Actor'); }\n"
    )
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    actor_edges = [e for e in edges if e["specifier"] == "Actor"]
    assert len(actor_edges) == 1, "Actor should appear exactly once"


def test_import_no_extends_returns_empty():
    src = "// no class declaration\nfunction void Foo() {}\n"
    edges = extract_imports(src, "Foo.uc", "unrealscript")
    # No class header -> no extends/within/dependson; class literals still scanned.
    assert all("Foo" not in e["specifier"] for e in edges)


def test_import_case_insensitive_dedup():
    src = (
        "class Weapon extends Actor;\n"
        "function void A() { Spawn(class'actor'); }\n"
    )
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    actor_edges = [e for e in edges if e["specifier"].lower() == "actor"]
    assert len(actor_edges) == 1, "actor/Actor should be deduped case-insensitively"


def test_import_fixture_file():
    src = FIXTURE.read_text(encoding="utf-8")
    edges = extract_imports(src, "Weapon.uc", "unrealscript")
    specs = _specifiers(edges)
    # class Weapon extends Actor within Pawn
    assert "Actor" in specs, f"Actor not in {specs}"
    assert "Pawn" in specs, f"Pawn not in {specs}"
    # class'Game.Pistol' inside defaultproperties — note: masked text may skip it
    # but the literal scanner runs on raw content, so it must still appear.
    assert "Pistol" in specs, f"Pistol not in {specs}"


# ---------------------------------------------------------------------------
# resolve_specifier: .uc stem resolution
# ---------------------------------------------------------------------------

def test_resolve_specifier_uc_stem():
    files = {"Classes/Actor.uc", "Classes/Pawn.uc", "Classes/Weapon.uc"}
    result = resolve_specifier("Actor", "Classes/Weapon.uc", files)
    assert result == "Classes/Actor.uc"


def test_resolve_specifier_uc_case_insensitive():
    files = {"Classes/Actor.uc"}
    result = resolve_specifier("actor", "Classes/Weapon.uc", files)
    assert result == "Classes/Actor.uc"


def test_resolve_specifier_uc_missing():
    files = {"Classes/Actor.uc"}
    result = resolve_specifier("NonExistent", "Classes/Weapon.uc", files)
    assert result is None
