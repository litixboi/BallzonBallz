import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("ClubManager")

script_dir = Path(__file__).parent.resolve()
DEFAULT_DB_PATH = script_dir / "orders.db"

_lock = threading.RLock()


class ClubManager:
    """Production-grade Customer Club & Bonus Manager.
    Manages:
    1. Referral Discount (5% per referral up to 3 = 15%)
    2. Loyalty / Renewal Discount (5% per renewed month up to 3 = 15%)
    3. Combined Total Discount (Capped at 30% on invoice)
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._init_db()
        self._backfill_loyalty_from_approved_orders()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with _lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_club (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        referred_by INTEGER,
                        referral_count INTEGER DEFAULT 0,
                        renewed_months INTEGER DEFAULT 0,
                        total_orders INTEGER DEFAULT 0,
                        total_spent_toman INTEGER DEFAULT 0,
                        created_at TEXT,
                        updated_at TEXT
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS referral_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        referrer_id INTEGER NOT NULL,
                        referred_user_id INTEGER NOT NULL UNIQUE,
                        joined_at TEXT,
                        first_order_at TEXT,
                        status TEXT DEFAULT 'JOINED'
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_club_referred_by ON user_club(referred_by);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_events_ref ON referral_events(referrer_id);")
                conn.commit()

    def _backfill_loyalty_from_approved_orders(self):
        """Backfill existing approved orders to give users retroactive loyalty credit."""
        with _lock:
            try:
                with self._get_connection() as conn:
                    # Check if user_club is empty but orders has approved orders
                    club_count = conn.execute("SELECT COUNT(*) FROM user_club").fetchone()[0]
                    if club_count == 0:
                        rows = conn.execute(
                            """
                            SELECT user_id, username, first_name, duration_days, price_toman
                            FROM orders WHERE status = 'APPROVED'
                            """
                        ).fetchall()
                        user_stats: Dict[int, Dict[str, Any]] = {}
                        for r in rows:
                            uid = r["user_id"]
                            if uid not in user_stats:
                                user_stats[uid] = {
                                    "username": r["username"] or "",
                                    "first_name": r["first_name"] or "",
                                    "months": 0,
                                    "orders": 0,
                                    "toman": 0
                                }
                            days = r["duration_days"] or 30
                            months = max(1, round(days / 30))
                            user_stats[uid]["months"] += months
                            user_stats[uid]["orders"] += 1
                            user_stats[uid]["toman"] += int(r["price_toman"] or 0)

                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        for uid, s in user_stats.items():
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO user_club (
                                    user_id, username, first_name, referral_count,
                                    renewed_months, total_orders, total_spent_toman,
                                    created_at, updated_at
                                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                                """,
                                (uid, s["username"], s["first_name"], s["months"], s["orders"], s["toman"], now_str, now_str)
                            )
                        conn.commit()
                        if user_stats:
                            logger.info("Backfilled loyalty data for %d existing customers.", len(user_stats))
            except Exception as e:
                logger.warning("Backfill loyalty check warning: %s", e)

    def register_user_if_needed(self, user_id: int, username: str = "", first_name: str = ""):
        """Ensure user exists in user_club."""
        with _lock:
            with self._get_connection() as conn:
                existing = conn.execute("SELECT 1 FROM user_club WHERE user_id = ?", (user_id,)).fetchone()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not existing:
                    conn.execute(
                        """
                        INSERT INTO user_club (
                            user_id, username, first_name, referral_count,
                            renewed_months, total_orders, total_spent_toman,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?)
                        """,
                        (user_id, username or "", first_name or "", now_str, now_str)
                    )
                    conn.commit()
                else:
                    if username or first_name:
                        conn.execute(
                            "UPDATE user_club SET username = ?, first_name = ?, updated_at = ? WHERE user_id = ?",
                            (username or "", first_name or "", now_str, user_id)
                        )
                        conn.commit()

    def process_referral(self, new_user_id: int, new_username: str, new_first_name: str, referrer_id: int) -> Tuple[bool, str]:
        """Process a referral invitation when a new user enters via /start ref_{referrer_id}."""
        if new_user_id == referrer_id:
            return False, "self_referral"

        with _lock:
            with self._get_connection() as conn:
                # Check if referred user already recorded
                existing_ref = conn.execute(
                    "SELECT referrer_id FROM referral_events WHERE referred_user_id = ?", (new_user_id,)
                ).fetchone()
                if existing_ref:
                    return False, "already_referred"

                # Check if user already exists in user_club with previous activity
                existing_club = conn.execute("SELECT * FROM user_club WHERE user_id = ?", (new_user_id,)).fetchone()
                if existing_club and (existing_club["total_orders"] > 0 or existing_club["referred_by"]):
                    return False, "existing_customer"

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Insert referral event
                conn.execute(
                    """
                    INSERT INTO referral_events (referrer_id, referred_user_id, joined_at, status)
                    VALUES (?, ?, ?, 'JOINED')
                    """,
                    (referrer_id, new_user_id, now_str)
                )

                # Ensure referrer exists in user_club
                ref_user = conn.execute("SELECT 1 FROM user_club WHERE user_id = ?", (referrer_id,)).fetchone()
                if not ref_user:
                    conn.execute(
                        """
                        INSERT INTO user_club (user_id, referral_count, renewed_months, total_orders, created_at, updated_at)
                        VALUES (?, 1, 0, 0, ?, ?)
                        """,
                        (referrer_id, now_str, now_str)
                    )
                else:
                    conn.execute(
                        "UPDATE user_club SET referral_count = referral_count + 1, updated_at = ? WHERE user_id = ?",
                        (now_str, referrer_id)
                    )

                # Ensure new user recorded with referred_by
                if not existing_club:
                    conn.execute(
                        """
                        INSERT INTO user_club (
                            user_id, username, first_name, referred_by, referral_count,
                            renewed_months, total_orders, total_spent_toman, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
                        """,
                        (new_user_id, new_username or "", new_first_name or "", referrer_id, now_str, now_str)
                    )
                else:
                    conn.execute(
                        "UPDATE user_club SET referred_by = ?, updated_at = ? WHERE user_id = ?",
                        (referrer_id, now_str, new_user_id)
                    )

                conn.commit()
                return True, "success"

    def record_order_approved(self, user_id: int, duration_days: int, toman_amount: int):
        """When an order is approved by admin, increment customer loyalty and total spent."""
        months = max(1, round((duration_days or 30) / 30))
        with _lock:
            with self._get_connection() as conn:
                self.register_user_if_needed(user_id)
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    UPDATE user_club
                    SET renewed_months = renewed_months + ?,
                        total_orders = total_orders + 1,
                        total_spent_toman = total_spent_toman + ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (months, toman_amount, now_str, user_id)
                )

                # If this user was referred by someone, mark first order
                conn.execute(
                    """
                    UPDATE referral_events
                    SET status = 'PURCHASED', first_order_at = ?
                    WHERE referred_user_id = ? AND status = 'JOINED'
                    """,
                    (now_str, user_id)
                )
                conn.commit()

    def get_user_club_info(self, user_id: int) -> Dict[str, Any]:
        """Fetch user club statistics and calculated discount percentages."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM user_club WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                referrals = 0
                renewed = 0
                orders = 0
                spent = 0
            else:
                referrals = row["referral_count"] or 0
                renewed = row["renewed_months"] or 0
                orders = row["total_orders"] or 0
                spent = row["total_spent_toman"] or 0

        # 1. Referral discount: 5% per referral (up to 3 = 15%)
        ref_discount = min(15, referrals * 5)

        # 2. Loyalty discount: 5% per renewal month (up to 3 months = 15%)
        loyalty_discount = min(15, renewed * 5)

        # 3. Total discount: combined up to 30% max
        total_discount = min(30, ref_discount + loyalty_discount)

        return {
            "user_id": user_id,
            "referral_count": referrals,
            "renewed_months": renewed,
            "total_orders": orders,
            "total_spent_toman": spent,
            "referral_discount_pct": ref_discount,
            "loyalty_discount_pct": loyalty_discount,
            "total_discount_pct": total_discount,
        }

    def calculate_discounted_price(self, user_id: int, base_toman: int, base_usd: float) -> Dict[str, Any]:
        """Calculates exact discount amounts and net payable price."""
        info = self.get_user_club_info(user_id)
        pct = info["total_discount_pct"]

        if pct > 0:
            discount_toman = int(base_toman * (pct / 100.0))
            final_toman = max(0, base_toman - discount_toman)
            discount_usd = round(base_usd * (pct / 100.0), 2)
            final_usd = max(0.1, round(base_usd - discount_usd, 2))
        else:
            discount_toman = 0
            final_toman = base_toman
            discount_usd = 0.0
            final_usd = base_usd

        return {
            "user_id": user_id,
            "discount_pct": pct,
            "ref_discount_pct": info["referral_discount_pct"],
            "loyalty_discount_pct": info["loyalty_discount_pct"],
            "base_toman": base_toman,
            "discount_toman": discount_toman,
            "final_toman": final_toman,
            "base_usd": base_usd,
            "discount_usd": discount_usd,
            "final_usd": final_usd,
        }

    def get_club_text(self, user_id: int, bot_username: str = "litixconnectBot") -> str:
        """Returns beautiful, engaging Persian message for Customer Club."""
        info = self.get_user_club_info(user_id)
        ref_count = info["referral_count"]
        ref_pct = info["referral_discount_pct"]
        renew_m = info["renewed_months"]
        loyalty_pct = info["loyalty_discount_pct"]
        tot_pct = info["total_discount_pct"]
        bot_clean = bot_username.lstrip("@")
        ref_link = f"https://t.me/{bot_clean}?start=ref_{user_id}"

        status_stars = "⭐" * max(1, tot_pct // 5)
        text = (
            "🎁 <b>باشگاه مشتریان و تخفیف‌های ویژه LitixConnect</b>\n\n"
            f"👤 <b>شناسه حساب کاربری:</b> <code>{user_id}</code>\n"
            f"🏆 <b>سطح تخفیف فعال شما:</b> <b>{tot_pct}٪ تخفیف روی کل فاکتور</b> {status_stars}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 وضعیت امتیازات و تخفیف‌های شما:</b>\n\n"
            f"👥 <b>۱. تخفیف دعوت از دوستان:</b> <b>{ref_pct}٪</b> (از سقف ۱۵٪)\n"
            f"▫️ تعداد دوستان معرفی‌شده: <b>{ref_count} نفر</b>\n"
            "▫️ <i>فرمول: به ازای هر معرفی ۵٪ تخفیف (تا ۳ نفر = ۱۵٪)</i>\n\n"
            f"🔄 <b>۲. تخفیف وفاداری و تمدید:</b> <b>{loyalty_pct}٪</b> (از سقف ۱۵٪)\n"
            f"▫️ سابقه تمدید و اشتراک: <b>{renew_m} ماه</b>\n"
            "▫️ <i>فرمول: به ازای هر ماه تمدید ۵٪ تخفیف (تا ۳ ماه = ۱۵٪)</i>\n\n"
            "⭐️ <b>امکان تجمیع و دریافت همزمان هر دو تخفیف تا سقف ۳۰٪!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔗 <b>لینک اختصاصی دعوت شما:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "💡 <i>لینک بالا را کپی کنید یا با دکمه زیر مستقیماً برای دوستانتان بفرستید. "
            "به محض ورود و خرید آنها، تخفیف ۵ درصدی بلافاصله به حسابتان اعمال می‌شود!</i>"
        )
        return text


club_manager = ClubManager()
