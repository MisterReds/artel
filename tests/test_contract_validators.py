"""The validators of artel.contracts._base called as a contract author calls them: defaults, messages, field paths."""

from __future__ import annotations

import pytest

from artel.contracts import _base as b
from artel.contracts import core, schemas
from artel.contracts.agent_event import AgentEvent
from artel.contracts.events import Event


def problem(call, *args, **kw):
    with pytest.raises(b.ContractError) as info:
        call(*args, **kw)
    return info.value.field, info.value.problem


# Every validator names the offending field; without a path argument that is just the key.
@pytest.mark.parametrize(
    "call, args",
    [
        (b.string, ()),
        (b.integer, ()),
        (b.number, ()),
        (b.boolean, ()),
        (b.enum, (("a",),)),
        (b.strings, ()),
        (b.nested, (schemas.Tokens,)),
        (b.nested_list, (schemas.Tokens,)),
        (b.mapping, ()),
        (b.mapping_list, ()),
        (b.string_groups, ()),
    ],
)
def test_missing_field_is_named_by_its_key_without_a_path(call, args):
    assert problem(call, {}, "k", *args) == ("k", "required field is missing")


def test_a_path_prefixes_the_field_name():
    assert problem(b.string, {}, "k", "outer.") == ("outer.k", "required field is missing")
    assert problem(b.string, {"k": 1}, "k", "outer.") == ("outer.k", "expected a string")


def test_string_defaults_are_not_nullable_and_may_be_empty():
    assert problem(b.string, {"k": None}, "k") == ("k", "expected a string")
    assert b.string({"k": None}, "k", nullable=True) is None
    assert b.string({"k": ""}, "k") == ""
    assert problem(b.string, {"k": ""}, "k", nonempty=True) == ("k", "must not be empty")
    assert b.string({"k": "x"}, "k", nonempty=True) == "x"


def test_integer_defaults_minimum_zero_and_rejects_bool():
    assert b.integer({"k": 0}, "k") == 0
    assert problem(b.integer, {"k": -1}, "k") == ("k", "must be >= 0")
    assert problem(b.integer, {"k": True}, "k") == ("k", "expected an integer")
    assert problem(b.integer, {"k": 1.5}, "k") == ("k", "expected an integer")
    assert b.integer({"k": 1}, "k", minimum=1) == 1
    assert problem(b.integer, {"k": 0}, "k", minimum=1) == ("k", "must be >= 1")


def test_number_defaults_minimum_zero_no_maximum_and_finite():
    assert b.number({"k": 0}, "k") == 0
    assert b.number({"k": 1e12}, "k") == 1e12
    assert problem(b.number, {"k": -0.5}, "k") == ("k", "must be >= 0.0")
    assert problem(b.number, {"k": float("inf")}, "k") == ("k", "must be a finite number")
    assert problem(b.number, {"k": "1"}, "k") == ("k", "expected a number")
    assert problem(b.number, {"k": 3}, "k", maximum=2) == ("k", "must be >= 0.0 and <= 2")
    assert b.number({"k": 2}, "k", maximum=2) == 2


def test_boolean_and_enum():
    assert b.boolean({"k": False}, "k") is False
    assert problem(b.boolean, {"k": 0}, "k") == ("k", "expected a boolean")
    assert b.enum({"k": "a"}, "k", ("a", "b")) == "a"
    assert problem(b.enum, {"k": "z"}, "k", ("b", "a")) == ("k", "must be one of ['a', 'b'], got 'z'")
    assert problem(b.enum, {"k": None}, "k", ("a",)) == ("k", "must be one of ['a'], got None")
    assert b.enum({"k": None}, "k", ("a",), nullable=True) is None


def test_strings_checks_items_and_the_optional_maximum():
    assert b.strings({"k": ["a", "b"]}, "k") == ("a", "b")
    assert problem(b.strings, {"k": "a"}, "k") == ("k", "expected a list")
    assert problem(b.strings, {"k": ["a", 2]}, "k") == ("k[1]", "expected a string")
    assert b.strings({"k": ["a", "b"]}, "k", maximum=2) == ("a", "b")
    assert problem(b.strings, {"k": ["a", "b", "c"]}, "k", maximum=2) == ("k", "at most 2 items allowed, got 3")


def test_nested_object_paths_and_nullable():
    assert b.nested({"k": {"in": 1, "out": 2}}, "k", schemas.Tokens) == schemas.Tokens(1, 2)
    assert problem(b.nested, {"k": {"in": 1}}, "k", schemas.Tokens) == ("k.out", "required field is missing")
    assert problem(b.nested, {"k": None}, "k", schemas.Tokens) == ("k.", "expected an object")
    assert b.nested({"k": None}, "k", schemas.Tokens, nullable=True) is None
    assert problem(b.nested, {"k": {"in": 1}}, "k", schemas.Tokens, "p.") == ("p.k.out", "required field is missing")


