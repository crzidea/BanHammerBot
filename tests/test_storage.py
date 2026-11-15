import sqlite3
import tempfile
import unittest
from contextlib import closing

from banhammerbot.storage import Store


class BanLogStorageTests(unittest.TestCase):
    def test_recent_bans_filter_by_group_and_outcome_and_paginate_by_time(self):
        with closing(Store(":memory:")) as store:
            events = [
                (90, -1001, "banned", "2026-11-14 00:00:00"),
                (1, -1001, "would_ban", "2026-11-15 00:00:00"),
                (2, -1001, "banned", "2026-11-15 00:00:00"),
                (3, -1002, "banned", "2026-11-15 00:00:00"),
                (4, -1001, "allow", "2026-11-15 00:00:00"),
                (5, -1001, "error", "2026-11-15 00:00:00"),
                (6, -1001, "ban_pending", "2026-11-15 00:00:00"),
            ]
            for update_id, chat_id, action, created_at in events:
                store.record(
                    update_id,
                    {"chat": {"id": chat_id}, "from": {"id": 42}, "message_id": update_id},
                    action,
                    {"unsolicited_ad": 0.99, "spam": 0.01},
                )
                store.db.execute(
                    "UPDATE audit SET created_at=? WHERE update_id=?", (created_at, update_id)
                )
            newest = store.recent_bans(-1001, limit=2)
            older = store.recent_bans(-1001, offset=2, limit=2)
            self.assertEqual([entry.update_id for entry in newest], [2, 1])
            self.assertEqual([entry.update_id for entry in older], [90])
            self.assertEqual(newest[0].decision, {"unsolicited_ad": 0.99, "spam": 0.01})
            self.assertEqual([entry.update_id for entry in store.recent_bans(-1002)], [3])
            self.assertEqual(store.recent_bans(-1003), [])

    def test_existing_audits_are_available_after_upgrade_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE audit (
                        update_id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL, action TEXT NOT NULL,
                        decision TEXT, detail TEXT
                    );
                    INSERT INTO audit (update_id, chat_id, user_id, message_id, action)
                        VALUES (1, -1001, 42, 9, 'would_ban'), (2, -1001, 43, 10, 'banned');
                """)
            for _ in range(2):
                with closing(Store(path)) as store:
                    entries = store.recent_bans(-1001)
                    self.assertEqual([entry.action for entry in entries], ["banned", "would_ban"])
                    self.assertEqual([entry.user_id for entry in entries], [43, 42])
                    self.assertEqual([entry.message_id for entry in entries], [10, 9])

    def test_missing_or_unreadable_old_decision_does_not_hide_ban(self):
        with closing(Store(":memory:")) as store:
            for update_id, decision in enumerate((None, "not json", "[]", "null"), start=1):
                store.record(
                    update_id,
                    {"chat": {"id": -1001}, "from": {"id": 42}, "message_id": update_id},
                    "banned",
                )
                store.db.execute(
                    "UPDATE audit SET decision=? WHERE update_id=?", (decision, update_id)
                )
            entries = store.recent_bans(-1001)
            self.assertEqual(len(entries), 4)
            self.assertTrue(all(entry.decision is None for entry in entries))
