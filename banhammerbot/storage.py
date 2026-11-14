import json
import sqlite3
from pathlib import Path


class Store:
    """Persist only the polling cursor and moderation metadata, never message/image content."""

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
        """)

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
