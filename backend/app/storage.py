from __future__ import annotations

import secrets
import sqlite3
import shutil
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .config import Settings


DEFAULT_SCREEN_TEXT = "С Новым годом!"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.data_dir = settings.resolved_data_dir
        self.db_path = settings.resolved_database_path
        self.telegram_bot_status = "not_configured" if not settings.telegram_bot_token else "not_started"
        self.telegram_bot_error: str | None = None
        self.video_worker_status = "not_started"
        self.video_worker_error: str | None = None
        self._app_lock_handle: Any | None = None

    def init(self) -> None:
        if not self.settings.telegram_bot_token:
            self.telegram_bot_status = "not_configured"
            self.telegram_bot_error = None
        elif self.telegram_bot_status in {"not_configured", "stopped"}:
            self.telegram_bot_status = "not_started"
        self._ensure_directories()
        self._cleanup_temp_dir()
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            self._apply_migrations(conn)
            self._delete_expired_table_selections(conn)
            conn.execute("INSERT OR IGNORE INTO app_state (id, version, created_at, updated_at) VALUES (1, 0, ?, ?)", (utc_now(), utc_now()))
            conn.execute(
                """
                UPDATE submissions
                SET preparation_status = 'received', processing_locked_at = NULL, updated_at = ?
                WHERE preparation_status IN ('downloading', 'processing')
                """,
                (utc_now(),),
            )
            self._recover_playing_queue_items(conn)
            conn.commit()

    def acquire_app_lock(self) -> None:
        self._ensure_directories()
        lock_path = self.data_dir / "app.lock"
        handle = lock_path.open("a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another Visual Pozdr instance is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._app_lock_handle = handle

    def release_app_lock(self) -> None:
        handle = self._app_lock_handle
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._app_lock_handle = None

    def _cleanup_temp_dir(self) -> None:
        temp_dir = self.data_dir / "temp"
        if not temp_dir.exists():
            return
        for child in temp_dir.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
        submission_columns = {
            "telegram_file_id": "TEXT",
            "telegram_file_unique_id": "TEXT",
            "telegram_file_size": "INTEGER",
            "telegram_duration_sec": "INTEGER",
            "processing_attempts": "INTEGER NOT NULL DEFAULT 0",
            "processing_locked_at": "TEXT",
            "hidden_from_wall": "INTEGER NOT NULL DEFAULT 0",
            "table_number": "INTEGER",
            "table_task": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in submission_columns.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {column} {definition}")
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "theme" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN theme TEXT NOT NULL DEFAULT 'new_year'")
        if "playback_state" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN playback_state TEXT NOT NULL DEFAULT 'stopped'")
        if "playback_mode" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN playback_mode TEXT NOT NULL DEFAULT 'auto_loop'")
        for column, definition in {
            "primary_color": "TEXT NOT NULL DEFAULT '#102033'",
            "accent_color": "TEXT NOT NULL DEFAULT '#0f5f5c'",
            "background_path": "TEXT",
            "table_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in event_columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
        conn.execute("UPDATE events SET theme = 'new_year' WHERE theme IN ('simple_new_year', '')")
        queue_columns = {row["name"] for row in conn.execute("PRAGMA table_info(queue_items)").fetchall()}
        for column in ["started_at", "completed_at"]:
            if column not in queue_columns:
                conn.execute(f"ALTER TABLE queue_items ADD COLUMN {column} TEXT")
        for column, definition in {
            "is_loop": "INTEGER NOT NULL DEFAULT 0",
            "hidden_from_history": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in queue_columns:
                conn.execute(f"ALTER TABLE queue_items ADD COLUMN {column} {definition}")
        playback_session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(playback_sessions)").fetchall()}
        if "error_reason" not in playback_session_columns:
            conn.execute("ALTER TABLE playback_sessions ADD COLUMN error_reason TEXT")
        for column in ["progress_sec", "duration_sec"]:
            if column not in playback_session_columns:
                conn.execute(f"ALTER TABLE playback_sessions ADD COLUMN {column} REAL")
        # Wall capacity is fixed so existing events use the single supported layout.
        conn.execute("UPDATE events SET wall_limit = 12 WHERE wall_limit != 12")

    def _recover_playing_queue_items(self, conn: sqlite3.Connection) -> None:
        playing_rows = conn.execute("SELECT * FROM queue_items WHERE status = 'playing' ORDER BY updated_at ASC").fetchall()
        if not playing_rows:
            conn.execute("UPDATE events SET playback_state = 'stopped' WHERE playback_state != 'stopped'")
            return
        for row in playing_rows:
            min_position = conn.execute(
                "SELECT COALESCE(MIN(position), 0) AS position FROM queue_items WHERE event_id = ? AND status = 'queued'",
                (row["event_id"],),
            ).fetchone()["position"]
            conn.execute(
                """
                UPDATE queue_items
                SET status = 'queued', playback_id = NULL, position = ?, updated_at = ?
                WHERE id = ?
                """,
                (min_position - 1, utc_now(), row["id"]),
            )
        conn.execute("UPDATE events SET playback_state = 'stopped'")
        self._normalize_all_queue_positions(conn)

    def _stop_show_locked(self, conn: sqlite3.Connection, event_id: str, now: str) -> None:
        item = conn.execute("SELECT * FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
        if item is not None:
            min_position = conn.execute(
                "SELECT COALESCE(MIN(position), 0) AS position FROM queue_items WHERE event_id = ? AND status = 'queued'",
                (event_id,),
            ).fetchone()["position"]
            conn.execute(
                "UPDATE queue_items SET status = 'queued', playback_id = NULL, position = ?, updated_at = ? WHERE id = ?",
                (min_position - 1, now, item["id"]),
            )
        conn.execute("UPDATE events SET playback_state = 'stopped', updated_at = ? WHERE id = ?", (now, event_id))
        self._normalize_queue_positions(conn, event_id)

    def _is_screen_fresh(self, row: sqlite3.Row | None, now_dt: datetime | None = None) -> bool:
        if row is None:
            return False
        last_seen = parse_utc(row["last_seen_at"])
        if last_seen is None:
            return False
        now_dt = now_dt or datetime.now(timezone.utc)
        return now_dt - last_seen <= timedelta(seconds=self.settings.screen_heartbeat_timeout_sec)

    def _active_screen_row(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM playback_sessions
            WHERE state IN ('active', 'autoplay_blocked', 'loading', 'playing', 'paused', 'error')
            ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()

    def _handle_stale_active_screen_locked(self, conn: sqlite3.Connection, now: str) -> bool:
        active = self._active_screen_row(conn)
        if active is None or self._is_screen_fresh(active):
            return False
        conn.execute("UPDATE playback_sessions SET state = 'stale', updated_at = ? WHERE id = ?", (now, active["id"]))
        if active["event_id"] is not None:
            self._stop_show_locked(conn, active["event_id"], now)
        return True

    def refresh_screen_liveness(self) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._handle_stale_active_screen_locked(conn, now)
            if changed:
                self._bump_version(conn)
            conn.commit()

    def get_free_disk_mb(self) -> int | None:
        try:
            usage = shutil.disk_usage(self.data_dir)
        except OSError:
            return None
        return usage.free // (1024 * 1024)

    def has_min_free_disk(self) -> bool:
        free_mb = self.get_free_disk_mb()
        return free_mb is None or free_mb >= self.settings.min_free_disk_mb

    def _start_next_queued_item(self, conn: sqlite3.Connection, event_id: str, now: str) -> None:
        playing = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
        if playing is not None:
            conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, event_id))
            return
        item = conn.execute(
            "SELECT * FROM queue_items WHERE event_id = ? AND status = 'queued' ORDER BY position ASC, created_at ASC LIMIT 1",
            (event_id,),
        ).fetchone()
        if item is None:
            conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            return
        conn.execute(
            "UPDATE queue_items SET status = 'playing', playback_id = ?, started_at = ?, updated_at = ? WHERE id = ?",
            (str(uuid4()), now, now, item["id"]),
        )
        conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, event_id))

    def _ensure_loop_queue_locked(self, conn: sqlite3.Connection, event_id: str, now: str) -> None:
        active = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status IN ('queued', 'playing') LIMIT 1", (event_id,)).fetchone()
        if active is not None:
            return
        submissions = conn.execute(
            """
            SELECT s.*
            FROM submissions s
            JOIN wall_items wi ON wi.submission_id = s.id
            WHERE s.event_id = ?
              AND s.preparation_status = 'ready'
              AND s.moderation_status = 'approved'
            ORDER BY wi.position ASC, wi.created_at ASC
            """,
            (event_id,),
        ).fetchall()
        for submission in submissions:
            self._enqueue_submission_if_allowed(conn, submission, now, allow_terminal_previous=True, is_loop=True)

    def _start_next_or_cycle_item(self, conn: sqlite3.Connection, event_id: str, now: str) -> None:
        self._ensure_loop_queue_locked(conn, event_id, now)
        self._start_next_queued_item(conn, event_id, now)

    def _continue_if_show_waiting(self, conn: sqlite3.Connection, event_id: str, now: str) -> None:
        event = conn.execute("SELECT playback_state, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
        if event is not None and event["playback_state"] == "idle" and event["playback_mode"] == "auto_loop":
            self._start_next_or_cycle_item(conn, event_id, now)

    def _ensure_directories(self) -> None:
        for path in [
            self.data_dir,
            self.data_dir / "events",
            self.data_dir / "temp",
            self.data_dir / "logs",
        ]:
            path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def get_state(self) -> dict[str, Any]:
        self.refresh_screen_liveness()
        with self.connect() as conn:
            # All fields, including version, belong to the same SQLite snapshot.
            conn.execute("BEGIN")
            version = conn.execute("SELECT version FROM app_state WHERE id = 1").fetchone()["version"]
            events = [self._with_connection_link(serialize_event(row)) for row in conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()]
            active_event = next((event for event in events if event["active"]), None)
            active_event_id = active_event["id"] if active_event else None
            counts = {
                "submissions": conn.execute("SELECT COUNT(*) AS count FROM submissions WHERE event_id = ?", (active_event_id,)).fetchone()["count"] if active_event_id else 0,
                "queued": conn.execute("SELECT COUNT(*) AS count FROM queue_items WHERE event_id = ? AND status = 'queued'", (active_event_id,)).fetchone()["count"] if active_event_id else 0,
                "playing": conn.execute("SELECT COUNT(*) AS count FROM queue_items WHERE event_id = ? AND status = 'playing'", (active_event_id,)).fetchone()["count"] if active_event_id else 0,
            }
            recent_submissions = [
                serialize_submission(row)
                for row in conn.execute("SELECT * FROM submissions WHERE event_id = ? ORDER BY created_at DESC LIMIT 20", (active_event_id,)).fetchall()
            ] if active_event_id else []
            pending_submissions = self._list_pending_submissions(conn, active_event_id) if active_event_id else []
            queue_items = self._list_queue_items(conn, active_event_id) if active_event_id else []
            history_items = self._list_history_items(conn, active_event_id) if active_event_id else []
            wall_items = self._list_wall_items(conn, active_event_id) if active_event_id else []
            tables = self._list_event_tables(conn, active_event_id) if active_event_id else []
            failed_submissions = self._list_failed_submissions(conn, active_event_id) if active_event_id else []
            playback = {
                "state": active_event["playback_state"] if active_event else "stopped",
                "current": next((item for item in queue_items if item["status"] == "playing"), None),
            }
            screen = self._get_screen_status(conn)
            free_disk_mb = self.get_free_disk_mb()
        return {
            "version": version,
            "active_event": active_event,
            "events": events,
            "counts": counts,
            "recent_submissions": recent_submissions,
            "pending_submissions": pending_submissions,
            "queue": queue_items,
            "history": history_items,
            "wall": wall_items,
            "tables": tables,
            "failed_submissions": failed_submissions,
            "playback": playback,
            "screen": screen,
            "diagnostics": {
                "bot": self.telegram_bot_status,
                "bot_error": self.telegram_bot_error,
                "video_worker": self.video_worker_status,
                "video_worker_error": self.video_worker_error,
                "screen": screen["status"],
                "ffmpeg": self.settings.media_tools_diagnostics["ffmpeg"]["status"],
                "ffmpeg_path": self.settings.media_tools_diagnostics["ffmpeg"]["path"],
                "ffmpeg_version": self.settings.media_tools_diagnostics["ffmpeg"]["version"],
                "ffprobe": self.settings.media_tools_diagnostics["ffprobe"]["status"],
                "ffprobe_path": self.settings.media_tools_diagnostics["ffprobe"]["path"],
                "ffprobe_version": self.settings.media_tools_diagnostics["ffprobe"]["version"],
                "database": str(self.db_path),
                "free_disk_mb": free_disk_mb,
                "min_free_disk_mb": self.settings.min_free_disk_mb,
                "disk": "unknown" if free_disk_mb is None else ("ok" if free_disk_mb >= self.settings.min_free_disk_mb else "low"),
            },
        }

    def _get_screen_status(self, conn: sqlite3.Connection) -> dict[str, Any]:
        active = self._active_screen_row(conn)
        if active is None:
            return {"status": "not_connected", "screen_id": None, "last_seen_at": None, "message": None, "progress_sec": None, "duration_sec": None, "load_timeout_sec": self.settings.screen_load_timeout_sec}
        if not self._is_screen_fresh(active):
            return {"status": "stale", "screen_id": active["screen_id"], "last_seen_at": active["last_seen_at"], "message": active["error_reason"], "progress_sec": active["progress_sec"], "duration_sec": active["duration_sec"], "load_timeout_sec": self.settings.screen_load_timeout_sec}
        return {"status": active["state"], "screen_id": active["screen_id"], "last_seen_at": active["last_seen_at"], "message": active["error_reason"], "progress_sec": active["progress_sec"], "duration_sec": active["duration_sec"], "load_timeout_sec": self.settings.screen_load_timeout_sec}

    def register_screen(self, screen_id: str | None = None) -> dict[str, Any]:
        screen_id = screen_id or str(uuid4())
        now = utc_now()
        active_event = self.get_active_event()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._handle_stale_active_screen_locked(conn, now)
            active = self._active_screen_row(conn)
            existing = conn.execute("SELECT * FROM playback_sessions WHERE screen_id = ?", (screen_id,)).fetchone()
            if active is None or active["screen_id"] == screen_id:
                state = "active"
                if existing is None:
                    session_id = str(uuid4())
                    conn.execute(
                        """
                        INSERT INTO playback_sessions (id, event_id, screen_id, playback_id, state, last_seen_at, error_reason, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 'active', ?, NULL, ?, ?)
                        """,
                        (session_id, active_event["id"] if active_event else None, screen_id, now, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE playback_sessions SET event_id = ?, state = 'active', last_seen_at = ?, error_reason = NULL, updated_at = ? WHERE screen_id = ?",
                        (active_event["id"] if active_event else None, now, now, screen_id),
                    )
            else:
                state = "secondary"
                if existing is None:
                    session_id = str(uuid4())
                    conn.execute(
                        """
                        INSERT INTO playback_sessions (id, event_id, screen_id, playback_id, state, last_seen_at, error_reason, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 'secondary', ?, NULL, ?, ?)
                        """,
                        (session_id, active_event["id"] if active_event else None, screen_id, now, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE playback_sessions SET event_id = ?, state = 'secondary', last_seen_at = ?, error_reason = NULL, updated_at = ? WHERE screen_id = ?",
                        (active_event["id"] if active_event else None, now, now, screen_id),
                    )
            self._bump_version(conn)
            conn.commit()
        return {"screen_id": screen_id, "status": state, "heartbeat_timeout_sec": self.settings.screen_heartbeat_timeout_sec}

    def heartbeat_screen(self, screen_id: str, state: str = "active", message: str | None = None, progress_sec: float | None = None, duration_sec: float | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._handle_stale_active_screen_locked(conn, now)
            row = conn.execute("SELECT * FROM playback_sessions WHERE screen_id = ?", (screen_id,)).fetchone()
            active = self._active_screen_row(conn)
            if row is None:
                active_event = conn.execute("SELECT id FROM events WHERE active = 1").fetchone()
                status = "active" if active is None else "secondary"
                conn.execute(
                    """
                    INSERT INTO playback_sessions (id, event_id, screen_id, playback_id, state, last_seen_at, error_reason, progress_sec, duration_sec, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), active_event["id"] if active_event else None, screen_id, status, now, message, progress_sec, duration_sec, now, now),
                )
                self._bump_version(conn)
                conn.commit()
                return {"screen_id": screen_id, "status": status, "heartbeat_timeout_sec": self.settings.screen_heartbeat_timeout_sec}
            # A waiting screen must be able to take over after the active one is released.
            status = "active" if active is None or active["screen_id"] == screen_id else "secondary"
            next_state = state if status == "active" and state in {"active", "autoplay_blocked", "loading", "playing", "paused", "error"} else status
            conn.execute(
                "UPDATE playback_sessions SET state = ?, last_seen_at = ?, error_reason = ?, progress_sec = ?, duration_sec = ?, updated_at = ? WHERE screen_id = ?",
                (next_state, now, message, progress_sec, duration_sec, now, screen_id),
            )
            self._bump_version(conn)
            conn.commit()
        return {"screen_id": screen_id, "status": status, "heartbeat_timeout_sec": self.settings.screen_heartbeat_timeout_sec}

    def release_screen(self, screen_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM playback_sessions WHERE screen_id = ?", (screen_id,)).fetchone()
            was_active = row is not None and row["state"] in {"active", "autoplay_blocked", "loading", "playing", "paused", "error"}
            if row is not None:
                conn.execute("UPDATE playback_sessions SET state = 'released', last_seen_at = ?, updated_at = ? WHERE screen_id = ?", (now, now, screen_id))
            if was_active and row["event_id"] is not None:
                self._stop_show_locked(conn, row["event_id"], now)
            self._bump_version(conn)
            conn.commit()
        return {"screen_id": screen_id, "status": "released"}

    def is_active_screen(self, screen_id: str | None) -> bool:
        if not screen_id:
            return False
        with self.connect() as conn:
            row = self._active_screen_row(conn)
        return row is not None and row["screen_id"] == screen_id and self._is_screen_fresh(row)

    def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(uuid4())
        code = secrets.token_urlsafe(6).replace("-", "_")
        now = utc_now()
        company_name = payload["company_name"].strip()
        event_name = payload.get("event_name", "").strip()
        screen_text = payload.get("screen_text", DEFAULT_SCREEN_TEXT).strip() or DEFAULT_SCREEN_TEXT
        with self.connect() as conn:
            existing_count = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            active = 1 if existing_count == 0 else 0
            if active:
                conn.execute("UPDATE events SET active = 0")
            conn.execute(
                """
                INSERT INTO events (
                    id, code, company_name, event_name, screen_text, moderation_mode,
                    reception_open, volume, wall_limit, transition_seconds, theme, table_count,
                    max_duration_sec, max_size_mb, guest_limit, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'manual', 0, 80, 12, 1, 'new_year', 0, 60, 20, 3, ?, ?, ?)
                """,
                (event_id, code, company_name, event_name, screen_text, active, now, now),
            )
            self._bump_version(conn)
            conn.commit()
        self._ensure_event_directories(event_id)
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._with_connection_link(serialize_event(row))

    def get_event_by_code(self, code: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise KeyError(code)
        return self._with_connection_link(serialize_event(row))

    def get_active_event(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE active = 1").fetchone()
        return self._with_connection_link(serialize_event(row)) if row is not None else None

    def activate_event(self, event_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            self._handle_stale_active_screen_locked(conn, now)
            active_event = conn.execute("SELECT id, playback_state FROM events WHERE active = 1").fetchone()
            if active_event is not None and active_event["id"] != event_id and active_event["playback_state"] != "stopped":
                raise ValueError("Stop playback before switching event")
            exists = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
            if exists is None:
                raise KeyError(event_id)
            conn.execute("UPDATE events SET active = 0")
            conn.execute("UPDATE events SET active = 1, updated_at = ? WHERE id = ?", (now, event_id))
            active_screen = self._active_screen_row(conn)
            if active_screen is not None and self._is_screen_fresh(active_screen):
                conn.execute("UPDATE playback_sessions SET event_id = ?, updated_at = ? WHERE id = ?", (event_id, now, active_screen["id"]))
            self._bump_version(conn)
            conn.commit()
        return self.get_event(event_id)

    def update_event(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "company_name",
            "event_name",
            "screen_text",
            "moderation_mode",
            "playback_mode",
            "reception_open",
            "volume",
            "transition_seconds",
            "theme",
            "primary_color",
            "accent_color",
            "background_path",
            "max_duration_sec",
            "max_size_mb",
            "guest_limit",
        }
        updates = {key: value for key, value in payload.items() if key in allowed and value is not None}
        if not updates:
            return self.get_event(event_id)
        if updates.get("moderation_mode") not in (None, "manual", "auto"):
            raise ValueError("moderation_mode must be manual or auto")
        if updates.get("playback_mode") not in (None, "auto_loop", "interactive_wall"):
            raise ValueError("playback_mode must be auto_loop or interactive_wall")
        if updates.get("theme") == "simple_new_year":
            updates["theme"] = "new_year"
        if updates.get("theme") not in (None, "new_year", "neutral"):
            raise ValueError("theme must be new_year or neutral")
        for color_key in ["primary_color", "accent_color"]:
            if color_key in updates and not is_hex_color(str(updates[color_key])):
                raise ValueError(f"{color_key} must be a hex color")
        if "company_name" in updates and not str(updates["company_name"]).strip():
            raise ValueError("company_name is required")
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [normalize_value(value) for value in updates.values()]
        values.append(event_id)
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
            if exists is None:
                raise KeyError(event_id)
            conn.execute(f"UPDATE events SET {assignments} WHERE id = ?", values)
            self._bump_version(conn)
            conn.commit()
        return self.get_event(event_id)

    def configure_event_tables(self, event_id: str, table_count: int, tasks: list[str]) -> list[dict[str, Any]]:
        if table_count < 1 or table_count > 15:
            raise ValueError("table_count must be between 1 and 15")
        if len(tasks) != table_count:
            raise ValueError("A task is required for each configured table")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise KeyError(event_id)
            conn.execute("DELETE FROM event_tables WHERE event_id = ?", (event_id,))
            conn.executemany(
                "INSERT INTO event_tables (event_id, table_number, task_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(event_id, number, task.strip()[:240], now, now) for number, task in enumerate(tasks, start=1)],
            )
            conn.execute("UPDATE events SET table_count = ?, updated_at = ? WHERE id = ?", (table_count, now, event_id))
            self._bump_version(conn)
            conn.commit()
        with self.connect() as conn:
            return self._list_event_tables(conn, event_id)

    def get_event_tables(self, event_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return self._list_event_tables(conn, event_id)

    def update_submission_table(self, submission_id: str, table_number: int) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT event_id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            table = conn.execute(
                "SELECT task_text FROM event_tables WHERE event_id = ? AND table_number = ?",
                (submission["event_id"], table_number),
            ).fetchone()
            if table is None:
                raise ValueError("Table is not configured")
            conn.execute(
                "UPDATE submissions SET table_number = ?, table_task = ?, updated_at = ? WHERE id = ?",
                (table_number, table["task_text"], now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def bind_guest(self, telegram_user_id: int, event_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            event = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            conn.execute(
                """
                INSERT INTO guest_bindings (telegram_user_id, event_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET event_id = excluded.event_id, updated_at = excluded.updated_at
                """,
                (telegram_user_id, event_id, now, now),
            )
            self._bump_version(conn)
            conn.commit()
        binding = self.get_guest_binding(telegram_user_id)
        if binding is None:
            raise RuntimeError("Guest binding was not saved")
        return binding

    def get_guest_binding(self, telegram_user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT telegram_user_id, event_id, created_at, updated_at FROM guest_bindings WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "telegram_user_id": row["telegram_user_id"],
            "event_id": row["event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_telegram_submission(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        submission_id = str(uuid4())
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM submissions WHERE telegram_chat_id = ? AND telegram_message_id = ?",
                (payload["telegram_chat_id"], payload["telegram_message_id"]),
            ).fetchone()
            if existing is not None:
                return serialize_submission(existing), False
            conn.execute(
                """
                INSERT INTO submissions (
                    id, event_id, telegram_chat_id, telegram_message_id, telegram_user_id,
                    guest_display_name, telegram_file_id, telegram_file_unique_id,
                    telegram_file_size, telegram_duration_sec, duration_sec,
                    preparation_status, moderation_status, table_number, table_task, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 'pending', ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    payload["event_id"],
                    payload["telegram_chat_id"],
                    payload["telegram_message_id"],
                    payload["telegram_user_id"],
                    payload.get("guest_display_name", ""),
                    payload["telegram_file_id"],
                    payload.get("telegram_file_unique_id"),
                    payload.get("telegram_file_size"),
                    payload.get("telegram_duration_sec"),
                    payload.get("telegram_duration_sec"),
                    payload.get("table_number"),
                    payload.get("table_task", ""),
                    now,
                    now,
                ),
            )
            self._bump_version(conn)
            conn.commit()
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if row is None:
            raise RuntimeError("Submission was not saved")
        return serialize_submission(row), True

    def create_pending_table_selection(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        selection_id = str(uuid4())
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._delete_expired_table_selections(conn)
            existing = conn.execute(
                "SELECT * FROM submissions WHERE telegram_chat_id = ? AND telegram_message_id = ?",
                (payload["telegram_chat_id"], payload["telegram_message_id"]),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"id": existing["id"], "completed": True}, False
            existing_pending = conn.execute(
                "SELECT * FROM pending_table_selections WHERE telegram_chat_id = ? AND telegram_message_id = ?",
                (payload["telegram_chat_id"], payload["telegram_message_id"]),
            ).fetchone()
            if existing_pending is not None:
                conn.commit()
                return serialize_pending_table_selection(existing_pending), False
            conn.execute(
                """
                INSERT INTO pending_table_selections (
                    id, event_id, telegram_chat_id, telegram_message_id, telegram_user_id,
                    guest_display_name, telegram_file_id, telegram_file_unique_id,
                    telegram_file_size, telegram_duration_sec, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selection_id, payload["event_id"], payload["telegram_chat_id"], payload["telegram_message_id"],
                    payload["telegram_user_id"], payload.get("guest_display_name", ""), payload["telegram_file_id"],
                    payload.get("telegram_file_unique_id"), payload.get("telegram_file_size"),
                    payload.get("telegram_duration_sec"), now, expires_at,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM pending_table_selections WHERE id = ?", (selection_id,)).fetchone()
        if row is None:
            raise RuntimeError("Pending table selection was not saved")
        return serialize_pending_table_selection(row), True

    def confirm_pending_table_selection(self, selection_id: str, telegram_user_id: int, table_number: int) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._delete_expired_table_selections(conn)
            pending = conn.execute("SELECT * FROM pending_table_selections WHERE id = ?", (selection_id,)).fetchone()
            if pending is None:
                raise ValueError("Время выбора стола истекло. Отправьте видео ещё раз.")
            if pending["telegram_user_id"] != telegram_user_id:
                raise ValueError("Этот выбор относится к другому видео.")
            event = conn.execute("SELECT active, reception_open, guest_limit FROM events WHERE id = ?", (pending["event_id"],)).fetchone()
            if event is None or not event["active"] or not event["reception_open"]:
                raise ValueError("Приём для мероприятия завершён")
            if not self.has_min_free_disk():
                raise ValueError("Недостаточно свободного места. Отправьте видео позже.")
            table = conn.execute(
                "SELECT task_text FROM event_tables WHERE event_id = ? AND table_number = ?",
                (pending["event_id"], table_number),
            ).fetchone()
            if table is None:
                raise ValueError("Стол не настроен")
            accepted_count = conn.execute(
                "SELECT COUNT(*) AS count FROM submissions WHERE event_id = ? AND telegram_user_id = ? AND preparation_status != 'failed'",
                (pending["event_id"], telegram_user_id),
            ).fetchone()["count"]
            if accepted_count >= event["guest_limit"]:
                raise ValueError("Лимит поздравлений для этого мероприятия уже исчерпан.")
            submission_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO submissions (
                    id, event_id, telegram_chat_id, telegram_message_id, telegram_user_id,
                    guest_display_name, telegram_file_id, telegram_file_unique_id,
                    telegram_file_size, telegram_duration_sec, duration_sec,
                    preparation_status, moderation_status, table_number, table_task, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 'pending', ?, ?, ?, ?)
                """,
                (
                    submission_id, pending["event_id"], pending["telegram_chat_id"], pending["telegram_message_id"],
                    pending["telegram_user_id"], pending["guest_display_name"], pending["telegram_file_id"],
                    pending["telegram_file_unique_id"], pending["telegram_file_size"], pending["telegram_duration_sec"],
                    pending["telegram_duration_sec"], table_number, table["task_text"], now, now,
                ),
            )
            conn.execute("DELETE FROM pending_table_selections WHERE id = ?", (selection_id,))
            self._bump_version(conn)
            conn.commit()
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if row is None:
            raise RuntimeError("Submission was not saved")
        return serialize_submission(row)

    def get_pending_table_selection(self, selection_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            self._delete_expired_table_selections(conn)
            row = conn.execute("SELECT * FROM pending_table_selections WHERE id = ?", (selection_id,)).fetchone()
            conn.commit()
        return serialize_pending_table_selection(row) if row is not None else None

    def get_pending_table_selection_by_message(self, telegram_chat_id: int, telegram_message_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            self._delete_expired_table_selections(conn)
            row = conn.execute(
                "SELECT * FROM pending_table_selections WHERE telegram_chat_id = ? AND telegram_message_id = ?",
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
            conn.commit()
        return serialize_pending_table_selection(row) if row is not None else None

    def _delete_expired_table_selections(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM pending_table_selections WHERE expires_at <= ?", (utc_now(),))

    def get_submission_by_telegram_message(self, telegram_chat_id: int, telegram_message_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE telegram_chat_id = ? AND telegram_message_id = ?",
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
        return serialize_submission(row) if row is not None else None

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if row is None:
            raise KeyError(submission_id)
        return serialize_submission(row)

    def claim_next_submission_for_processing(self) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM submissions
                WHERE preparation_status = 'received'
                  AND telegram_file_id IS NOT NULL
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE submissions
                SET preparation_status = 'downloading', processing_attempts = processing_attempts + 1,
                    processing_locked_at = ?, error_reason = NULL, updated_at = ?
                WHERE id = ? AND preparation_status = 'received'
                """,
                (now, now, row["id"]),
            )
            self._bump_version(conn)
            conn.commit()
            claimed = conn.execute("SELECT * FROM submissions WHERE id = ?", (row["id"],)).fetchone()
        return serialize_submission(claimed) if claimed is not None else None

    def mark_submission_processing(self, submission_id: str) -> dict[str, Any]:
        return self._update_submission_status(submission_id, "processing")

    def mark_submission_ready(
        self,
        submission_id: str,
        *,
        original_path: str,
        playback_path: str,
        thumbnail_path: str,
        duration_sec: float,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET preparation_status = 'ready', original_path = ?, playback_path = ?, thumbnail_path = ?,
                    duration_sec = ?, error_reason = NULL, processing_locked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (original_path, playback_path, thumbnail_path, duration_sec, now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def mark_submission_failed(self, submission_id: str, reason: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET preparation_status = 'failed', error_reason = ?, processing_locked_at = NULL, updated_at = ?
                WHERE id = ? AND preparation_status != 'ready'
                """,
                (reason[:500], now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def retry_submission(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if exists is None:
                raise KeyError(submission_id)
            conn.execute(
                """
                UPDATE submissions
                SET preparation_status = 'received', error_reason = NULL, processing_locked_at = NULL, updated_at = ?
                WHERE id = ? AND preparation_status = 'failed'
                """,
                (now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def _update_submission_status(self, submission_id: str, status: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE submissions SET preparation_status = ?, processing_locked_at = ?, updated_at = ? WHERE id = ? AND preparation_status != 'ready'",
                (status, now, now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def count_guest_submissions(self, event_id: str, telegram_user_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM submissions
                WHERE event_id = ?
                  AND telegram_user_id = ?
                  AND preparation_status != 'failed'
                """,
                (event_id, telegram_user_id),
            ).fetchone()
        return row["count"]

    def approve_submission(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            if submission["preparation_status"] != "ready":
                raise ValueError("Only ready submissions can be approved")
            conn.execute(
                "UPDATE submissions SET moderation_status = 'approved', updated_at = ? WHERE id = ?",
                (now, submission_id),
            )
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def reject_submission(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            conn.execute("UPDATE submissions SET moderation_status = 'rejected', updated_at = ? WHERE id = ?", (now, submission_id))
            conn.execute(
                """
                UPDATE queue_items
                SET status = 'cancelled', hidden_from_history = 1, updated_at = ?
                WHERE submission_id = ? AND status = 'queued'
                """,
                (now, submission_id),
            )
            self._normalize_queue_positions(conn, submission["event_id"])
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def approve_all_pending(self, event_id: str) -> int:
        now = utc_now()
        approved = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE event_id = ? AND preparation_status = 'ready' AND moderation_status = 'pending'
                ORDER BY created_at ASC
                """,
                (event_id,),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE submissions SET moderation_status = 'approved', updated_at = ? WHERE id = ?", (now, row["id"]))
                approved += 1
            if approved:
                self._bump_version(conn)
            conn.commit()
        return approved

    def update_submission_display_name(self, submission_id: str, name: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if exists is None:
                raise KeyError(submission_id)
            conn.execute("UPDATE submissions SET guest_display_name = ?, updated_at = ? WHERE id = ?", (name.strip(), now, submission_id))
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def delete_submission(self, submission_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            playing = conn.execute("SELECT id FROM queue_items WHERE submission_id = ? AND status = 'playing'", (submission_id,)).fetchone()
            if playing is not None:
                raise ValueError("Нельзя удалить кружочек, который сейчас воспроизводится")
            affected_event_ids = {submission["event_id"]}
            affected_event_ids.update(
                row["event_id"]
                for row in conn.execute("SELECT DISTINCT event_id FROM queue_items WHERE submission_id = ?", (submission_id,)).fetchall()
            )
            affected_event_ids.update(
                row["event_id"]
                for row in conn.execute("SELECT DISTINCT event_id FROM wall_items WHERE submission_id = ?", (submission_id,)).fetchall()
            )
            file_paths = [submission["original_path"], submission["playback_path"], submission["thumbnail_path"]]
            conn.execute("DELETE FROM wall_items WHERE submission_id = ?", (submission_id,))
            conn.execute("DELETE FROM queue_items WHERE submission_id = ?", (submission_id,))
            conn.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
            for event_id in affected_event_ids:
                self._normalize_queue_positions(conn, event_id)
                self._normalize_wall_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        self._delete_submission_files(file_paths)
        return {"deleted": submission_id}

    def _delete_submission_files(self, file_paths: list[str | None]) -> None:
        data_root = self.data_dir.resolve()
        for relative_path in file_paths:
            if not relative_path:
                continue
            target = (self.data_dir / relative_path).resolve()
            if data_root != target and data_root not in target.parents:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    def set_submission_wall_visibility(self, submission_id: str, visible: bool) -> dict[str, Any]:
        return self.add_submission_to_wall(submission_id) if visible else self.remove_submission_from_wall(submission_id)

    def add_submission_to_wall(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            if submission["preparation_status"] != "ready" or submission["moderation_status"] != "approved":
                raise ValueError("Only ready and approved submissions can be added to wall")
            event = conn.execute("SELECT playback_mode, playback_state FROM events WHERE id = ?", (submission["event_id"],)).fetchone()
            if event is None:
                raise KeyError(submission["event_id"])
            existing = conn.execute("SELECT id FROM wall_items WHERE submission_id = ?", (submission_id,)).fetchone()
            if existing is None:
                count = conn.execute("SELECT COUNT(*) AS count FROM wall_items WHERE event_id = ?", (submission["event_id"],)).fetchone()["count"]
                if count >= 12:
                    raise ValueError("Wall layout is full")
                position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS position FROM wall_items WHERE event_id = ?", (submission["event_id"],)).fetchone()["position"]
                conn.execute(
                    "INSERT INTO wall_items (id, event_id, submission_id, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), submission["event_id"], submission_id, position, now, now),
                )
            conn.execute("UPDATE submissions SET hidden_from_wall = 0, updated_at = ? WHERE id = ?", (now, submission_id))
            if event["playback_mode"] == "auto_loop" and event["playback_state"] == "running":
                self._enqueue_submission_if_allowed(conn, submission, now, allow_terminal_previous=True, is_loop=True)
            self._continue_if_show_waiting(conn, submission["event_id"], now)
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def remove_submission_from_wall(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            conn.execute("DELETE FROM wall_items WHERE submission_id = ?", (submission_id,))
            conn.execute("UPDATE submissions SET hidden_from_wall = 1, updated_at = ? WHERE id = ?", (now, submission_id))
            conn.execute(
                "UPDATE queue_items SET status = 'cancelled', hidden_from_history = 1, updated_at = ? WHERE submission_id = ? AND status = 'queued' AND is_loop = 1",
                (now, submission_id),
            )
            self._normalize_wall_positions(conn, submission["event_id"])
            self._bump_version(conn)
            conn.commit()
        return self.get_submission(submission_id)

    def replay_submission(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            submission = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if submission is None:
                raise KeyError(submission_id)
            if submission["preparation_status"] != "ready" or submission["moderation_status"] != "approved":
                raise ValueError("Only ready and approved submissions can be replayed")
            item = self._enqueue_submission_if_allowed(conn, submission, now, allow_terminal_previous=True)
            if item is None:
                raise ValueError("Submission already has queued or playing item")
            self._continue_if_show_waiting(conn, submission["event_id"], now)
            self._bump_version(conn)
            conn.commit()
        return self.get_queue_item(item)

    def cancel_queue_item(self, queue_item_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM queue_items WHERE id = ?", (queue_item_id,)).fetchone()
            if item is None:
                raise KeyError(queue_item_id)
            if item["status"] != "queued":
                raise ValueError("Only queued items can be cancelled")
            conn.execute("UPDATE queue_items SET status = 'cancelled', hidden_from_history = 1, updated_at = ? WHERE id = ?", (now, queue_item_id))
            self._normalize_queue_positions(conn, item["event_id"])
            self._bump_version(conn)
            conn.commit()
        return self.get_queue_item(queue_item_id)

    def reorder_queue(self, event_id: str, queue_item_ids: list[str]) -> list[dict[str, Any]]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = [row["id"] for row in conn.execute(
                "SELECT id FROM queue_items WHERE event_id = ? AND status = 'queued' ORDER BY position ASC, created_at ASC",
                (event_id,),
            ).fetchall()]
            if set(existing) != set(queue_item_ids):
                raise ValueError("Queue order must include all queued items exactly once")
            for position, item_id in enumerate(queue_item_ids, start=1):
                conn.execute("UPDATE queue_items SET position = ?, updated_at = ? WHERE id = ?", (position, now, item_id))
            self._bump_version(conn)
            conn.commit()
        with self.connect() as conn:
            return self._list_queue_items(conn, event_id)

    def get_queue_item(self, queue_item_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(QUEUE_SELECT + " WHERE qi.id = ?", (queue_item_id,)).fetchone()
        if row is None:
            raise KeyError(queue_item_id)
        return serialize_queue_item(row)

    def play_queue_item_now(self, queue_item_id: str, screen_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute(
                """
                SELECT qi.*, s.preparation_status, s.moderation_status, s.playback_path, e.playback_mode
                FROM queue_items qi
                JOIN submissions s ON s.id = qi.submission_id
                JOIN events e ON e.id = qi.event_id
                WHERE qi.id = ?
                """,
                (queue_item_id,),
            ).fetchone()
            if item is None:
                raise KeyError(queue_item_id)
            if item["playback_mode"] != "interactive_wall":
                raise ValueError("Queue item can be clicked only in interactive wall mode")
            if screen_id is not None:
                active = self._active_screen_row(conn)
                if active is None or active["screen_id"] != screen_id or not self._is_screen_fresh(active):
                    raise ValueError("Only active screen can start wall item")
            if item["status"] != "queued":
                raise ValueError("Only queued item can be started from wall")
            if item["preparation_status"] != "ready" or item["moderation_status"] != "approved" or not item["playback_path"]:
                raise ValueError("Queue item is not ready for playback")
            playing = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (item["event_id"],)).fetchone()
            if playing is not None:
                raise ValueError("Another item is already playing")
            conn.execute(
                "UPDATE queue_items SET status = 'playing', playback_id = ?, started_at = ?, updated_at = ? WHERE id = ?",
                (str(uuid4()), now, now, queue_item_id),
            )
            conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, item["event_id"]))
            self._normalize_queue_positions(conn, item["event_id"])
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(item["event_id"])

    def play_wall_submission_now(self, submission_id: str, screen_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT s.*, e.playback_mode
                FROM submissions s
                JOIN wall_items wi ON wi.submission_id = s.id
                JOIN events e ON e.id = s.event_id
                WHERE s.id = ?
                """,
                (submission_id,),
            ).fetchone()
            if row is None:
                raise KeyError(submission_id)
            if row["playback_mode"] != "interactive_wall":
                raise ValueError("Wall item can be clicked only in interactive wall mode")
            if screen_id is not None:
                active = self._active_screen_row(conn)
                if active is None or active["screen_id"] != screen_id or not self._is_screen_fresh(active):
                    raise ValueError("Only active screen can start wall item")
            if row["preparation_status"] != "ready" or row["moderation_status"] != "approved" or not row["playback_path"]:
                raise ValueError("Wall item is not ready for playback")
            playing = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (row["event_id"],)).fetchone()
            if playing is not None:
                raise ValueError("Another item is already playing")
            item_id = str(uuid4())
            conn.execute(
                "INSERT INTO queue_items (id, event_id, submission_id, position, status, playback_id, started_at, is_loop, hidden_from_history, created_at, updated_at) VALUES (?, ?, ?, 0, 'playing', ?, ?, 1, 1, ?, ?)",
                (item_id, row["event_id"], submission_id, str(uuid4()), now, now, now),
            )
            conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, row["event_id"]))
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(row["event_id"])

    def play_wall_submission_from_admin(self, submission_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._handle_stale_active_screen_locked(conn, now)
            row = conn.execute(
                """
                SELECT s.*, e.playback_mode
                FROM submissions s
                JOIN wall_items wi ON wi.submission_id = s.id
                JOIN events e ON e.id = s.event_id
                WHERE s.id = ?
                """,
                (submission_id,),
            ).fetchone()
            if row is None:
                raise KeyError(submission_id)
            active_screen = self._active_screen_row(conn)
            if active_screen is None or not self._is_screen_fresh(active_screen):
                raise ValueError("Сначала откройте экран показа")
            if active_screen["event_id"] != row["event_id"]:
                raise ValueError("Активный экран подключён к другому мероприятию")
            if row["preparation_status"] != "ready" or row["moderation_status"] != "approved" or not row["playback_path"]:
                raise ValueError("Ролик не готов к показу")
            playing = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (row["event_id"],)).fetchone()
            if playing is not None:
                raise ValueError("Сначала остановите или пропустите текущий ролик")
            item_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO queue_items (id, event_id, submission_id, position, status, playback_id, started_at, is_loop, hidden_from_history, created_at, updated_at)
                VALUES (?, ?, ?, 0, 'playing', ?, ?, 0, 0, ?, ?)
                """,
                (item_id, row["event_id"], submission_id, str(uuid4()), now, now, now),
            )
            conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, row["event_id"]))
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(row["event_id"])

    def start_playback(self, event_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute("SELECT id, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            current = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if event["playback_mode"] == "interactive_wall" and current is None:
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            else:
                self._start_next_or_cycle_item(conn, event_id, now)
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def pause_playback(self, event_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise KeyError(event_id)
            current = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if current is not None:
                conn.execute("UPDATE events SET playback_state = 'paused', updated_at = ? WHERE id = ?", (now, event_id))
                self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def resume_playback(self, event_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise KeyError(event_id)
            current = conn.execute("SELECT id FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if current is not None:
                conn.execute("UPDATE events SET playback_state = 'running', updated_at = ? WHERE id = ?", (now, event_id))
            else:
                self._start_next_or_cycle_item(conn, event_id, now)
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def complete_current(self, event_id: str, playback_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if item is None or item["playback_id"] != playback_id:
                conn.commit()
                return self.get_playback_state(event_id)
            event = conn.execute("SELECT playback_state, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            conn.execute(
                "UPDATE queue_items SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, item["id"]),
            )
            if event["playback_mode"] == "interactive_wall":
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            elif event["playback_state"] != "stopped":
                if self._is_submission_eligible_for_auto_loop(conn, item["event_id"], item["submission_id"]):
                    self._enqueue_submission_if_allowed(conn, item, now, allow_terminal_previous=True, is_loop=True)
                self._start_next_or_cycle_item(conn, event_id, now)
            else:
                if self._is_submission_eligible_for_auto_loop(conn, item["event_id"], item["submission_id"]):
                    self._enqueue_submission_if_allowed(conn, item, now, allow_terminal_previous=True, is_loop=True)
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def fail_current(self, event_id: str, playback_id: str, reason: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if item is None or item["playback_id"] != playback_id:
                conn.commit()
                return self.get_playback_state(event_id)
            event = conn.execute("SELECT playback_state, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            conn.execute(
                "UPDATE queue_items SET status = 'failed', error_reason = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (reason[:500], now, now, item["id"]),
            )
            if event["playback_mode"] == "interactive_wall":
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            elif event["playback_state"] != "stopped":
                self._start_next_or_cycle_item(conn, event_id, now)
            else:
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def handle_playback_event(self, event_id: str, playback_id: str, event_type: str, reason: str | None = None, screen_id: str | None = None) -> dict[str, Any]:
        if screen_id is not None and not self.is_active_screen(screen_id):
            return self.get_playback_state(event_id)
        if event_type == "completed":
            return self.complete_current(event_id, playback_id)
        if event_type == "error":
            return self.fail_current(event_id, playback_id, reason or "Playback error")
        if event_type in {"started", "paused"}:
            return self.get_playback_state(event_id)
        raise ValueError("Unsupported playback event")

    def skip_current(self, event_id: str, playback_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute("SELECT playback_state, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            item = conn.execute("SELECT * FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if item is not None and playback_id is not None and item["playback_id"] != playback_id:
                conn.commit()
                return self.get_playback_state(event_id)
            if item is not None:
                conn.execute("UPDATE queue_items SET status = 'skipped', updated_at = ? WHERE id = ?", (now, item["id"]))
            if event["playback_mode"] == "interactive_wall":
                conn.execute("UPDATE events SET playback_state = 'idle', updated_at = ? WHERE id = ?", (now, event_id))
            elif event["playback_state"] != "stopped":
                self._start_next_or_cycle_item(conn, event_id, now)
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def stop_show(self, event_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute("SELECT id, playback_mode FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            item = conn.execute("SELECT * FROM queue_items WHERE event_id = ? AND status = 'playing'", (event_id,)).fetchone()
            if item is not None:
                if event["playback_mode"] == "interactive_wall":
                    conn.execute("UPDATE queue_items SET status = 'cancelled', playback_id = NULL, hidden_from_history = 1, updated_at = ? WHERE id = ?", (now, item["id"]))
                else:
                    position = conn.execute(
                        "SELECT COALESCE(MIN(position), 0) AS position FROM queue_items WHERE event_id = ? AND status = 'queued'",
                        (event_id,),
                    ).fetchone()["position"] - 1
                    conn.execute(
                        "UPDATE queue_items SET status = 'queued', playback_id = NULL, position = ?, updated_at = ? WHERE id = ?",
                        (position, now, item["id"]),
                    )
            conn.execute("UPDATE events SET playback_state = 'stopped', updated_at = ? WHERE id = ?", (now, event_id))
            self._normalize_queue_positions(conn, event_id)
            self._bump_version(conn)
            conn.commit()
        return self.get_playback_state(event_id)

    def get_playback_state(self, event_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            event = conn.execute("SELECT playback_state FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            queue = self._list_queue_items(conn, event_id)
        return {"state": event["playback_state"], "current": next((item for item in queue if item["status"] == "playing"), None)}

    def clear_history(self, event_id: str) -> int:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise KeyError(event_id)
            cursor = conn.execute(
                """
                UPDATE queue_items
                SET hidden_from_history = 1, updated_at = ?
                WHERE event_id = ? AND status IN ('completed', 'skipped', 'failed', 'cancelled') AND hidden_from_history = 0
                """,
                (now, event_id),
            )
            changed = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0
            if changed:
                self._bump_version(conn)
            conn.commit()
        return changed

    def _enqueue_submission_if_allowed(self, conn: sqlite3.Connection, submission: sqlite3.Row, now: str, allow_terminal_previous: bool = False, is_loop: bool = False, position: int | None = None) -> str | None:
        submission_id = submission["submission_id"] if "submission_id" in submission.keys() else submission["id"]
        event_id = submission["event_id"]
        active = conn.execute(
            "SELECT id FROM queue_items WHERE submission_id = ? AND status IN ('queued', 'playing') LIMIT 1",
            (submission_id,),
        ).fetchone()
        if active is not None:
            return None
        if not allow_terminal_previous:
            existing = conn.execute("SELECT id FROM queue_items WHERE submission_id = ? LIMIT 1", (submission_id,)).fetchone()
            if existing is not None:
                return None
        if position is not None:
            conn.execute(
                "UPDATE queue_items SET position = position + 1, updated_at = ? WHERE event_id = ? AND status = 'queued' AND position >= ?",
                (now, event_id, position),
            )
        elif is_loop:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM queue_items WHERE event_id = ? AND status = 'queued'",
                (event_id,),
            ).fetchone()["position"]
        else:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM queue_items WHERE event_id = ? AND status = 'queued' AND is_loop = 0",
                (event_id,),
            ).fetchone()["position"]
            conn.execute(
                "UPDATE queue_items SET position = position + 1, updated_at = ? WHERE event_id = ? AND status = 'queued' AND is_loop = 1 AND position >= ?",
                (now, event_id, position),
            )
        item_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO queue_items (id, event_id, submission_id, position, status, is_loop, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (item_id, event_id, submission_id, position, 1 if is_loop else 0, now, now),
        )
        return item_id

    def _is_submission_eligible_for_auto_loop(self, conn: sqlite3.Connection, event_id: str, submission_id: str) -> bool:
        return conn.execute(
            """
            SELECT 1
            FROM submissions s
            JOIN wall_items wi ON wi.submission_id = s.id
            WHERE s.id = ?
              AND s.event_id = ?
              AND s.preparation_status = 'ready'
              AND s.moderation_status = 'approved'
              AND s.playback_path IS NOT NULL
            """,
            (submission_id, event_id),
        ).fetchone() is not None

    def _normalize_queue_positions(self, conn: sqlite3.Connection, event_id: str) -> None:
        rows = conn.execute(
            "SELECT id FROM queue_items WHERE event_id = ? AND status = 'queued' ORDER BY position ASC, created_at ASC",
            (event_id,),
        ).fetchall()
        for position, row in enumerate(rows, start=1):
            conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (position, row["id"]))

    def _normalize_wall_positions(self, conn: sqlite3.Connection, event_id: str) -> None:
        rows = conn.execute(
            "SELECT id FROM wall_items WHERE event_id = ? ORDER BY position ASC, created_at ASC",
            (event_id,),
        ).fetchall()
        for position, row in enumerate(rows, start=1):
            conn.execute("UPDATE wall_items SET position = ? WHERE id = ?", (position, row["id"]))

    def _normalize_all_queue_positions(self, conn: sqlite3.Connection) -> None:
        event_ids = [row["event_id"] for row in conn.execute("SELECT DISTINCT event_id FROM queue_items").fetchall()]
        for event_id in event_ids:
            self._normalize_queue_positions(conn, event_id)

    def _list_pending_submissions(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        return [
            serialize_submission(row)
            for row in conn.execute(
                """
                SELECT * FROM submissions
                WHERE event_id = ? AND preparation_status = 'ready' AND moderation_status = 'pending'
                ORDER BY created_at ASC
                """,
                (event_id,),
            ).fetchall()
        ]

    def _list_queue_items(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        return [
            serialize_queue_item(row)
            for row in conn.execute(
                QUEUE_SELECT + " WHERE qi.event_id = ? AND qi.status IN ('queued', 'playing') ORDER BY qi.status = 'playing' DESC, qi.position ASC, qi.created_at ASC",
                (event_id,),
            ).fetchall()
        ]

    def _list_history_items(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        return [
            serialize_queue_item(row)
            for row in conn.execute(
                QUEUE_SELECT + " WHERE qi.event_id = ? AND qi.status IN ('completed', 'skipped', 'failed', 'cancelled') AND qi.hidden_from_history = 0 ORDER BY qi.updated_at DESC LIMIT 80",
                (event_id,),
            ).fetchall()
        ]

    def _list_wall_items(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT s.*, wi.position AS wall_position, wi.created_at AS wall_created_at, wi.updated_at AS wall_updated_at
            FROM submissions s
            JOIN wall_items wi ON wi.submission_id = s.id
            WHERE s.event_id = ?
              AND s.preparation_status = 'ready'
              AND s.moderation_status = 'approved'
            ORDER BY wi.position ASC, wi.created_at ASC
            LIMIT ?
            """,
            (event_id, 12),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = serialize_submission(row)
            item["wall_position"] = row["wall_position"]
            item["wall_completed_at"] = row["wall_created_at"]
            items.append(item)
        return items

    def _list_event_tables(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        return [
            {"number": row["table_number"], "task": row["task_text"]}
            for row in conn.execute(
                "SELECT table_number, task_text FROM event_tables WHERE event_id = ? ORDER BY table_number ASC",
                (event_id,),
            ).fetchall()
        ]

    def _list_failed_submissions(self, conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
        return [
            serialize_submission(row)
            for row in conn.execute(
                """
                SELECT * FROM submissions
                WHERE event_id = ? AND preparation_status = 'failed'
                ORDER BY updated_at DESC LIMIT 50
                """,
                (event_id,),
            ).fetchall()
        ]

    def _bump_version(self, conn: sqlite3.Connection) -> int:
        now = utc_now()
        conn.execute("UPDATE app_state SET version = version + 1, updated_at = ? WHERE id = 1", (now,))
        return conn.execute("SELECT version FROM app_state WHERE id = 1").fetchone()["version"]

    def _ensure_event_directories(self, event_id: str) -> None:
        for name in ["originals", "playback", "thumbnails", "background"]:
            (self.data_dir / "events" / event_id / name).mkdir(parents=True, exist_ok=True)

    def _with_connection_link(self, event: dict[str, Any]) -> dict[str, Any]:
        username = self.settings.telegram_bot_username
        event["connection_link"] = f"https://t.me/{username}?start={event['code']}" if username else None
        return event


def normalize_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def is_hex_color(value: str) -> bool:
    if len(value) != 7 or not value.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value[1:])


def serialize_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "company_name": row["company_name"],
        "event_name": row["event_name"],
        "screen_text": row["screen_text"],
        "moderation_mode": row["moderation_mode"],
        "reception_open": bool(row["reception_open"]),
        "volume": row["volume"],
        "transition_seconds": row["transition_seconds"],
        "theme": "new_year" if row["theme"] == "simple_new_year" else row["theme"],
        "primary_color": row["primary_color"],
        "accent_color": row["accent_color"],
        "background_path": row["background_path"],
        "max_duration_sec": row["max_duration_sec"],
        "max_size_mb": row["max_size_mb"],
        "guest_limit": row["guest_limit"],
        "table_count": row["table_count"],
        "playback_state": row["playback_state"],
        "playback_mode": row["playback_mode"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def serialize_queue_item(row: sqlite3.Row) -> dict[str, Any]:
    submission = {
        "id": row["submission_id"],
        "event_id": row["event_id"],
        "telegram_chat_id": row["telegram_chat_id"],
        "telegram_message_id": row["telegram_message_id"],
        "telegram_user_id": row["telegram_user_id"],
        "guest_display_name": row["guest_display_name"],
        "table_number": row["table_number"],
        "table_task": row["table_task"],
        "telegram_file_id": row["telegram_file_id"],
        "telegram_file_unique_id": row["telegram_file_unique_id"],
        "telegram_file_size": row["telegram_file_size"],
        "telegram_duration_sec": row["telegram_duration_sec"],
        "duration_sec": row["duration_sec"],
        "preparation_status": row["preparation_status"],
        "moderation_status": row["moderation_status"],
        "original_path": row["original_path"],
        "playback_path": row["playback_path"],
        "thumbnail_path": row["thumbnail_path"],
        "error_reason": row["error_reason"],
        "processing_attempts": row["processing_attempts"],
        "processing_locked_at": row["processing_locked_at"],
        "hidden_from_wall": bool(row["hidden_from_wall"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "submission_id": row["submission_id"],
        "position": row["position"],
        "status": row["status"],
        "playback_id": row["playback_id"],
        "is_loop": bool(row["is_loop"]),
        "hidden_from_history": bool(row["hidden_from_history"]),
        "error_reason": row["queue_error_reason"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "created_at": row["queue_created_at"],
        "updated_at": row["queue_updated_at"],
        "submission": submission,
    }


QUEUE_SELECT = """
SELECT
    qi.id,
    qi.event_id,
    qi.submission_id,
    qi.position,
    qi.status,
    qi.playback_id,
    qi.is_loop,
    qi.hidden_from_history,
    qi.error_reason AS queue_error_reason,
    qi.started_at,
    qi.completed_at,
    qi.created_at AS queue_created_at,
    qi.updated_at AS queue_updated_at,
    s.telegram_chat_id,
    s.telegram_message_id,
    s.telegram_user_id,
    s.guest_display_name,
    s.table_number,
    s.table_task,
    s.telegram_file_id,
    s.telegram_file_unique_id,
    s.telegram_file_size,
    s.telegram_duration_sec,
    s.duration_sec,
    s.preparation_status,
    s.moderation_status,
    s.original_path,
    s.playback_path,
    s.thumbnail_path,
    s.error_reason,
    s.processing_attempts,
    s.processing_locked_at,
    s.hidden_from_wall,
    s.created_at,
    s.updated_at
FROM queue_items qi
JOIN submissions s ON s.id = qi.submission_id
"""


def serialize_submission(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "telegram_chat_id": row["telegram_chat_id"],
        "telegram_message_id": row["telegram_message_id"],
        "telegram_user_id": row["telegram_user_id"],
        "guest_display_name": row["guest_display_name"],
        "table_number": row["table_number"],
        "table_task": row["table_task"],
        "telegram_file_id": row["telegram_file_id"],
        "telegram_file_unique_id": row["telegram_file_unique_id"],
        "telegram_file_size": row["telegram_file_size"],
        "telegram_duration_sec": row["telegram_duration_sec"],
        "duration_sec": row["duration_sec"],
        "preparation_status": row["preparation_status"],
        "moderation_status": row["moderation_status"],
        "original_path": row["original_path"],
        "playback_path": row["playback_path"],
        "thumbnail_path": row["thumbnail_path"],
        "error_reason": row["error_reason"],
        "processing_attempts": row["processing_attempts"],
        "processing_locked_at": row["processing_locked_at"],
        "hidden_from_wall": bool(row["hidden_from_wall"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def serialize_pending_table_selection(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "telegram_chat_id": row["telegram_chat_id"],
        "telegram_message_id": row["telegram_message_id"],
        "telegram_user_id": row["telegram_user_id"],
        "expires_at": row["expires_at"],
        "completed": False,
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL,
    event_name TEXT NOT NULL DEFAULT '',
    screen_text TEXT NOT NULL,
    moderation_mode TEXT NOT NULL CHECK (moderation_mode IN ('manual', 'auto')),
    reception_open INTEGER NOT NULL DEFAULT 0 CHECK (reception_open IN (0, 1)),
    volume INTEGER NOT NULL DEFAULT 80 CHECK (volume BETWEEN 0 AND 100),
    wall_limit INTEGER NOT NULL DEFAULT 12 CHECK (wall_limit = 12),
    transition_seconds INTEGER NOT NULL DEFAULT 1 CHECK (transition_seconds >= 0),
    theme TEXT NOT NULL DEFAULT 'new_year',
    max_duration_sec INTEGER NOT NULL DEFAULT 60 CHECK (max_duration_sec > 0),
    max_size_mb INTEGER NOT NULL DEFAULT 20 CHECK (max_size_mb > 0),
    guest_limit INTEGER NOT NULL DEFAULT 3 CHECK (guest_limit > 0),
    background_path TEXT,
    primary_color TEXT NOT NULL DEFAULT '#102033',
    accent_color TEXT NOT NULL DEFAULT '#0f5f5c',
    playback_state TEXT NOT NULL DEFAULT 'stopped',
    playback_mode TEXT NOT NULL DEFAULT 'auto_loop' CHECK (playback_mode IN ('auto_loop', 'interactive_wall')),
    table_count INTEGER NOT NULL DEFAULT 0 CHECK (table_count BETWEEN 0 AND 15),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_event ON events(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS guest_bindings (
    telegram_user_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    telegram_chat_id INTEGER,
    telegram_message_id INTEGER,
    telegram_user_id INTEGER,
    guest_display_name TEXT NOT NULL DEFAULT '',
    telegram_file_id TEXT,
    telegram_file_unique_id TEXT,
    telegram_file_size INTEGER,
    telegram_duration_sec INTEGER,
    table_number INTEGER,
    table_task TEXT NOT NULL DEFAULT '',
    processing_attempts INTEGER NOT NULL DEFAULT 0,
    processing_locked_at TEXT,
    original_path TEXT,
    playback_path TEXT,
    thumbnail_path TEXT,
    duration_sec REAL,
    preparation_status TEXT NOT NULL CHECK (preparation_status IN ('received', 'downloading', 'processing', 'ready', 'failed')),
    moderation_status TEXT NOT NULL CHECK (moderation_status IN ('pending', 'approved', 'rejected')),
    error_reason TEXT,
    hidden_from_wall INTEGER NOT NULL DEFAULT 0 CHECK (hidden_from_wall IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (telegram_chat_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS event_tables (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    table_number INTEGER NOT NULL CHECK (table_number BETWEEN 1 AND 15),
    task_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, table_number)
);

CREATE TABLE IF NOT EXISTS pending_table_selections (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    telegram_chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    guest_display_name TEXT NOT NULL DEFAULT '',
    telegram_file_id TEXT NOT NULL,
    telegram_file_unique_id TEXT,
    telegram_file_size INTEGER,
    telegram_duration_sec INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (telegram_chat_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'playing', 'completed', 'skipped', 'failed', 'cancelled')),
    playback_id TEXT,
    error_reason TEXT,
    started_at TEXT,
    completed_at TEXT,
    is_loop INTEGER NOT NULL DEFAULT 0 CHECK (is_loop IN (0, 1)),
    hidden_from_history INTEGER NOT NULL DEFAULT 0 CHECK (hidden_from_history IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_event_status_position ON queue_items(event_id, status, position);

CREATE TABLE IF NOT EXISTS wall_items (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, submission_id)
);

CREATE INDEX IF NOT EXISTS idx_wall_items_event_position ON wall_items(event_id, position);

CREATE TABLE IF NOT EXISTS playback_sessions (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    screen_id TEXT,
    playback_id TEXT,
    state TEXT NOT NULL DEFAULT 'stopped',
    last_seen_at TEXT,
    error_reason TEXT,
    progress_sec REAL,
    duration_sec REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_entries (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);
"""
