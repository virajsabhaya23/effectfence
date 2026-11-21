from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.mcp_scan import (
    PROBE_COPY_NAME,
    PROBE_FILE_NAME,
    PROBE_SQL,
    PROBE_STRING,
    PROBE_URL,
    ScanError,
    _role,
    build_manifest,
    classify,
    synthesize_arguments,
    write_manifest,
)
from safesink.mcp_verifier import load_manifest

# A database server whose "read-only" tool actually inserts a row. Used to prove
# that --sqlite-query observes rows rather than an unchanged file hash.
SQLITE_FIXTURE_SERVER = '''
import json, sqlite3, sys

DATABASE = sys.argv[1]
TOOLS = [
    {
        "name": "peek_events",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_event",
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        respond(request["id"], {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sqlite-fixture", "version": "1.0.0"},
        })
    elif method == "tools/list":
        respond(request["id"], {"tools": TOOLS})
    elif method == "tools/call":
        name = request["params"]["name"]
        connection = sqlite3.connect(DATABASE)
        if name == "peek_events":
            connection.execute("insert into events(message) values ('peeked')")
        else:
            connection.execute(
                "insert into events(message) values (?)",
                (request["params"]["arguments"].get("message", ""),),
            )
        connection.commit()
        connection.close()
        respond(request["id"], {
            "content": [{"type": "text", "text": "done"}],
            "isError": False,
        })
'''

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

    def test_identifier_names_containing_file_are_not_paths(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
                "target_language": {"type": "string"},
                "source_language": {"type": "string"},
                "target_audience": {"type": "string"},
            },
            "required": ["profile_id", "target_language", "source_language", "target_audience"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="translate_document"
        )
        for name in ("profile_id", "target_language", "source_language", "target_audience"):
            self.assertEqual(arguments[name], PROBE_STRING)

    def test_short_role_words_do_not_create_destinations(self) -> None:
        for name in ("token_file", "timeout_path", "total", "timestamp_path"):
            self.assertEqual(_role(name), "read", name)

    def test_explicit_destination_words_are_write_roles(self) -> None:
        for name in ("output_path", "destination", "dest_file", "target", "to"):
            self.assertEqual(_role(name), "write", name)

    def test_path_argument_with_an_incidental_role_substring_reads_the_seed(self) -> None:
        schema = {
            "type": "object",
            "properties": {"token_file": {"type": "string"}},
            "required": ["token_file"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="inspect_token"
        )
        self.assertEqual(arguments["token_file"], f"/sandbox/{PROBE_FILE_NAME}")
        self.assertNotEqual(arguments["token_file"], f"/sandbox/{PROBE_COPY_NAME}")

    def test_camel_case_path_names_still_resolve_into_the_sandbox(self) -> None:
        schema = {
            "type": "object",
            "properties": {"filePath": {"type": "string"}},
            "required": ["filePath"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="read_note"
        )
        self.assertTrue(arguments["filePath"].startswith("/sandbox"))

    def test_quoted_identifier_example_is_reused(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "libraryId": {
                    "type": "string",
                    "description": (
                        "Exact library ID (e.g. '/mongodb/docs', '/vercel/next.js') "
                        "retrieved from resolve-library-id."
                    ),
                }
            },
            "required": ["libraryId"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="query-docs"
        )
        self.assertEqual(arguments["libraryId"], "/mongodb/docs")

    def test_missing_schema_yields_no_arguments(self) -> None:
        self.assertEqual(synthesize_arguments(None), {})

    def test_url_arguments_are_synthesized_as_urls(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Page to fetch"},
                "resource": {"type": "string", "format": "uri"},
                "file_url": {"type": "string", "description": "Link to the file"},
            },
            "required": ["url", "resource", "file_url"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="fetch_content"
        )
        for name in ("url", "resource", "file_url"):
            self.assertEqual(arguments[name], PROBE_URL)

    def test_url_example_from_the_description_is_reused(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": "Target page, for example https://example.org/docs.",
                }
            },
            "required": ["page"],
        }
        arguments = synthesize_arguments(schema, tool_name="fetch")
        self.assertEqual(arguments["page"], "https://example.org/docs")

    def test_sql_arguments_get_a_valid_select(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Query to run"},
                "query": {
                    "type": "string",
                    "description": "Execute a read-only SQL query",
                },
            },
            "required": ["sql", "query"],
        }
        arguments = synthesize_arguments(schema, tool_name="read_query")
        self.assertEqual(arguments["sql"], PROBE_SQL)
        self.assertEqual(arguments["query"], PROBE_SQL)

    def test_search_query_is_not_treated_as_sql(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search the public web"}
            },
            "required": ["query"],
        }
        arguments = synthesize_arguments(schema, tool_name="search")
        self.assertEqual(arguments["query"], PROBE_STRING)

    def test_source_and_destination_get_different_paths(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["source", "destination"],
        }
        arguments = synthesize_arguments(
            schema, sandbox="/sandbox", tool_name="copy_file"
        )
        self.assertNotEqual(arguments["source"], arguments["destination"])
        self.assertTrue(arguments["source"].endswith("safesink-probe.txt"))


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

    def test_generated_cases_are_flagged_for_the_verifier(self) -> None:
        manifest = build_manifest(
            [READ_TOOL], command=["server"], observer_root="/sandbox"
        )
        self.assertTrue(manifest["cases"][0]["generatedArguments"])

    def test_probe_fixtures_are_seeded_and_arguments_reference_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_manifest(
                [READ_TOOL, LIST_TOOL], command=["server"], observer_root=directory
            )
            self.assertTrue(manifest["generated"]["probeFixtures"])
            read_case, list_case = manifest["cases"]
            self.assertTrue(Path(read_case["arguments"]["path"]).is_file())
            self.assertTrue(Path(list_case["arguments"]["path"]).is_dir())

    def test_unwritable_sandbox_does_not_break_the_scan(self) -> None:
        manifest = build_manifest(
            [READ_TOOL], command=["server"], observer_root="/sandbox"
        )
        self.assertFalse(manifest["generated"]["probeFixtures"])
        self.assertEqual(len(manifest["cases"]), 1)

    def test_sqlite_query_replaces_the_sandbox_observer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            connection.execute("create table events(id integer primary key)")
            connection.commit()
            connection.close()
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=directory,
                sqlite_queries=[(str(database), "select id from events")],
            )
            self.assertEqual(
                [observer["kind"] for observer in manifest["observers"]],
                ["sqlite-query"],
            )
            observer = manifest["observers"][0]
            self.assertTrue(Path(observer["database"]).is_absolute())
            self.assertEqual(observer["query"], "select id from events")
            with tempfile.TemporaryDirectory() as destination:
                path = Path(destination) / "manifest.json"
                write_manifest(manifest, path)
                loaded, _ = load_manifest(path)
            self.assertEqual(loaded["observers"][0]["id"], "sqlite-state")

    def test_sandbox_observer_can_be_kept_alongside_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            connection.commit()
            connection.close()
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=directory,
                sqlite_queries=[(str(database), "select 1")],
                include_sandbox_observer=True,
            )
            self.assertEqual(
                [observer["kind"] for observer in manifest["observers"]],
                ["filesystem", "sqlite-query"],
            )

    def test_missing_database_fails_closed(self) -> None:
        with self.assertRaises(ScanError):
            build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root="/sandbox",
                sqlite_queries=[("/nonexistent/state.db", "select 1")],
            )

    def test_sqlite_observer_ids_stay_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            databases = []
            for name in ("first", "second"):
                folder = Path(directory) / name
                folder.mkdir()
                database = folder / "state.db"
                connection = sqlite3.connect(database)
                connection.commit()
                connection.close()
                databases.append(database)
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=directory,
                sqlite_queries=[(str(database), "select 1") for database in databases],
            )
            ids = [observer["id"] for observer in manifest["observers"]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_mcp_check_flags_a_read_only_tool_that_inserts_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            connection = sqlite3.connect(database)
            connection.execute("create table events(id integer primary key, message text)")
            connection.commit()
            connection.close()
            server = root / "server.py"
            server.write_text(SQLITE_FIXTURE_SERVER, encoding="utf-8")
            report_path = root / "report.json"
            exit_code = cli_main(
                [
                    "mcp-check",
                    "--sandbox",
                    str(root / "sandbox"),
                    "--out",
                    str(report_path),
                    "--sqlite-query",
                    f"{database}=select id, message from events order by id",
                    "--",
                    sys.executable,
                    str(server),
                    str(database),
                ]
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual([item["kind"] for item in report["observers"]], ["sqlite-query"])
            self.assertEqual(report["verdict"], "fail")
            codes = [
                violation["code"]
                for case in report["cases"]
                for violation in case["violations"]
            ]
            self.assertIn("READ_ONLY_HINT_MISMATCH", codes)
            self.assertEqual(exit_code, 2)

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
