from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Storage:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, input_json TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                    output_path TEXT, last_error TEXT, started_at TEXT, finished_at TEXT,
                    next_retry_at TEXT, reused INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id, seq)
                );
                CREATE TABLE IF NOT EXISTS step_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step_id TEXT, attempt INTEGER, event TEXT NOT NULL, message TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS result_cache (
                    cache_key TEXT PRIMARY KEY, output_path TEXT NOT NULL,
                    step_name TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_steps_task_seq ON steps(task_id, seq);
                CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(status, next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_history_task ON step_history(task_id, id);
                """
            )
        finally:
            connection.close()

    def create_task(
        self, title: str, input_data: dict[str, Any], step_names: list[str], max_attempts: int = 3
    ) -> str:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, 'pending', NULL, ?, ?)",
                (task_id, title, json.dumps(input_data, ensure_ascii=False), now, now),
            )
            for sequence, step_name in enumerate(step_names, start=1):
                connection.execute(
                    "INSERT INTO steps(id, task_id, seq, name, status, attempts, max_attempts) "
                    "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                    (f"step_{uuid.uuid4().hex[:12]}", task_id, sequence, step_name, max_attempts),
                )
            self._history(connection, task_id, None, None, "task_created", "任务已创建")
        return task_id

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT t.*, COUNT(s.id) total_steps,
                       SUM(CASE WHEN s.status = 'succeeded' THEN 1 ELSE 0 END) completed_steps,
                       SUM(CASE WHEN s.status = 'failed' THEN 1 ELSE 0 END) failed_steps
                FROM tasks t LEFT JOIN steps s ON s.task_id = t.id
                GROUP BY t.id ORDER BY t.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("input_json", None)
            total = int(item.get("total_steps") or 0)
            item["progress"] = round(int(item.get("completed_steps") or 0) / total, 3) if total else 0
            result.append(item)
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task_row is None:
                return None
            steps = connection.execute(
                "SELECT * FROM steps WHERE task_id = ? ORDER BY seq", (task_id,)
            ).fetchall()
            history = connection.execute(
                "SELECT * FROM step_history WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        task = dict(task_row)
        task["input"] = json.loads(task.pop("input_json"))
        task["steps"] = [dict(row) for row in steps]
        task["history"] = [dict(row) for row in history]
        total = len(task["steps"])
        task["progress"] = (
            round(sum(step["status"] == "succeeded" for step in task["steps"]) / total, 3)
            if total else 0
        )
        return task


    def claim_next_step(self) -> dict[str, Any] | None:
        now = utc_now()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT s.*, t.input_json, t.title
                FROM steps s JOIN tasks t ON t.id = s.task_id
                WHERE s.status = 'pending' AND t.status IN ('pending', 'running')
                  AND (s.next_retry_at IS NULL OR s.next_retry_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM steps previous
                      WHERE previous.task_id = s.task_id AND previous.seq < s.seq
                        AND previous.status <> 'succeeded'
                  )
                ORDER BY t.created_at, s.seq LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE steps SET status = 'running', attempts = ?, started_at = ?,
                    finished_at = NULL, next_retry_at = NULL, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (attempt, now, row["id"]),
            )
            connection.execute(
                "UPDATE tasks SET status = 'running', error = NULL, updated_at = ? WHERE id = ?",
                (now, row["task_id"]),
            )
            self._history(
                connection, row["task_id"], row["id"], attempt, "step_running",
                f"开始执行步骤 {row['name']}（第 {attempt} 次）",
            )
            claimed = dict(row)
            claimed["attempts"] = attempt
            claimed["started_at"] = now
            claimed["status"] = "running"
            return claimed

    def complete_step(self, step: dict[str, Any], output_path: str, reused: bool) -> None:
        now = utc_now()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE steps SET status = 'succeeded', output_path = ?, finished_at = ?,
                    next_retry_at = NULL, last_error = NULL, reused = ?
                WHERE id = ?
                """,
                (output_path, now, 1 if reused else 0, step["id"]),
            )
            event = "step_reused" if reused else "step_succeeded"
            message = f"复用已有结果 {step['name']}" if reused else f"步骤 {step['name']} 执行成功"
            self._history(
                connection, step["task_id"], step["id"], step["attempts"], event, message
            )
            self._sync_task(connection, step["task_id"], now)

    def fail_step(self, step: dict[str, Any], error: str, retry_delay_seconds: float) -> None:
        now = utc_now()
        attempt = int(step["attempts"])
        can_retry = attempt < int(step["max_attempts"])
        with self._transaction(immediate=True) as connection:
            if can_retry:
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=max(0.0, retry_delay_seconds))
                ).isoformat(timespec="milliseconds")
                connection.execute(
                    "UPDATE steps SET status = 'pending', last_error = ?, next_retry_at = ?, "
                    "finished_at = ? WHERE id = ?",
                    (error, retry_at, now, step["id"]),
                )
                self._history(
                    connection, step["task_id"], step["id"], attempt,
                    "retry_scheduled", f"{step['name']} 失败，将自动重试：{error}",
                )
            else:
                connection.execute(
                    "UPDATE steps SET status = 'failed', last_error = ?, next_retry_at = NULL, "
                    "finished_at = ? WHERE id = ?",
                    (error, now, step["id"]),
                )
                self._history(
                    connection, step["task_id"], step["id"], attempt,
                    "step_failed", f"{step['name']} 达到最大重试次数：{error}",
                )
            self._sync_task(connection, step["task_id"], now)

    def retry_task(self, task_id: str) -> bool:
        now = utc_now()
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id, name FROM steps WHERE task_id = ? AND status = 'failed'", (task_id,)
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                connection.execute(
                    "UPDATE steps SET status = 'pending', attempts = 0, last_error = NULL, "
                    "started_at = NULL, finished_at = NULL, next_retry_at = NULL WHERE id = ?",
                    (row["id"],),
                )
                self._history(
                    connection, task_id, row["id"], None, "manual_retry",
                    f"人工触发重试步骤 {row['name']}，已成功步骤会直接复用",
                )
            connection.execute(
                "UPDATE tasks SET status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            return True

    def recover_interrupted_steps(self) -> int:
        """Return running steps to pending after the process restarts."""
        now = utc_now()
        recovered = 0
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id, task_id, name, attempts FROM steps WHERE status = 'running'"
            ).fetchall()
            task_ids: set[str] = set()
            for row in rows:
                connection.execute(
                    "UPDATE steps SET status = 'pending', finished_at = NULL, next_retry_at = ?, "
                    "last_error = '进程中断，步骤将从最近持久化状态恢复' WHERE id = ?",
                    (now, row["id"]),
                )
                self._history(
                    connection, row["task_id"], row["id"], row["attempts"], "step_recovered",
                    f"检测到进程中断，{row['name']} 已重新进入待执行队列",
                )
                task_ids.add(row["task_id"])
                recovered += 1
            for task_id in task_ids:
                self._sync_task(connection, task_id, now)
        return recovered

    def get_cached_result(self, cache_key: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM result_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return dict(row) if row else None

    def save_cached_result(self, cache_key: str, output_path: str, step_name: str) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO result_cache "
                "(cache_key, output_path, step_name, created_at) VALUES (?, ?, ?, ?)",
                (cache_key, output_path, step_name, utc_now()),
            )

    def stats(self) -> dict[str, int]:
        with self._transaction() as connection:
            task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            step_count = connection.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            cache_count = connection.execute("SELECT COUNT(*) FROM result_cache").fetchone()[0]
        return {
            "tasks": int(task_count),
            "steps": int(step_count),
            "cached_results": int(cache_count),
        }

    def _sync_task(self, connection: sqlite3.Connection, task_id: str, now: str) -> None:
        rows = connection.execute(
            "SELECT status, last_error FROM steps WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        statuses = [row["status"] for row in rows]
        if any(status == "running" for status in statuses):
            status = "running"
        elif any(status == "failed" for status in statuses):
            status = "failed"
        elif statuses and all(status == "succeeded" for status in statuses):
            status = "succeeded"
        else:
            status = "pending"
        error = next(
            (row["last_error"] for row in rows if row["status"] == "failed" and row["last_error"]),
            None,
        )
        connection.execute(
            "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, now, task_id),
        )

    @staticmethod
    def _history(
        connection: sqlite3.Connection,
        task_id: str,
        step_id: str | None,
        attempt: int | None,
        event: str,
        message: str,
    ) -> None:
        connection.execute(
            "INSERT INTO step_history(task_id, step_id, attempt, event, message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, step_id, attempt, event, message, utc_now()),
        )