def test_nested_list_names_the_failing_element():
    got = b.nested_list({"k": [{"in": 1, "out": 2}]}, "k", schemas.Tokens)
    assert got == (schemas.Tokens(1, 2),)
    assert problem(b.nested_list, {"k": [{"in": 1, "out": 2}, {"in": 1}]}, "k", schemas.Tokens) == (
        "k[1].out",
        "required field is missing",
    )
    assert problem(b.nested_list, {"k": "x"}, "k", schemas.Tokens) == ("k", "expected a list")


def test_mapping_is_frozen_and_nullable_only_on_request():
    got = b.mapping({"k": {"a": [1, 2]}}, "k")
    assert got["a"] == (1, 2)
    assert problem(b.mapping, {"k": None}, "k") == ("k", "expected an object")
    assert b.mapping({"k": None}, "k", nullable=True) is None
    assert problem(b.mapping, {"k": []}, "k") == ("k", "expected an object")


def test_mapping_list_checks_every_item_field():
    types = {"id": (str,), "n": (int, float)}
    good = [{"id": "a", "n": 1}, {"id": "b", "n": 2.5}]
    assert [dict(x) for x in b.mapping_list({"k": good}, "k", item_types=types)] == good
    assert b.mapping_list({"k": [{"anything": 1}]}, "k")[0]["anything"] == 1  # no item_types: any object
    assert problem(b.mapping_list, {"k": "x"}, "k") == ("k", "expected a list")
    assert problem(b.mapping_list, {"k": [1]}, "k") == ("k[0]", "expected an object")
    assert problem(b.mapping_list, {"k": [{"id": "a"}]}, "k", item_types=types) == (
        "k[0].n",
        "required field is missing",
    )
    assert problem(b.mapping_list, {"k": [{"id": 1, "n": 1}]}, "k", item_types=types) == (
        "k[0].id",
        "expected str",
    )
    assert problem(b.mapping_list, {"k": [{"id": "a", "n": "x"}]}, "k", "p.", item_types=types) == (
        "p.k[0].n",
        "expected int or float",
    )
    # a bool is not an int unless bool is accepted explicitly
    assert problem(b.mapping_list, {"k": [{"n": True}]}, "k", item_types={"n": (int,)}) == ("k[0].n", "expected int")
    assert b.mapping_list({"k": [{"n": True}]}, "k", item_types={"n": (int, bool)})[0]["n"] is True
    assert problem(b.mapping_list, {"k": [{"m": 1}]}, "k", item_types={"m": (dict,)}) == ("k[0].m", "expected dict")
    assert problem(b.mapping_list, {"k": [{"m": 1}]}, "k", item_types={"m": (b.Mapping,)}) == (
        "k[0].m",
        "expected object",
    )


def test_string_groups_checks_each_group():
    assert b.string_groups({"k": [["a"], ["b", "c"]]}, "k") == (("a",), ("b", "c"))
    assert problem(b.string_groups, {"k": "x"}, "k") == ("k", "expected a list")
    assert problem(b.string_groups, {"k": [["a"], [1]]}, "k") == ("k[1].g[0]", "expected a string")


# from_dict of every contract: a non-object payload is reported at the root, whatever the class
CLASSES = [
    schemas.IntentBudget,
    schemas.Intent,
    schemas.ManagerBudget,
    schemas.ManagerInput,
    schemas.TaskContract,
    schemas.RangeResearchTask,
    schemas.Altitude,
    schemas.Depth,
    schemas.RangeEntry,
    schemas.AmplifiedTaskContract,
    schemas.ArtifactRef,
    schemas.ExecutorResult,
    schemas.EscalationRequest,
    schemas.Tokens,
    schemas.DecisionLogEntry,
    schemas.CapabilityProfile,
    schemas.RewriteModule,
    schemas.RewriteBudget,
    schemas.RewriteProposal,
    schemas.RewriteTaskContract,
    core.Usage,
    core.NormRequest,
    core.NormResponse,
    core.ToolResult,
    core.ModelCard,
    core.ErrorClass,
    Event,
    AgentEvent,
]


@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: c.__name__)
def test_from_dict_reports_a_non_object_at_the_root(cls):
    assert problem(cls.from_dict, 42) == ("<root>", "expected an object")
    assert problem(cls.from_dict, 42, "outer") == ("outer.", "expected an object")


def test_plain_serialisation_renames_the_in_field():
    assert schemas.Tokens(3, 4).to_dict() == {"in": 3, "out": 4}


def test_check_path_adds_one_trailing_dot_only():
    assert b.check_path("") == ""
    assert b.check_path("a") == "a."
    assert b.check_path("a.") == "a."


def test_to_plain_converts_contracts_tuples_and_mappings():
    assert b.to_plain({"a": (schemas.Tokens(1, 2), [3])}) == {"a": [{"in": 1, "out": 2}, [3]]}
