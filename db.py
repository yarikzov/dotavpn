"""
db.py — SQLite database layer for VpnDotaBot bot.
All user profiles, subscriptions, payments, referrals, tariffs.
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "vpn_bot.db"


class Database:
    _lock = threading.Lock()

    def __init__(self, path: str = DB_PATH):
        self._path = path

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ── Init ──────────────────────────────────────────────────────────────────
    def init(self):
        with self._conn() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id      INTEGER PRIMARY KEY,
                    username     TEXT,
                    first_name   TEXT,
                    uuid         TEXT,
                    client_email TEXT,
                    sub_end      INTEGER DEFAULT 0,
                    trial_used   INTEGER DEFAULT 0,
                    balance      REAL    DEFAULT 0.0,
                    referrer_id  INTEGER,
                    created_at   INTEGER DEFAULT (strftime('%s','now')),
                    updated_at   INTEGER DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id  INTEGER UNIQUE,
                    user_id     INTEGER,
                    tariff_id   INTEGER,
                    amount      REAL,
                    currency    TEXT    DEFAULT 'USDT',
                    status      TEXT    DEFAULT 'pending',
                    created_at  INTEGER DEFAULT (strftime('%s','now')),
                    paid_at     INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER,
                    referee_id  INTEGER UNIQUE,
                    reward      REAL    DEFAULT 0.0,
                    created_at  INTEGER DEFAULT (strftime('%s','now')),
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                    FOREIGN KEY (referee_id)  REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS tariffs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT,
                    days        INTEGER,
                    price_usd   REAL,
                    gb_limit    INTEGER DEFAULT 0,
                    active      INTEGER DEFAULT 1,
                    sort_order  INTEGER DEFAULT 0,
                    created_at  INTEGER DEFAULT (strftime('%s','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_users_sub_end ON users(sub_end);
                CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
                CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON referrals(referrer_id);
            """)
        logger.info("Database initialized: %s", self._path)

    def seed_tariffs(self):
        """Insert default tariffs if table is empty."""
        with self._conn() as db:
            if db.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0] > 0:
                return
            db.executemany(
                "INSERT INTO tariffs (name, days, price_usd, gb_limit, sort_order) VALUES (?,?,?,?,?)",
                [
                    ("🚀 Личный · 1 мес", 30, 1.99, 0),
                    ("👨‍👩‍👧 Семейный · 1 мес", 30, 3.99, 0),
                    ("💎 Максимальный · 1 мес", 30, 4.99, 0),
                    ("🎮 Игровой · 1 мес", 30, 3.99, 0),
                    ("🚀 Личный · 1 год", 365, 14.99, 0),
                    ("👨‍👩‍👧 Семейный · 1 год", 365, 35.99, 0),
                    ("💎 Максимальный · 1 год", 365, 55.00, 0),
                    ("🎮 Игровой · 1 год", 365, 45.00, 0),
                ],
            )
        logger.info("Default tariffs seeded.")

    # ── Users ─────────────────────────────────────────────────────────────────
    def ensure_user(
            self,
            user_id: int,
            username: Optional[str],
            referrer_id: Optional[int] = None,
    ) -> bool:
        """Create user if not exists. Returns True if new user."""
        with self._conn() as db:
            existing = db.execute(
                "SELECT user_id FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing:
                return False
            ref = referrer_id if (referrer_id and referrer_id != user_id) else None
            db.execute(
                "INSERT INTO users (user_id, username, referrer_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                (user_id, username, ref, int(time.time()), int(time.time())),
            )
            if ref:
                db.execute(
                    "INSERT OR IGNORE INTO referrals (referrer_id, referee_id) VALUES (?,?)",
                    (ref, user_id),
                )
            return True

    def update_username(self, user_id: int, username: str = None, first_name: str = None):
        """Update user's Telegram info"""
        with self._conn() as db:
            # Determine display name
            if username:
                display_name = username
            elif first_name:
                display_name = first_name
            else:
                display_name = str(user_id)

            # Also store first_name separately if provided
            if first_name:
                db.execute(
                    "UPDATE users SET username=?, first_name=?, updated_at=? WHERE user_id=?",
                    (display_name, first_name, int(time.time()), user_id),
                )
            else:
                db.execute(
                    "UPDATE users SET username=?, updated_at=? WHERE user_id=?",
                    (display_name, int(time.time()), user_id),
                )

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def set_trial(self, user_id: int, trial_days: int):
        expiry_ms = int((time.time() + trial_days * 86400) * 1000)
        with self._conn() as db:
            db.execute(
                "UPDATE users SET sub_end=?, trial_used=1, updated_at=? "
                "WHERE user_id=? AND trial_used=0",
                (expiry_ms, int(time.time()), user_id),
            )

    def set_client(self, user_id: int, uuid: str, email: str, expiry_ms: int):
        with self._conn() as db:
            db.execute(
                "UPDATE users SET uuid=?, client_email=?, sub_end=?, updated_at=? WHERE user_id=?",
                (uuid, email, expiry_ms, int(time.time()), user_id),
            )

    def extend_subscription(self, user_id: int, expiry_ms: int):
        with self._conn() as db:
            db.execute(
                "UPDATE users SET sub_end=?, updated_at=? WHERE user_id=?",
                (expiry_ms, int(time.time()), user_id),
            )

    def deactivate_user(self, user_id: int):
        with self._conn() as db:
            db.execute(
                "UPDATE users SET sub_end=0, updated_at=? WHERE user_id=?",
                (int(time.time()), user_id),
            )

    def get_all_user_ids(self) -> list:
        with self._conn() as db:
            rows = db.execute("SELECT user_id FROM users").fetchall()
            return [r["user_id"] for r in rows]

    def get_all_users(self, limit: int = 100, offset: int = 0) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT user_id, username, first_name, sub_end, trial_used, balance, "
                "referrer_id, created_at, updated_at FROM users "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_expired_users(self) -> list:
        now_ms = int(time.time() * 1000)
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM users WHERE sub_end > 0 AND sub_end < ?",
                (now_ms,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_active_users(self) -> list:
        now_ms = int(time.time() * 1000)
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM users WHERE sub_end > ?",
                (now_ms,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user_stats(self, user_id: int) -> dict:
        """Get user statistics including total spent and traffic"""
        with self._conn() as db:
            # Total spent
            spent_row = db.execute(
                "SELECT COALESCE(SUM(amount), 0) as total_spent FROM payments "
                "WHERE user_id=? AND status='paid'",
                (user_id,),
            ).fetchone()

            # Referral stats
            ref_stats = self.get_referral_stats(user_id)

            return {
                "total_spent": spent_row["total_spent"] if spent_row else 0,
                "ref_count": ref_stats["count"],
                "ref_earned": ref_stats["earned"],
            }

    # ── Tariffs ───────────────────────────────────────────────────────────────
    def get_all_tariffs(self) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM tariffs WHERE active=1 ORDER BY sort_order, price_usd"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_tariff(self, tariff_id: int) -> Optional[dict]:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM tariffs WHERE id=? AND active=1", (tariff_id,)
            ).fetchone()
            return dict(row) if row else None

    def add_tariff(self, name: str, days: int, price_usd: float, gb_limit: int = 0) -> int:
        with self._conn() as db:
            cursor = db.execute(
                "INSERT INTO tariffs (name, days, price_usd, gb_limit) VALUES (?,?,?,?)",
                (name, days, price_usd, gb_limit),
            )
            return cursor.lastrowid

    def update_tariff(self, tariff_id: int, **kwargs):
        with self._conn() as db:
            fields = []
            values = []
            for key, value in kwargs.items():
                if key in ["name", "days", "price_usd", "gb_limit", "active", "sort_order"]:
                    fields.append(f"{key}=?")
                    values.append(value)
            if fields:
                values.append(tariff_id)
                db.execute(
                    f"UPDATE tariffs SET {', '.join(fields)} WHERE id=?",
                    values,
                )

    def delete_tariff(self, tariff_id: int):
        with self._conn() as db:
            db.execute("UPDATE tariffs SET active=0 WHERE id=?", (tariff_id,))

    # ── Payments ──────────────────────────────────────────────────────────────
    def add_payment(
            self,
            invoice_id: int,
            user_id: int,
            amount: float,
            tariff_id: int = 0,
            currency: str = "USDT",
    ):
        with self._conn() as db:
            db.execute(
                "INSERT OR IGNORE INTO payments "
                "(invoice_id, user_id, tariff_id, amount, currency, created_at) VALUES (?,?,?,?,?,?)",
                (invoice_id, user_id, tariff_id, amount, currency, int(time.time())),
            )

    def update_payment_status(self, invoice_id: int, status: str):
        with self._conn() as db:
            db.execute(
                "UPDATE payments SET status=?, paid_at=? WHERE invoice_id=?",
                (status, int(time.time()) if status == "paid" else None, invoice_id),
            )

    def get_pending_payments(self) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM payments WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user_payments(self, user_id: int) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT p.*, t.name AS tariff_name FROM payments p "
                "LEFT JOIN tariffs t ON p.tariff_id=t.id "
                "WHERE p.user_id=? ORDER BY p.created_at DESC LIMIT 20",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_payment_activated(self, invoice_id: int) -> bool:
        with self._conn() as db:
            row = db.execute(
                "SELECT status FROM payments WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
            return row and row["status"] == "paid"

    def get_total_income(self) -> float:
        with self._conn() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE status='paid'"
            ).fetchone()
            return row["total"] if row else 0.0

    # ── Referrals ─────────────────────────────────────────────────────────────
    def get_referrer(self, user_id: int) -> Optional[dict]:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM referrals WHERE referee_id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_referral_stats(self, user_id: int) -> dict:
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(reward),0) AS earned "
                "FROM referrals WHERE referrer_id=?",
                (user_id,),
            ).fetchone()
            return {"count": row["cnt"], "earned": row["earned"]}

    def get_referrals_list(self, user_id: int) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT r.referee_id, r.reward, r.created_at, u.username, u.first_name "
                "FROM referrals r "
                "LEFT JOIN users u ON r.referee_id = u.user_id "
                "WHERE r.referrer_id=? "
                "ORDER BY r.created_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_referral_reward(self, referrer_id: int, referee_id: int, reward: float):
        with self._conn() as db:
            db.execute(
                "UPDATE referrals SET reward=reward+? WHERE referrer_id=? AND referee_id=?",
                (reward, referrer_id, referee_id),
            )
            db.execute(
                "UPDATE users SET balance=balance+?, updated_at=? WHERE user_id=?",
                (reward, int(time.time()), referrer_id),
            )

    # ── Balance / Withdrawals ─────────────────────────────────────────────────
    def update_balance(self, user_id: int, amount: float):
        """Add or subtract from user balance"""
        with self._conn() as db:
            db.execute(
                "UPDATE users SET balance=balance+?, updated_at=? WHERE user_id=?",
                (amount, int(time.time()), user_id),
            )

    def get_balance(self, user_id: int) -> float:
        with self._conn() as db:
            row = db.execute(
                "SELECT balance FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return row["balance"] if row else 0.0

    # ── Admin stats ───────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        now_ms = int(time.time() * 1000)
        with self._conn() as db:
            total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_subs = db.execute(
                "SELECT COUNT(*) FROM users WHERE sub_end > ?", (now_ms,)
            ).fetchone()[0]
            total_income = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'"
            ).fetchone()[0]
            total_referrals = db.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
            trial_users = db.execute(
                "SELECT COUNT(*) FROM users WHERE trial_used=1"
            ).fetchone()[0]

        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "total_income": total_income,
            "total_referrals": total_referrals,
            "trial_users": trial_users,
        }

    def get_detailed_stats(self) -> dict:
        """Get detailed statistics for admin panel"""
        with self._conn() as db:
            # Daily new users for last 7 days
            daily_users = db.execute("""
                SELECT date(created_at, 'unixepoch') as day, COUNT(*) as count
                FROM users 
                WHERE created_at > strftime('%s', 'now', '-7 days')
                GROUP BY day
                ORDER BY day DESC
            """).fetchall()

            # Daily income for last 7 days
            daily_income = db.execute("""
                SELECT date(paid_at, 'unixepoch') as day, COALESCE(SUM(amount), 0) as total
                FROM payments 
                WHERE status='paid' AND paid_at > strftime('%s', 'now', '-7 days')
                GROUP BY day
                ORDER BY day DESC
            """).fetchall()

            # Top referrers
            top_referrers = db.execute("""
                SELECT u.user_id, u.username, COUNT(r.id) as ref_count, COALESCE(SUM(r.reward), 0) as total_reward
                FROM users u
                JOIN referrals r ON u.user_id = r.referrer_id
                GROUP BY u.user_id
                ORDER BY ref_count DESC
                LIMIT 10
            """).fetchall()

            return {
                "daily_users": [dict(r) for r in daily_users],
                "daily_income": [dict(r) for r in daily_income],
                "top_referrers": [dict(r) for r in top_referrers],
            }

    # ── Maintenance ───────────────────────────────────────────────────────────
    def cleanup_expired_sessions(self):
        """Clean up expired subscriptions"""
        now_ms = int(time.time() * 1000)
        with self._conn() as db:
            db.execute(
                "UPDATE users SET sub_end=0, updated_at=? WHERE sub_end > 0 AND sub_end < ?",
                (int(time.time()), now_ms),
            )

    def vacuum(self):
        """Optimize database"""
        with self._conn() as db:
            db.execute("VACUUM")
        logger.info("Database vacuum completed")