import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GroupSettings:
    dry_run: bool
    join_mute_seconds: int = 0
    join_mute_duration: int = 14400


@dataclass(frozen=True)
class BanLogEntry:
    update_id: int
    created_at: str
    user_id: int
    message_id: int
    action: str
    decision: dict | None


class Store:
    """Persist settings, group directory, selections, and audits; never message/image content."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER);
            CREATE TABLE IF NOT EXISTS audit (
                update_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                decision TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_ban_log
                ON audit (chat_id, created_at DESC, update_id DESC)
                WHERE action IN ('banned', 'would_ban');
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                dry_run INTEGER CHECK(dry_run IN (0, 1)),
                join_mute_seconds INTEGER NOT NULL DEFAULT 0
                    CHECK(join_mute_seconds = 0 OR join_mute_seconds BETWEEN 60 AND 2592000),
                join_mute_duration INTEGER NOT NULL DEFAULT 14400
                    CHECK(join_mute_duration BETWEEN 60 AND 2592000)
            );
            CREATE TABLE IF NOT EXISTS selections (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS join_actions (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, joined_at INTEGER NOT NULL,
                action TEXT NOT NULL, until_date INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id, joined_at)
            );
            INSERT OR IGNORE INTO groups (chat_id, title, type)
                SELECT DISTINCT chat_id, CAST(chat_id AS TEXT), 'unknown' FROM audit;
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(group_settings)")}
        if "join_mute_duration" not in columns:
            with self.db:
                self.db.execute("BEGIN")
                self.db.execute(
                    "ALTER TABLE group_settings ADD COLUMN join_mute_duration INTEGER "
                    "NOT NULL DEFAULT 14400 CHECK(join_mute_duration BETWEEN 60 AND 2592000)"
                )
                self.db.execute(
                    "UPDATE group_settings SET join_mute_duration=join_mute_seconds "
                    "WHERE join_mute_seconds > 0"
                )

    def remember_group(self, chat: dict) -> None:
        if chat.get("type") not in {"group", "supergroup"}:
            return
        with self.db:
            self.db.execute(
                "INSERT INTO groups VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE "
                "SET title=excluded.title, type=excluded.type",
                (chat["id"], chat.get("title", str(chat["id"]))[:256], chat["type"]),
            )

    def groups(self, offset: int = 0, limit: int = 10) -> list[int]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT chat_id FROM groups ORDER BY chat_id LIMIT ? OFFSET ?",
                (limit, offset),
            )
        ]

    def select_group(self, user_id: int, chat_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO selections VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id",
                (user_id, chat_id),
            )

    def selected_group(self, user_id: int) -> int | None:
        row = self.db.execute(
            "SELECT chat_id FROM selections WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def settings(self, chat_id: int, default_dry_run: bool) -> GroupSettings:
        row = self.db.execute(
            "SELECT dry_run, join_mute_seconds, join_mute_duration "
            "FROM group_settings WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return GroupSettings(default_dry_run)
        return GroupSettings(default_dry_run if row[0] is None else bool(row[0]), row[1], row[2])

    def set_dry_run(self, chat_id: int, enabled: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO group_settings (chat_id, dry_run) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET dry_run=excluded.dry_run",
                (chat_id, int(enabled)),
            )

    def set_join_mute(self, chat_id: int, seconds: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO group_settings (chat_id, join_mute_seconds, join_mute_duration) "
                "VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE "
                "SET join_mute_seconds=excluded.join_mute_seconds, "
                "join_mute_duration=CASE WHEN excluded.join_mute_seconds > 0 "
                "THEN excluded.join_mute_seconds ELSE group_settings.join_mute_duration END",
                (chat_id, seconds, seconds or 14400),
            )

    def join_handled(self, chat_id: int, user_id: int, joined_at: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM join_actions WHERE chat_id=? AND user_id=? AND joined_at=?",
                (chat_id, user_id, joined_at),
            ).fetchone()
            is not None
        )

    def record_join(
        self, chat_id: int, user_id: int, joined_at: int, action: str, until_date: int
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO join_actions VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, joined_at, action, until_date),
            )

    def offset(self) -> int | None:
        row = self.db.execute("SELECT offset FROM cursor WHERE id=1").fetchone()
        return row[0] if row else None

    def acknowledge(self, update_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO cursor VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET offset=excluded.offset",
                (update_id + 1,),
            )

    def completed(self, update_id: int) -> bool:
        row = self.db.execute("SELECT action FROM audit WHERE update_id=?", (update_id,)).fetchone()
        return row is not None and row[0] != "ban_pending"

    def recent_bans(self, chat_id: int, offset: int = 0, limit: int = 6) -> list[BanLogEntry]:
        rows = self.db.execute(
            "SELECT update_id, created_at, user_id, message_id, action, decision FROM audit "
            "WHERE chat_id=? AND action IN ('banned', 'would_ban') "
            "ORDER BY created_at DESC, update_id DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset),
        )
        entries = []
        for update_id, created_at, user_id, message_id, action, raw_decision in rows:
            try:
                decision = json.loads(raw_decision) if raw_decision else None
            except (ValueError, TypeError):
                decision = None
            entries.append(
                BanLogEntry(
                    update_id,
                    created_at,
                    user_id,
                    message_id,
                    action,
                    decision if isinstance(decision, dict) else None,
                )
            )
        return entries

    def record(
        self,
        update_id: int,
        message: dict,
        action: str,
        decision: dict | None = None,
        detail: str | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO audit
                       (update_id, chat_id, user_id, message_id, action, decision, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(update_id) DO UPDATE SET action=excluded.action,
                       decision=excluded.decision, detail=excluded.detail""",
                (
                    update_id,
                    message["chat"]["id"],
                    message["from"]["id"],
                    message["message_id"],
                    action,
                    json.dumps(decision) if decision else None,
                    detail,
                ),
            )

    def close(self) -> None:
        self.db.close()
