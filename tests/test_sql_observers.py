from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

from safesink.mcp_scan import build_manifest
from safesink.mcp_verifier import ManifestError, load_manifest
from safesink.observers import (
    MySqlQueryObserver,
    ObserverError,
    PostgresQueryObserver,
)
from pathlib import Path
import json
import tempfile


class _Cursor:
    def __init__(self, rows, statements):
        self._rows = rows
        self._statements = statements
        self.description = [("id",)]

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql, parameters=None):
        self._statements.append(sql)

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows, statements):
        self._rows = rows
        self._statements = statements
        self.autocommit = True

    def cursor(self):
        return _Cursor(self._rows, self._statements)

    def rollback(self):
        self._statements.append("ROLLBACK")

    def close(self):
        self._statements.append("CLOSE")


def fake_psycopg(rows_by_call: list[list[list[int]]]):
    module = types.ModuleType("psycopg")
    statements: list[str] = []
    calls = {"count": 0, "dsns": []}

    def connect(dsn):
        calls["dsns"].append(dsn)
        rows = rows_by_call[min(calls["count"], len(rows_by_call) - 1)]
        calls["count"] += 1
        return _Connection(rows, statements)

    module.connect = connect  # type: ignore[attr-defined]
    return module, statements, calls


class PostgresObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["TEST_PG_DSN"] = "postgresql://user:pw@127.0.0.1/db"
        self.observer = PostgresQueryObserver(
            "orders", "TEST_PG_DSN", "select id from orders order by id"
        )

    def tearDown(self) -> None:
        os.environ.pop("TEST_PG_DSN", None)

    def test_the_snapshot_runs_read_only_with_a_statement_timeout(self) -> None:
        module, statements, calls = fake_psycopg([[[1], [2]]])
        with mock.patch.dict(sys.modules, {"psycopg": module}):
            snapshot = self.observer.snapshot()
        self.assertTrue(any("BEGIN READ ONLY" in item for item in statements))
        self.assertTrue(any("statement_timeout" in item for item in statements))
        self.assertIn("ROLLBACK", statements)
        self.assertEqual(snapshot.state["rows"], [[1], [2]])
        self.assertEqual(calls["dsns"], ["postgresql://user:pw@127.0.0.1/db"])

    def test_a_removed_row_is_destructive(self) -> None:
        before_rows, after_rows = [[[1], [2]]], [[1]]
        module, _, _ = fake_psycopg([before_rows, after_rows])
        with mock.patch.dict(sys.modules, {"psycopg": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertTrue(delta.destructive)

    def test_an_insert_is_a_change_but_not_destructive(self) -> None:
        module, _, _ = fake_psycopg([[[1]], [[1], [2]]])
        with mock.patch.dict(sys.modules, {"psycopg": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertFalse(delta.destructive)

    def test_a_missing_environment_variable_fails_closed(self) -> None:
        os.environ.pop("TEST_PG_DSN")
        module, _, _ = fake_psycopg([[[1]]])
        with mock.patch.dict(sys.modules, {"psycopg": module}):
            with self.assertRaises(ObserverError):
                self.observer.snapshot()

    def test_a_missing_driver_explains_the_optional_extra(self) -> None:
        with mock.patch.dict(sys.modules, {"psycopg": None}):
            with self.assertRaises(ObserverError) as raised:
                self.observer.snapshot()
        self.assertIn("safesink[postgres]", str(raised.exception))


class MySqlObserverTests(unittest.TestCase):
    def test_a_missing_driver_explains_the_optional_extra(self) -> None:
        os.environ["TEST_MYSQL_DSN"] = "mysql://user:pw@127.0.0.1:3306/db"
        observer = MySqlQueryObserver("orders", "TEST_MYSQL_DSN", "select id from orders")
        try:
            with mock.patch.dict(sys.modules, {"pymysql": None}):
                with self.assertRaises(ObserverError) as raised:
                    observer.snapshot()
        finally:
            os.environ.pop("TEST_MYSQL_DSN", None)
        self.assertIn("safesink[mysql]", str(raised.exception))


READ_TOOL = {
    "name": "read_orders",
    "annotations": {"readOnlyHint": True},
    "inputSchema": {"type": "object", "properties": {}},
}


class SqlObserverManifestTests(unittest.TestCase):
    def test_postgres_queries_replace_the_sandbox_observer(self) -> None:
        manifest = build_manifest(
            [READ_TOOL],
            command=["server"],
            observer_root="/sandbox",
            postgres_queries=[("ORDERS_DSN", "select id from orders")],
        )
        self.assertEqual(
            [observer["kind"] for observer in manifest["observers"]], ["postgres-query"]
        )
        observer = manifest["observers"][0]
        self.assertEqual(observer["dsnFromEnv"], "ORDERS_DSN")
        self.assertNotIn("dsn", observer)

    def test_sqlite_and_mysql_can_be_observed_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            database.write_text("", encoding="utf-8")
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=directory,
                sqlite_queries=[(str(database), "select 1")],
                mysql_queries=[("MYSQL_DSN", "select 1")],
            )
        kinds = [observer["kind"] for observer in manifest["observers"]]
        self.assertEqual(kinds, ["sqlite-query", "mysql-query"])

    def test_a_manifest_cannot_hold_a_literal_dsn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "effectfence.mcp.v1",
                        "server": {"transport": "stdio", "command": ["server"]},
                        "observers": [
                            {
                                "id": "pg",
                                "kind": "postgres-query",
                                "dsn": "postgresql://user:pw@host/db",
                                "query": "select 1",
                            }
                        ],
                        "cases": [{"id": "a", "tool": "read_orders"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
