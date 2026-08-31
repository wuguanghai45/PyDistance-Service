"""SQLite audit storage with durable station ownership and process exclusion."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now() -> str:
    """Return an unambiguous millisecond UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StationBusy(ValueError):
    """The shared laser station is owned by an active or unacknowledged task."""


class Store:
    """Persist each transition atomically; one service owns one database file."""

    def __init__(self, path: str):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = target.with_suffix(target.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise RuntimeError(
                "标定服务只允许一个进程，请使用 uvicorn --workers 1"
            ) from None
        self.db = sqlite3.connect(target)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS configs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_task ON events(task_id, seq);
            CREATE TABLE IF NOT EXISTS station (id INTEGER PRIMARY KEY CHECK(id=1), task_id TEXT);
            INSERT OR IGNORE INTO station VALUES (1, NULL);
        """)
        for task in self.list_tasks(100000):
            if task["status"] in ("RUNNING", "CANCELLING"):
                task.update(
                    status="INTERRUPTED",
                    finished_at=utc_now(),
                    error="服务已重启；不自动重发动作，请确认机器人停止和料箱安全后解锁",
                )
                self.save_task(task)
                self.event(task["id"], "interrupted", {"message": task["error"]})
                if task["mode"] == "simulation":
                    self.release(task["id"])

    def close(self) -> None:
        """Flush storage and release the process lock."""
        self.db.close()
        self._lock_file.close()

    def save_config(self, config: dict) -> dict:
        """Create an immutable recipe version, preserving earlier task inputs."""
        record = {"id": uuid4().hex, "created_at": utc_now(), "config": config}
        with self.db:
            self.db.execute(
                "INSERT INTO configs VALUES (?, ?)",
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def configs(self) -> list[dict]:
        """List recipes newest first."""
        return [
            json.loads(r[0])
            for r in self.db.execute("SELECT data FROM configs ORDER BY rowid DESC")
        ]

    def config(self, config_id: str) -> dict:
        """Read a recipe or raise KeyError."""
        row = self.db.execute(
            "SELECT data FROM configs WHERE id=?", (config_id,)
        ).fetchone()
        if row is None:
            raise KeyError(config_id)
        return json.loads(row[0])

    def delete_config(self, config_id: str) -> None:
        """Delete one recipe version without changing task snapshots.

        Tasks persist a full copy of their recipe when they are created, so removing
        the editable version does not affect in-progress or historical tasks.
        """
        with self.db:
            deleted = self.db.execute(
                "DELETE FROM configs WHERE id=?", (config_id,)
            ).rowcount
        if not deleted:
            raise KeyError(config_id)

    def owner(self) -> str | None:
        """Read the durable station lock, including failed live tasks."""
        return self.db.execute("SELECT task_id FROM station WHERE id=1").fetchone()[0]

    def create_task(self, task: dict) -> None:
        """Claim the station and persist the new task in one transaction."""
        with self.db:
            changed = self.db.execute(
                "UPDATE station SET task_id=? WHERE id=1 AND task_id IS NULL",
                (task["id"],),
            ).rowcount
            if not changed:
                raise StationBusy(
                    "工位已占用；请完成当前任务或人工确认异常工位安全后解锁"
                )
            self.db.execute(
                "INSERT INTO tasks VALUES (?, ?)",
                (task["id"], json.dumps(task, ensure_ascii=False)),
            )

    def save_task(self, task: dict) -> None:
        """Commit a task snapshot; measurement records are included in the snapshot."""
        with self.db:
            self.db.execute(
                "UPDATE tasks SET data=? WHERE id=?",
                (json.dumps(task, ensure_ascii=False), task["id"]),
            )

    def task(self, task_id: str) -> dict:
        """Read a task snapshot or raise KeyError."""
        row = self.db.execute(
            "SELECT data FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def list_tasks(self, limit: int = 100) -> list[dict]:
        """List recent snapshots, with a caller-supplied bounded count."""
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT data FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)
            )
        ]

    def delete_task(self, task_id: str) -> None:
        """Delete one unlocked task and all audit events recorded for it.

        A station-owning task cannot be removed because it may still need a safety
        acknowledgement and explicit release before another task can start.
        """
        with self.db:
            if self.owner() == task_id:
                raise StationBusy("工位仍被该任务占用；请先完成任务或确认安全后解锁")
            deleted = self.db.execute(
                "DELETE FROM tasks WHERE id=?", (task_id,)
            ).rowcount
            if not deleted:
                raise KeyError(task_id)
            self.db.execute("DELETE FROM events WHERE task_id=?", (task_id,))

    def clear_tasks(self) -> int:
        """Delete every unlocked historical task and its audit events.

        Clearing is rejected while the station has an owner so operational safety
        records remain available until the task is explicitly released.
        """
        with self.db:
            if self.owner() is not None:
                raise StationBusy("工位仍被任务占用；请先完成任务或确认安全后解锁")
            deleted = self.db.execute("DELETE FROM tasks").rowcount
            self.db.execute("DELETE FROM events")
        return deleted

    def release(self, task_id: str) -> None:
        """Release only the matching owner; never unlock another task."""
        with self.db:
            self.db.execute(
                "UPDATE station SET task_id=NULL WHERE id=1 AND task_id=?", (task_id,)
            )

    def event(self, task_id: str, kind: str, data: dict) -> None:
        """Append immutable command, measurement, or operator evidence."""
        with self.db:
            self.db.execute(
                "INSERT INTO events(task_id,timestamp,kind,data) VALUES (?,?,?,?)",
                (task_id, utc_now(), kind, json.dumps(data, ensure_ascii=False)),
            )

    def events(self, task_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        """Read incremental audit events for API consumers."""
        return [
            {**dict(row), "data": json.loads(row["data"])}
            for row in self.db.execute(
                "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, after, limit),
            )
        ]
