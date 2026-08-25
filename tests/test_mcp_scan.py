from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from effectfence.mcp_scan import (
    ScanError,
    build_manifest,
    classify,
    synthesize_arguments,
    write_manifest,
)
from effectfence.mcp_verifier import load_manifest

READ_TOOL = {
    "name": "read_file",
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to read"}},
        "required": ["path"],
    },
}
LIST_TOOL = {
    "name": "list_directory",
    "annotations": {"readOnlyHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory to list"}},
        "required": ["path"],
    },
}
DESTRUCTIVE_TOOL = {
    "name": "delete_file",
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}


class ClassifyTests(unittest.TestCase):
    def test_read_only_wins_over_absent_hints(self) -> None:
        self.assertEqual(classify(READ_TOOL), "read-only")

    def test_destructive_is_detected(self) -> None:
        self.assertEqual(classify(DESTRUCTIVE_TOOL), "destructive")

    def test_unannotated_tools_are_treated_as_mutating(self) -> None:
        self.assertEqual(classify({"name": "x"}), "mutating")


class SynthesizeArgumentTests(unittest.TestCase):
    def test_only_required_properties_are_generated(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
        self.assertEqual(list(synthesize_arguments(schema)), ["a"])

    def test_enum_const_and_default_are_preferred(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "slow"]},
                "kind": {"const": "fixed"},
                "limit": {"type": "integer", "default": 7},
            },
        }
        arguments = synthesize_arguments(schema)
        self.assertEqual(arguments["mode"], "fast")
        self.assertEqual(arguments["kind"], "fixed")
        self.assertEqual(arguments["limit"], 7)

    def test_numeric_minimums_are_respected(self) -> None:
        schema = {
            "type": "object",
            "properties": {"size": {"type": "integer", "minimum": 5}},
        }
        self.assertEqual(synthesize_arguments(schema)["size"], 5)

    def test_arrays_produce_at_least_one_typed_item(self) -> None:
        schema = {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
        }
        value = synthesize_arguments(schema, sandbox="/sandbox")["paths"]
        self.assertEqual(len(value), 1)
        self.assertTrue(value[0].startswith("/sandbox"))

    def test_nullable_union_types_use_the_concrete_type(self) -> None:
        schema = {
            "type": "object",
            "properties": {"count": {"type": ["integer", "null"]}},
            "required": ["count"],
        }
        self.assertEqual(synthesize_arguments(schema)["count"], 1)

    def test_nested_objects_recurse(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"deep": {"type": "boolean"}},
                    "required": ["deep"],
                }
            },
            "required": ["options"],
        }
        self.assertEqual(synthesize_arguments(schema)["options"], {"deep": False})

    def test_file_paths_and_directory_paths_are_distinguished(self) -> None:
        file_argument = synthesize_arguments(
            READ_TOOL["inputSchema"], sandbox="/sandbox", tool_name="read_file"
        )
        directory_argument = synthesize_arguments(
            LIST_TOOL["inputSchema"], sandbox="/sandbox", tool_name="list_directory"
        )
        self.assertTrue(file_argument["path"].endswith(".txt"))
        self.assertEqual(directory_argument["path"], "/sandbox")

    def test_missing_schema_yields_no_arguments(self) -> None:
        self.assertEqual(synthesize_arguments(None), {})


class BuildManifestTests(unittest.TestCase):
    def test_destructive_tools_are_skipped_by_default(self) -> None:
        manifest = build_manifest(
            [READ_TOOL, DESTRUCTIVE_TOOL],
            command=["server"],
            observer_root="/sandbox",
        )
        self.assertEqual([case["tool"] for case in manifest["cases"]], ["read_file"])
        self.assertEqual(manifest["generated"]["skipped"][0]["tool"], "delete_file")

    def test_destructive_tools_can_be_opted_in(self) -> None:
        manifest = build_manifest(
            [READ_TOOL, DESTRUCTIVE_TOOL],
            command=["server"],
            observer_root="/sandbox",
            include_destructive=True,
        )
        self.assertEqual(len(manifest["cases"]), 2)

    def test_declared_annotations_become_the_case_contract(self) -> None:
        manifest = build_manifest(
            [READ_TOOL], command=["server"], observer_root="/sandbox"
        )
        self.assertEqual(
            manifest["cases"][0]["contract"],
            {"readOnlyHint": True, "openWorldHint": False},
        )

    def test_duplicate_tool_names_get_unique_case_ids(self) -> None:
        manifest = build_manifest(
            [READ_TOOL, dict(READ_TOOL)],
            command=["server"],
            observer_root="/sandbox",
        )
        ids = [case["id"] for case in manifest["cases"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_testable_tools_fails_closed(self) -> None:
        with self.assertRaises(ScanError):
            build_manifest([DESTRUCTIVE_TOOL], command=["s"], observer_root="/sandbox")

    def test_generated_manifest_passes_verifier_validation(self) -> None:
        manifest = build_manifest(
            [READ_TOOL, LIST_TOOL],
            command=["server", "--flag"],
            observer_root="/sandbox",
            minimum_tool_coverage=0.5,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "manifest.json"
            write_manifest(manifest, destination)
            loaded, _ = load_manifest(destination)
        self.assertEqual(loaded["schemaVersion"], "effectfence.mcp.v1")
        self.assertEqual(len(loaded["cases"]), 2)

    def test_manifest_is_written_deterministically(self) -> None:
        manifest = build_manifest(
            [READ_TOOL], command=["server"], observer_root="/sandbox"
        )
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a.json"
            second = Path(directory) / "nested" / "b.json"
            write_manifest(manifest, first)
            write_manifest(manifest, second)
            self.assertEqual(first.read_text(), second.read_text())
            self.assertEqual(json.loads(first.read_text())["server"]["command"], ["server"])


if __name__ == "__main__":
    unittest.main()
