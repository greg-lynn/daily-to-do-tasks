"""
SQLite-backed task storage.

Schema
------
tasks
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  title         TEXT NOT NULL
  description   TEXT
  due_date      TEXT   -- ISO date string YYYY-MM-DD (nullable = no specific date)
  priority      TEXT   -- high | medium | low
  completed     INTEGER (0/1)
  completed_at  TEXT   -- ISO datetime
  source        TEXT   -- manual | avoma
  avoma_meeting_uuid  TEXT
  avoma_meeting_subject TEXT
  created_at    TEXT
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config


@dataclass
class Task:
    id: int
    title: str
    description: str
    due_date: Optional[str]          # YYYY-MM-DD or None
    priority: str                    # high | medium | low
    completed: bool
    completed_at: Optional[str]
    source: str                      # manual | avoma
    avoma_meeting_uuid: Optional[str]
    avoma_meeting_subject: Optional[str]
    created_at: str

    @property
    def is_today(self) -> bool:
        if not self.due_date:
            return False
        return self.due_date == date.today().isoformat()

    @property
    def priority_icon(self) -> str:
        return {"high": "!", "medium": "-", "low": "."}.get(self.priority, "-")


class TaskManager:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or Config.DB_PATH
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    due_date TEXT,
                    priority TEXT DEFAULT 'medium',
                    completed INTEGER DEFAULT 0,
                    completed_at TEXT,
                    source TEXT DEFAULT 'manual',
                    avoma_meeting_uuid TEXT,
                    avoma_meeting_subject TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_task(
        self,
        title: str,
        description: str = "",
        due_date: str | None = None,
        priority: str = "medium",
        source: str = "manual",
        avoma_meeting_uuid: str | None = None,
        avoma_meeting_subject: str | None = None,
    ) -> Task:
        now = _now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (title, description, due_date, priority, completed,
                    source, avoma_meeting_uuid, avoma_meeting_subject, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (title, description, due_date, priority, source,
                 avoma_meeting_uuid, avoma_meeting_subject, now),
            )
            conn.commit()
            return self.get_task(cur.lastrowid)  # type: ignore[arg-type]

    def get_task(self, task_id: int) -> Task:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"Task {task_id} not found")
        return _row_to_task(row)

    def list_tasks(
        self,
        due_date: str | None = None,
        include_completed: bool = False,
        source: str | None = None,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list = []
        if due_date is not None:
            clauses.append("(due_date = ? OR due_date IS NULL)")
            params.append(due_date)
        if not include_completed:
            clauses.append("completed = 0")
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_today(self, include_completed: bool = False) -> list[Task]:
        return self.list_tasks(due_date=date.today().isoformat(),
                               include_completed=include_completed)

    def complete_task(self, task_id: int) -> Task:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET completed=1, completed_at=? WHERE id=?",
                (now, task_id),
            )
            conn.commit()
        return self.get_task(task_id)

    def uncomplete_task(self, task_id: int) -> Task:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET completed=0, completed_at=NULL WHERE id=?",
                (task_id,),
            )
            conn.commit()
        return self.get_task(task_id)

    def update_task(
        self,
        task_id: int,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        new_title = title or task.title
        new_desc = description if description is not None else task.description
        new_date = due_date if due_date is not None else task.due_date
        new_prio = priority or task.priority
        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET title=?, description=?, due_date=?, priority=?
                   WHERE id=?""",
                (new_title, new_desc, new_date, new_prio, task_id),
            )
            conn.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()

    def delete_all_avoma_tasks_for_date(self, due_date: str) -> int:
        """Remove auto-imported Avoma tasks for a date before re-importing."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE source='avoma' AND due_date=? AND completed=0",
                (due_date,),
            )
            conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Stats helpers for email
    # ------------------------------------------------------------------

    def today_stats(self) -> dict:
        today = date.today().isoformat()
        all_today = self.list_tasks(due_date=today, include_completed=True)
        done = [t for t in all_today if t.completed]
        pending = [t for t in all_today if not t.completed]
        return {
            "total": len(all_today),
            "completed": len(done),
            "pending": len(pending),
            "tasks": all_today,
            "done_tasks": done,
            "pending_tasks": pending,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"] or "",
        due_date=row["due_date"],
        priority=row["priority"] or "medium",
        completed=bool(row["completed"]),
        completed_at=row["completed_at"],
        source=row["source"] or "manual",
        avoma_meeting_uuid=row["avoma_meeting_uuid"],
        avoma_meeting_subject=row["avoma_meeting_subject"],
        created_at=row["created_at"],
    )
