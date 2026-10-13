"""SQLite registry: sessions, pending approvals and questions, queued phone messages, flags.

WAL mode; every write runs on one dedicated writer thread, so writes from hooks and adapters
never interleave. Reads use the same connection through that thread too, which keeps the
API simple and is fast enough at this scale.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import JsonValue

from agent_relay.log import get_logger
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import (
    PendingKind,
    PendingRequest,
    PendingStatus,
    QueuedMessage,
    QueueKind,
    SessionRecord,
    SessionStatus,
)

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    harness_id TEXT NOT NULL,
    workspace TEXT,
    model TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    webex_thread_id TEXT,
    title TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_harness_id ON sessions (harness, harness_id);
CREATE INDEX IF NOT EXISTS sessions_thread ON sessions (webex_thread_id);
CREATE TABLE IF NOT EXISTS pending (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    session_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    answer TEXT,
    webex_message_id TEXT,
    created TEXT NOT NULL,
    deadline TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS away (
    scope TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

GLOBAL_SCOPE = "*"
# Per-session grants live under these prefixes, followed by `{session_key}:`; the lifecycle drops them.
ALLOW_SESSION_PREFIX = "allow_session:"
RECENT_APPROVAL_PREFIX = "approved_recent:"
# A recent approval is honoured this long; the stored value is `<ISO-8601 UTC> <decision>`.
RECENT_APPROVAL_TTL = timedelta(seconds=60)
# Webex card nonces, valued with the ISO-8601 UTC time they were issued.
CARD_NONCE_PREFIX = "card.nonce:"
# Processed Webex message ids, valued with the time they were seen so they can be pruned.
SEEN_PREFIX = "webex.seen:"


def _add_pending_status_index(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS pending_status ON pending (status)")


def _drop_bogus_sticky_models(conn: sqlite3.Connection) -> None:
    # `model:vscode_local` predates the harness rename; a sticky "Run" came from a mis-parsed /new.
    conn.execute(
        "DELETE FROM kv WHERE key IN ('model:vscode_local', 'model:vscode') AND value IN ('Run', '')"
    )


def _stamp_seen_keys(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE kv SET value=? WHERE substr(key, 1, ?) = ? AND value = '1'",
        (_ts(utcnow()), len(SEEN_PREFIX), SEEN_PREFIX),
    )


# Ordered and append-only: entry N moves the schema from version N-1 to N (`PRAGMA user_version`).
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _add_pending_status_index,
    _drop_bogus_sticky_models,
    _stamp_seen_keys,
)


def _ts(value: datetime) -> str:
    return value.isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > len(MIGRATIONS):
        log.warning(f"registry schema v{version} is newer than this build (v{len(MIGRATIONS)})")
        return
    for target, migration in enumerate(MIGRATIONS, start=1):
        if target <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration(conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_key=row["session_key"],
        harness=Harness(row["harness"]),
        harness_id=row["harness_id"],
        workspace=row["workspace"],
        model=row["model"],
        origin=Origin(row["origin"]),
        status=SessionStatus(row["status"]),
        webex_thread_id=row["webex_thread_id"],
        title=row["title"],
        extra=json.loads(row["extra"]),
        created=datetime.fromisoformat(row["created"]),
        updated=datetime.fromisoformat(row["updated"]),
    )


def _readable(rows: list[sqlite3.Row]) -> list[SessionRecord]:
    """Rows this build can read; one the schema does not know (a newer build wrote it) is skipped."""
    found: list[SessionRecord] = []
    for row in rows:
        try:
            found.append(_session_from_row(row))
        except ValueError as exc:
            log.warning(f"skipping unreadable session row {row['session_key']}: {exc}")
    return found


def stamped_at(value: str) -> datetime | None:
    """The leading ISO-8601 time of a stored value (`<time>` or `<time> <rest>`), or None. Naive means UTC."""
    head = value.split(maxsplit=1)[0] if value.strip() else ""
    try:
        parsed = datetime.fromisoformat(head)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _expired(value: str, older_than: datetime, drop_unparseable: bool) -> bool:
    stamped = stamped_at(value)
    return drop_unparseable if stamped is None else stamped < older_than


def _pending_from_row(row: sqlite3.Row) -> PendingRequest:
    return PendingRequest(
        request_id=row["request_id"],
        kind=row["kind"],
        session_key=row["session_key"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        answer=json.loads(row["answer"]) if row["answer"] else None,
        webex_message_id=row["webex_message_id"],
        created=datetime.fromisoformat(row["created"]),
        deadline=datetime.fromisoformat(row["deadline"]),
    )


class Registry:
    def __init__(self, db_file: Path) -> None:
        self.db_file = db_file
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="registry")
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        await self._run(self._open_sync)

    def _open_sync(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_file, timeout=30.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        self._conn = conn
        self.db_file.chmod(0o600)

    async def close(self) -> None:
        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await self._run(_close)
        self._executor.shutdown(wait=True)

    async def _run[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("registry is not open")
        return self._conn

    def _execute(self, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute(sql, params)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return cur

    def _write(self, sql: str, params: tuple[object, ...]) -> int:
        """Run one write in its own transaction; returns the number of rows changed."""
        return self._execute(sql, params).rowcount

    def _insert(self, sql: str, params: tuple[object, ...]) -> int:
        """Run one insert in its own transaction; returns the new row id."""
        return self._execute(sql, params).lastrowid or 0

    # sessions

    async def create_session(self, record: SessionRecord) -> SessionRecord:
        """Insert a session. If its key or its `(harness, harness_id)` already exists, adopt that row.

        Returns the stored row, which is untouched when it already existed.
        """
        now = utcnow()
        sql = """
        INSERT INTO sessions (session_key, harness, harness_id, workspace, model, origin, status,
                              webex_thread_id, title, extra, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """
        params = (
            record.session_key,
            record.harness.value,
            record.harness_id,
            record.workspace,
            record.model,
            record.origin.value,
            record.status.value,
            record.webex_thread_id,
            record.title,
            json.dumps(record.extra),
            _ts(record.created),
            _ts(now),
        )

        def _create() -> SessionRecord:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(sql, params)
                row = db.execute(
                    "SELECT * FROM sessions WHERE session_key=? OR (harness=? AND harness_id=?)",
                    (record.session_key, record.harness.value, record.harness_id),
                ).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            found = _readable([row]) if row else []
            return found[0] if found else record.model_copy(update={"updated": now})

        return await self._run(_create)

    async def get_session(self, session_key: str) -> SessionRecord | None:
        def _get() -> SessionRecord | None:
            row = self._db().execute("SELECT * FROM sessions WHERE session_key=?", (session_key,)).fetchone()
            found = _readable([row]) if row else []
            return found[0] if found else None

        return await self._run(_get)

    async def find_session(self, harness: Harness, harness_id: str) -> SessionRecord | None:
        def _get() -> SessionRecord | None:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM sessions WHERE harness=? AND harness_id=?", (harness.value, harness_id)
                )
                .fetchone()
            )
            found = _readable([row]) if row else []
            return found[0] if found else None

        return await self._run(_get)

    async def session_by_thread(self, thread_id: str) -> SessionRecord | None:
        def _get() -> SessionRecord | None:
            row = (
                self._db().execute("SELECT * FROM sessions WHERE webex_thread_id=?", (thread_id,)).fetchone()
            )
            found = _readable([row]) if row else []
            return found[0] if found else None

        return await self._run(_get)

    async def list_sessions(self, include_ended: bool = False) -> list[SessionRecord]:
        def _list() -> list[SessionRecord]:
            sql = "SELECT * FROM sessions"
            if not include_ended:
                sql += " WHERE status != 'ended'"
            sql += " ORDER BY updated DESC"
            return _readable(self._db().execute(sql).fetchall())

        return await self._run(_list)

    async def update_session_fields(self, session_key: str, **fields: str | None) -> None:
        """Set just the named columns. `status` is not one of them: only the lifecycle changes it."""
        allowed = {"model", "webex_thread_id", "title", "workspace", "harness", "harness_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        params = (*fields.values(), _ts(utcnow()), session_key)
        sql = f"UPDATE sessions SET {assignments}, updated=? WHERE session_key=?"
        await self._run(lambda: self._write(sql, params))

    async def set_session_status(self, session_key: str, status: SessionStatus) -> None:
        """Write the status column. Only `core.lifecycle` calls this; everything else goes through it."""
        sql = "UPDATE sessions SET status=?, updated=? WHERE session_key=?"
        await self._run(lambda: self._write(sql, (status.value, _ts(utcnow()), session_key)))

    async def drop_session_grants(self, session_key: str) -> None:
        """Forget the "allow for session" and recent-approval flags that belong to one session."""

        def _drop() -> None:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                for prefix in (ALLOW_SESSION_PREFIX, RECENT_APPROVAL_PREFIX):
                    scoped = f"{prefix}{session_key}:"
                    db.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(scoped), scoped))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        await self._run(_drop)

    async def set_session_extra(self, session_key: str, key: str, value: JsonValue) -> None:
        """Set one key of the session's `extra` JSON, leaving the rest as it is."""

        def _update() -> None:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT extra FROM sessions WHERE session_key=?", (session_key,)).fetchone()
                if row is not None:
                    extra = json.loads(row["extra"])
                    extra[key] = value
                    db.execute(
                        "UPDATE sessions SET extra=?, updated=? WHERE session_key=?",
                        (json.dumps(extra), _ts(utcnow()), session_key),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        await self._run(_update)

    # pending approvals and questions

    async def add_pending(self, request: PendingRequest) -> None:
        sql = """
        INSERT OR REPLACE INTO pending (request_id, kind, session_key, payload, status, answer,
                                        webex_message_id, created, deadline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            request.request_id,
            request.kind,
            request.session_key,
            json.dumps(request.payload),
            request.status,
            json.dumps(request.answer) if request.answer is not None else None,
            request.webex_message_id,
            _ts(request.created),
            _ts(request.deadline),
        )
        await self._run(lambda: self._write(sql, params))

    async def get_pending(self, request_id: str) -> PendingRequest | None:
        def _get() -> PendingRequest | None:
            row = self._db().execute("SELECT * FROM pending WHERE request_id=?", (request_id,)).fetchone()
            return _pending_from_row(row) if row else None

        return await self._run(_get)

    async def open_pending(self, kind: PendingKind | None = None) -> list[PendingRequest]:
        def _list() -> list[PendingRequest]:
            if kind is None:
                rows = (
                    self._db()
                    .execute("SELECT * FROM pending WHERE status='open' ORDER BY created")
                    .fetchall()
                )
            else:
                rows = (
                    self._db()
                    .execute("SELECT * FROM pending WHERE status='open' AND kind=? ORDER BY created", (kind,))
                    .fetchall()
                )
            return [_pending_from_row(r) for r in rows]

        return await self._run(_list)

    async def resolve_pending(
        self, request_id: str, answer: dict[str, JsonValue], status: PendingStatus = "resolved"
    ) -> bool:
        """Resolve an open request. Returns False if it was already resolved (first answer wins)."""
        sql = "UPDATE pending SET status=?, answer=? WHERE request_id=? AND status='open'"
        params = (status, json.dumps(answer), request_id)
        changed = await self._run(lambda: self._write(sql, params))
        return changed > 0

    async def reopen_pending(self, request_id: str) -> bool:
        """Put a resolved request back to open, answer cleared and deadline kept. False if not resolved."""
        sql = "UPDATE pending SET status='open', answer=NULL WHERE request_id=? AND status='resolved'"
        return await self._run(lambda: self._write(sql, (request_id,))) > 0

    async def set_pending_message(self, request_id: str, message_id: str) -> None:
        sql = "UPDATE pending SET webex_message_id=? WHERE request_id=?"
        await self._run(lambda: self._write(sql, (message_id, request_id)))

    # queued phone messages

    async def enqueue(self, session_key: str, kind: QueueKind, text: str) -> int:
        sql = "INSERT INTO queue (session_key, kind, text, created) VALUES (?, ?, ?, ?)"
        params = (session_key, kind, text, _ts(utcnow()))
        return await self._run(lambda: self._insert(sql, params))

    async def pop_queued(self, session_key: str, kinds: tuple[QueueKind, ...]) -> list[QueuedMessage]:
        placeholders = ",".join("?" for _ in kinds)

        def _pop() -> list[QueuedMessage]:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    f"SELECT * FROM queue WHERE session_key=? AND consumed=0 AND kind IN ({placeholders}) "
                    "ORDER BY id",
                    (session_key, *kinds),
                ).fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    db.execute(f"UPDATE queue SET consumed=1 WHERE id IN ({','.join('?' for _ in ids)})", ids)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return [
                QueuedMessage(
                    id=r["id"],
                    session_key=r["session_key"],
                    kind=r["kind"],
                    text=r["text"],
                    created=datetime.fromisoformat(r["created"]),
                )
                for r in rows
            ]

        return await self._run(_pop)

    async def drop_queued(self, session_key: str) -> None:
        sql = "UPDATE queue SET consumed=1 WHERE session_key=? AND consumed=0"
        await self._run(lambda: self._write(sql, (session_key,)))

    # away mode

    async def set_away(self, enabled: bool, scope: str = GLOBAL_SCOPE) -> None:
        sql = (
            "INSERT INTO away (scope, enabled) VALUES (?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled"
        )
        await self._run(lambda: self._write(sql, (scope, int(enabled))))

    async def away_scopes(self) -> dict[str, bool]:
        def _all() -> dict[str, bool]:
            rows = self._db().execute("SELECT scope, enabled FROM away").fetchall()
            return {r["scope"]: bool(r["enabled"]) for r in rows}

        return await self._run(_all)

    # key/value flags

    async def kv_get(self, key: str) -> str | None:
        def _get() -> str | None:
            row = self._db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

        return await self._run(_get)

    async def kv_prefix(self, prefix: str) -> dict[str, str]:
        def _get() -> dict[str, str]:
            rows = (
                self._db()
                .execute(
                    "SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key",
                    (len(prefix), prefix),
                )
                .fetchall()
            )
            return {str(r["key"]): str(r["value"]) for r in rows}

        return await self._run(_get)

    async def kv_set(self, key: str, value: str) -> None:
        sql = "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        await self._run(lambda: self._write(sql, (key, value)))

    async def kv_delete(self, key: str) -> None:
        await self._run(lambda: self._write("DELETE FROM kv WHERE key=?", (key,)))

    async def kv_pop(self, key: str) -> str | None:
        """Delete a key and return its value in one transaction, so only one caller gets it."""

        def _pop() -> str | None:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("DELETE FROM kv WHERE key=? RETURNING value", (key,)).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return str(row["value"]) if row else None

        return await self._run(_pop)

    async def prune_seen(self, older_than: datetime) -> int:
        """Delete processed-message markers last written before `older_than`. Returns how many."""
        sql = "DELETE FROM kv WHERE substr(key, 1, ?) = ? AND value < ?"
        params = (len(SEEN_PREFIX), SEEN_PREFIX, _ts(older_than))
        return await self._run(lambda: self._write(sql, params))

    async def prune_stamped(self, prefix: str, older_than: datetime, *, drop_unparseable: bool) -> int:
        """Delete `prefix` keys whose value starts with a timestamp before `older_than`.

        A value that does not start with an ISO-8601 time is deleted only when `drop_unparseable`.
        """

        def _prune() -> int:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT key, value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
                ).fetchall()
                doomed = [r["key"] for r in rows if _expired(r["value"], older_than, drop_unparseable)]
                for key in doomed:
                    db.execute("DELETE FROM kv WHERE key=?", (key,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return len(doomed)

        return await self._run(_prune)

    async def schema_version(self) -> int:
        return await self._run(lambda: int(self._db().execute("PRAGMA user_version").fetchone()[0]))
