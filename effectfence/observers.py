from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ObserverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    observer_id: str
    kind: str
    digest: str
    state: Any
    world: str
    sensitive: bool


@dataclass(frozen=True)
class Delta:
    observer_id: str
    kind: str
    changed: bool
    destructive: bool
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    before_digest: str = ""
    after_digest: str = ""
    world: str = "closed"
    sensitive: bool = False

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "observer_id": self.observer_id,
            "kind": self.kind,
            "world": self.world,
            "changed": self.changed,
            "destructive": self.destructive,
            "counts": {
                "created": len(self.created),
                "modified": len(self.modified),
                "deleted": len(self.deleted),
            },
        }
        if self.sensitive:
            result["digestsRedacted"] = True
        else:
            result["beforeSha256"] = self.before_digest
            result["afterSha256"] = self.after_digest
            result["paths"] = {
                "created": list(self.created),
                "modified": list(self.modified),
                "deleted": list(self.deleted),
            }
        return result


class Observer(Protocol):
    observer_id: str
    kind: str
    world: str
    sensitive: bool

    def snapshot(self) -> Snapshot: ...

    def diff(self, before: Snapshot, after: Snapshot) -> Delta: ...


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode()}
    return str(value)


class FilesystemObserver:
    kind = "filesystem"

    def __init__(
        self,
        observer_id: str,
        root: Path,
        *,
        exclude: tuple[str, ...] = (),
        max_files: int = 10_000,
        max_entries: int = 50_000,
        max_total_bytes: int = 100 * 1024 * 1024,
        world: str = "closed",
        sensitive: bool = False,
    ):
        if max_files < 1 or max_entries < 1 or max_total_bytes < 1:
            raise ObserverError("filesystem observer limits must be positive")
        self.observer_id = observer_id
        self.root = root.resolve()
        self.exclude = exclude
        self.max_files = max_files
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes
        self.world = world
        self.sensitive = sensitive

    def _excluded(self, relative: str) -> bool:
        return any(fnmatch.fnmatch(relative, pattern) for pattern in self.exclude)

    def snapshot(self) -> Snapshot:
        if not self.root.exists():
            raise ObserverError(f"filesystem observer root does not exist: {self.root}")
        if not self.root.is_dir():
            raise ObserverError(f"filesystem observer root is not a directory: {self.root}")
        files: dict[str, dict[str, Any]] = {}
        total_bytes = 0
        for entry_number, path in enumerate(sorted(self.root.rglob("*")), start=1):
            if entry_number > self.max_entries:
                raise ObserverError(
                    f"filesystem observer {self.observer_id!r} exceeded "
                    f"maxEntries={self.max_entries}"
                )
            relative = path.relative_to(self.root).as_posix()
            if self._excluded(relative):
                continue
            if path.is_symlink():
                files[relative] = {"type": "symlink", "target": os.readlink(path)}
                continue
            if not path.is_file():
                continue
            if len(files) >= self.max_files:
                raise ObserverError(
                    f"filesystem observer {self.observer_id!r} exceeded "
                    f"maxFiles={self.max_files}"
                )
            size = path.stat().st_size
            total_bytes += size
            if total_bytes > self.max_total_bytes:
                raise ObserverError(
                    f"filesystem observer {self.observer_id!r} exceeded "
                    f"maxTotalBytes={self.max_total_bytes}"
                )
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[relative] = {
                "type": "file",
                "size": size,
                "sha256": digest.hexdigest(),
            }
        return Snapshot(
            self.observer_id,
            self.kind,
            canonical_digest(files),
            files,
            self.world,
            self.sensitive,
        )

    def diff(self, before: Snapshot, after: Snapshot) -> Delta:
        before_files = before.state
        after_files = after.state
        created = tuple(sorted(set(after_files) - set(before_files)))
        deleted = tuple(sorted(set(before_files) - set(after_files)))
        modified = tuple(
            sorted(
                path
                for path in set(before_files) & set(after_files)
                if before_files[path] != after_files[path]
            )
        )
        return Delta(
            observer_id=self.observer_id,
            kind=self.kind,
            world=self.world,
            changed=bool(created or modified or deleted),
            destructive=bool(deleted),
            created=created,
            modified=modified,
            deleted=deleted,
            before_digest=before.digest,
            after_digest=after.digest,
            sensitive=self.sensitive,
        )


