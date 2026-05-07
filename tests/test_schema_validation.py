"""Unit tests for app/schema.py — pure, no neo4j required."""
from __future__ import annotations

import pytest

from app import schema
from app.schema import SchemaError


def test_label_key_is_sorted_and_dotted():
    assert schema.label_key(["input", "note"]) == "input.note"
    assert schema.label_key(["note", "input"]) == "input.note"
    assert schema.label_key(["agent"]) == "agent"


def test_category_input_wins_over_other_labels():
    assert schema.category(["input", "note"]) == "input"
    assert schema.category(["agent"]) == "agent"
    assert schema.category(["edge_record"]) == "edge_record"


def test_validate_node_create_accepts_known_input_note():
    schema.validate_node_create(
        ["input", "note"],
        {
            "note_id": "n1",
            "provider_name": "weave",
            "external_chat_id": "chat",
            "external_id": "ext",
            "content": "hello",
            "created_at": "2026-05-07T00:00:00Z",
        },
    )


def test_validate_node_create_rejects_unknown_label():
    with pytest.raises(SchemaError):
        schema.validate_node_create(["Person"], {})


def test_validate_node_create_rejects_missing_required():
    with pytest.raises(SchemaError) as exc:
        schema.validate_node_create(["input", "note"], {"note_id": "n1"})
    assert "missing required" in str(exc.value)


def test_validate_node_create_rejects_unknown_property_when_closed():
    with pytest.raises(SchemaError) as exc:
        schema.validate_node_create(
            ["input", "note"],
            {
                "note_id": "n1",
                "provider_name": "weave",
                "external_chat_id": "chat",
                "external_id": "ext",
                "content": "hello",
                "created_at": "2026-05-07T00:00:00Z",
                "frobnicator": "yes",
            },
        )
    assert "unknown properties" in str(exc.value)


def test_validate_node_patch_rejects_immutable():
    with pytest.raises(SchemaError):
        schema.validate_node_patch(
            ["input", "note"],
            set_props={"note_id": "different"},
            unset_props=[],
        )


def test_validate_edge_create_accepts_input_extracted_from_input():
    schema.validate_edge_create("extracted_from", ["input", "note"], ["input", "note"])


def test_validate_edge_create_rejects_bad_endpoint():
    with pytest.raises(SchemaError):
        # `:author` requires (input)->(identity), not (input)->(agent).
        schema.validate_edge_create("author", ["input", "note"], ["agent"])


def test_validate_edge_create_rejects_unknown_type():
    with pytest.raises(SchemaError):
        schema.validate_edge_create("not_a_real_edge_type", ["input"], ["input"])


def test_label_def_lookup_works_for_simple_labels():
    ld = schema.SCHEMA.label_def(["agent"])
    assert ld.key == "agent"
    assert "auth_user_id" in ld.required


def test_open_properties_label_accepts_anything():
    schema.validate_node_create(
        ["agent"],
        {
            "auth_user_id": 1,
            "name": "n",
            "kind": "human",
            "created_at": "2026-05-07T00:00:00Z",
            "twitter_handle": "@whatever",  # not in required/optional
        },
    )