class SqliteQueryObserver:
    kind = "sqlite-query"

    def __init__(
        self,
        observer_id: str,
        database: Path,
        query: str,
        parameters: tuple[Any, ...] = (),
        *,
        world: str = "closed",
        sensitive: bool = True,
    ):
        self.observer_id = observer_id
        self.database = database.resolve()
        self.query = query
        self.parameters = parameters
        self.world = world
        self.sensitive = sensitive

    @staticmethod
    def _authorizer(action: int, *_: Any) -> int:
        denied = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_UPDATE,
        }
        return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

    def snapshot(self) -> Snapshot:
        if not self.database.is_file():
            raise ObserverError(f"SQLite database does not exist: {self.database}")
        uri = f"file:{urllib.parse.quote(str(self.database))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.set_authorizer(self._authorizer)
            cursor = connection.execute(self.query, self.parameters)
            columns = [item[0] for item in cursor.description or ()]
            rows = [[_json_value(value) for value in row] for row in cursor.fetchall()]
        except sqlite3.Error as error:
            raise ObserverError(
                f"SQLite observer {self.observer_id!r} query failed: {error}"
            ) from error
        finally:
            connection.close()
        state = {"columns": columns, "rows": rows}
        return Snapshot(
            self.observer_id,
            self.kind,
            canonical_digest(state),
            state,
            self.world,
            self.sensitive,
        )

    def diff(self, before: Snapshot, after: Snapshot) -> Delta:
        before_rows = before.state["rows"]
        after_rows = after.state["rows"]
        before_multiset: dict[str, int] = {}
        after_multiset: dict[str, int] = {}
        for row in before_rows:
            key = json.dumps(row, sort_keys=True, separators=(",", ":"))
            before_multiset[key] = before_multiset.get(key, 0) + 1
        for row in after_rows:
            key = json.dumps(row, sort_keys=True, separators=(",", ":"))
            after_multiset[key] = after_multiset.get(key, 0) + 1
        removed_row = any(
            after_multiset.get(key, 0) < count for key, count in before_multiset.items()
        )
        return Delta(
            observer_id=self.observer_id,
            kind=self.kind,
            world=self.world,
            changed=before.digest != after.digest,
            destructive=removed_row,
            before_digest=before.digest,
            after_digest=after.digest,
            sensitive=self.sensitive,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class HttpJsonObserver:
    kind = "http-json"

    def __init__(
        self,
        observer_id: str,
        url: str,
        *,
        allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1"),
        headers_from_env: dict[str, str] | None = None,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 1024 * 1024,
        world: str = "open",
        sensitive: bool = True,
    ):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ObserverError("HTTP observer URL must use http or https")
        if parsed.hostname not in allowed_hosts:
            raise ObserverError(
                f"HTTP observer host {parsed.hostname!r} is not in allowedHosts"
            )
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ObserverError("HTTP observer limits must be positive")
        self.observer_id = observer_id
        self.url = url
        self.headers_from_env = headers_from_env or {}
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.world = world
        self.sensitive = sensitive

    def snapshot(self) -> Snapshot:
        headers = {}
        for header, variable in self.headers_from_env.items():
            if variable not in os.environ:
                raise ObserverError(
                    f"HTTP observer requires environment variable {variable!r}"
                )
            headers[header] = os.environ[variable]
        request = urllib.request.Request(
            self.url,
            headers=headers,
            method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                data = response.read(self.max_response_bytes + 1)
        except urllib.error.URLError as error:
            raise ObserverError(
                f"HTTP observer {self.observer_id!r} request failed "
                f"({error.__class__.__name__}; details omitted)"
            ) from error
        if len(data) > self.max_response_bytes:
            raise ObserverError(
                f"HTTP observer {self.observer_id!r} response exceeded "
                f"maxResponseBytes={self.max_response_bytes}"
            )
        try:
            state = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ObserverError(
                f"HTTP observer {self.observer_id!r} returned invalid JSON"
            ) from error
        return Snapshot(
            self.observer_id,
            self.kind,
            canonical_digest(state),
            state,
            self.world,
            self.sensitive,
        )

    def diff(self, before: Snapshot, after: Snapshot) -> Delta:
        return Delta(
            observer_id=self.observer_id,
            kind=self.kind,
            world=self.world,
            changed=before.digest != after.digest,
            destructive=False,
            before_digest=before.digest,
            after_digest=after.digest,
            sensitive=self.sensitive,
        )
