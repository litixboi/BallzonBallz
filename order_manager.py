import json
import logging
import os
import random
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("OrderManager")

script_dir = Path(__file__).parent.resolve()
DEFAULT_DB_PATH = script_dir / "orders.db"
ORDERS_FILE = script_dir / "orders.json"

# Support configurable DB path (e.g. Railway persistent volume /data/orders.db)
_custom_db_env = os.getenv("ORDERS_DB_PATH") or os.getenv("DATA_DIR")
if _custom_db_env:
    _p = Path(_custom_db_env)
    DB_PATH = _p if _p.suffix == ".db" else _p / "orders.db"
else:
    DB_PATH = DEFAULT_DB_PATH

_lock = threading.RLock()


class OrderManager:
    """Production-grade order manager backed by SQLite with WAL mode.
    Includes automatic historical migration from orders.json, volume persistence,
    and periodic atomic JSON snapshots for backward compatibility."""

    def __init__(self, db_path: Path = DB_PATH, json_fallback: Path = ORDERS_FILE):
        self.db_path = Path(db_path)
        self.json_fallback = Path(json_fallback)
        self._init_db()
        self._migrate_from_json_if_needed()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a connection with WAL mode and row factory enabled."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        """Initialize database schema with indexes and WAL mode."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        first_name TEXT,
                        plan_id TEXT,
                        plan_name TEXT,
                        quantity INTEGER DEFAULT 1,
                        volume_gb REAL,
                        duration_days INTEGER,
                        devices INTEGER DEFAULT 1,
                        price_usd REAL,
                        unit_price_usd REAL,
                        price_toman INTEGER,
                        unit_price_toman INTEGER,
                        crypto_network TEXT,
                        crypto_currency TEXT,
                        crypto_amount REAL,
                        tx_hash TEXT,
                        photo_file_id TEXT,
                        status TEXT NOT NULL,
                        created_at TEXT,
                        submitted_at TEXT,
                        resolved_at TEXT,
                        delivered_sub_url TEXT,
                        delivered_subs_json TEXT,
                        reject_reason TEXT,
                        approving INTEGER DEFAULT 0,
                        multiplier REAL DEFAULT 1.0,
                        extra_data_json TEXT
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);")
                conn.commit()

    def _migrate_from_json_if_needed(self):
        """Auto-migrate historical orders from orders.json into SQLite if table is empty."""
        with _lock:
            try:
                with self._get_connection() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                    if count == 0 and self.json_fallback.exists():
                        raw_data = json.loads(self.json_fallback.read_text(encoding="utf-8"))
                        if isinstance(raw_data, dict) and raw_data:
                            logger.info("Migrating %d existing orders from JSON to SQLite WAL database...", len(raw_data))
                            for oid, o in raw_data.items():
                                deliv_subs = o.get("delivered_subs")
                                deliv_subs_json = json.dumps(deliv_subs, ensure_ascii=False) if deliv_subs else None
                                extra_data = {}
                                for k in ["reminder_80_sent", "reminder_90_sent", "reminder_expiry_sent", "last_synced"]:
                                    if k in o:
                                        extra_data[k] = o[k]
                                extra_data_json = json.dumps(extra_data) if extra_data else None

                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO orders (
                                        order_id, user_id, username, first_name, plan_id, plan_name,
                                        quantity, volume_gb, duration_days, devices, price_usd, unit_price_usd,
                                        price_toman, unit_price_toman, crypto_network, crypto_currency,
                                        crypto_amount, tx_hash, photo_file_id, status, created_at,
                                        submitted_at, resolved_at, delivered_sub_url, delivered_subs_json,
                                        reject_reason, approving, multiplier, extra_data_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        o.get("order_id") or oid,
                                        o.get("user_id", 0),
                                        o.get("username", ""),
                                        o.get("first_name", ""),
                                        o.get("plan_id", ""),
                                        o.get("plan_name", ""),
                                        o.get("quantity", 1),
                                        o.get("volume_gb", 30),
                                        o.get("duration_days", 30),
                                        o.get("devices", 1),
                                        o.get("price_usd", 0.0),
                                        o.get("unit_price_usd", o.get("price_usd", 0.0)),
                                        o.get("price_toman", 0),
                                        o.get("unit_price_toman", o.get("price_toman", 0)),
                                        o.get("crypto_network", ""),
                                        o.get("crypto_currency", ""),
                                        o.get("crypto_amount", 0.0),
                                        o.get("tx_hash"),
                                        o.get("photo_file_id"),
                                        o.get("status", "AWAITING_PAYMENT"),
                                        o.get("created_at"),
                                        o.get("submitted_at"),
                                        o.get("resolved_at"),
                                        o.get("delivered_sub_url"),
                                        deliv_subs_json,
                                        o.get("reject_reason"),
                                        1 if o.get("_approving") else 0,
                                        o.get("multiplier", 1.0),
                                        extra_data_json,
                                    ),
                                )
                            conn.commit()
                            logger.info("Historical order migration complete.")
            except Exception as e:
                logger.error("Error during JSON-to-SQLite migration: %s", e)

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a SQLite Row into a dictionary conforming to the Order schema."""
        d = dict(row)
        # Parse delivered_subs JSON array
        subs_json = d.pop("delivered_subs_json", None)
        if subs_json:
            try:
                d["delivered_subs"] = json.loads(subs_json)
            except Exception:
                d["delivered_subs"] = []
        else:
            d["delivered_subs"] = []
            if d.get("delivered_sub_url"):
                d["delivered_subs"] = [{"sub_url": d["delivered_sub_url"]}]

        # Approving flag
        approving_int = d.pop("approving", 0)
        if approving_int:
            d["_approving"] = True

        # Extra data json
        extra_json = d.pop("extra_data_json", None)
        if extra_json:
            try:
                extra = json.loads(extra_json)
                d.update(extra)
            except Exception:
                pass
        return d

    def _export_json_snapshot(self):
        """Asynchronous non-blocking snapshot export to orders.json for backward compatibility."""
        def _export_task():
            with _lock:
                try:
                    with self._get_connection() as conn:
                        rows = conn.execute("SELECT * FROM orders ORDER BY created_at ASC").fetchall()
                    export_dict = {}
                    for r in rows:
                        d = self._row_to_dict(r)
                        export_dict[d["order_id"]] = d

                    tmp = self.json_fallback.with_name(f"{self.json_fallback.stem}_{threading.get_ident()}_{time.time_ns()}.tmp")
                    tmp.write_text(json.dumps(export_dict, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(self.json_fallback)
                except Exception as e:
                    logger.warning("Could not export JSON snapshot of orders: %s", e)

        t = threading.Thread(target=_export_task, daemon=True, name="OrdersJsonExporter")
        t.start()

    def create_order(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        plan: Dict[str, Any],
        crypto_network: str,
        crypto_currency: str,
        crypto_amount: float,
        quantity: int = 1,
        multiplier: float = 1.0,
        discount_percent: int = 0,
        discount_toman: int = 0,
        final_price_toman: Optional[int] = None,
        final_price_usd: Optional[float] = None,
    ) -> str:
        """Atomically create a new pending order and return its order_id."""
        with _lock:
            with self._get_connection() as conn:
                chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
                while True:
                    rand_code = "".join(secrets.choice(chars) for _ in range(6))
                    oid = f"ORD-{rand_code}"
                    existing = conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (oid,)).fetchone()
                    if not existing:
                        break

                qty = max(1, int(quantity))
                unit_usd = float(plan.get("price_usd", 0.0))
                unit_toman = int(plan.get("price_toman", 0))
                total_usd = round(unit_usd * qty, 2)
                total_toman = unit_toman * qty
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                effective_toman = int(final_price_toman) if final_price_toman is not None else max(0, total_toman - discount_toman)
                effective_usd = float(final_price_usd) if final_price_usd is not None else round(total_usd * (1.0 - discount_percent / 100.0), 2)

                extra_data = {}
                if discount_percent > 0 or discount_toman > 0:
                    extra_data["discount_percent"] = int(discount_percent)
                    extra_data["discount_toman"] = int(discount_toman)
                    extra_data["original_price_toman"] = total_toman
                    extra_data["original_price_usd"] = total_usd
                extra_json = json.dumps(extra_data) if extra_data else None

                conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, user_id, username, first_name, plan_id, plan_name,
                        quantity, volume_gb, duration_days, devices, price_usd, unit_price_usd,
                        price_toman, unit_price_toman, crypto_network, crypto_currency,
                        crypto_amount, status, created_at, approving, multiplier, extra_data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        oid,
                        user_id,
                        username or "",
                        first_name or "",
                        plan.get("id", ""),
                        plan.get("name_fa", plan.get("name_en", "VIP Plan")),
                        qty,
                        plan.get("volume_gb", 30),
                        plan.get("duration_days", 30),
                        plan.get("devices", 1),
                        effective_usd,
                        unit_usd,
                        effective_toman,
                        unit_toman,
                        crypto_network,
                        crypto_currency,
                        crypto_amount,
                        "AWAITING_PAYMENT",
                        now_str,
                        multiplier,
                        extra_json,
                    ),
                )
                conn.commit()

        self._export_json_snapshot()
        return oid

    def submit_payment_proof(
        self,
        order_id: str,
        tx_hash: Optional[str] = None,
        photo_file_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> bool:
        """Record user's transaction hash or receipt photo and advance to PENDING_VERIFICATION."""
        with _lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
                if not row:
                    return False
                order = self._row_to_dict(row)

                if order.get("status") == "APPROVED":
                    logger.warning("Attempted to submit proof for already approved order %s", order_id)
                    return False
                if user_id is not None and order.get("user_id") != user_id:
                    logger.warning(
                        "User %s attempted to submit proof for order %s belonging to %s",
                        user_id,
                        order_id,
                        order.get("user_id"),
                    )
                    return False

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                clean_hash = tx_hash.strip() if tx_hash else order.get("tx_hash")
                clean_photo = photo_file_id if photo_file_id else order.get("photo_file_id")

                conn.execute(
                    """
                    UPDATE orders
                    SET tx_hash = ?, photo_file_id = ?, status = 'PENDING_VERIFICATION', submitted_at = ?
                    WHERE order_id = ?
                    """,
                    (clean_hash, clean_photo, now_str, order_id),
                )
                conn.commit()

        self._export_json_snapshot()
        return True

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row:
                return self._row_to_dict(row)
        return None

    def get_user_latest_unpaid_order(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM orders
                WHERE user_id = ? AND status IN ('AWAITING_PAYMENT', 'PENDING_VERIFICATION')
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row:
                return self._row_to_dict(row)
        return None

    def get_user_orders(self, user_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'PENDING_VERIFICATION' ORDER BY created_at ASC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_approved_orders(self) -> List[Dict[str, Any]]:
        """Retrieve all active approved orders for background monitoring and quota accounting."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'APPROVED' ORDER BY resolved_at DESC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def start_approving_order(self, order_id: str) -> bool:
        """Atomically lock an order for approval using atomic SQLite compare-and-swap."""
        with _lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET approving = 1
                    WHERE order_id = ?
                      AND status NOT IN ('APPROVED', 'REJECTED')
                      AND approving = 0
                    """,
                    (order_id,),
                )
                conn.commit()
                success = cursor.rowcount == 1

        if success:
            self._export_json_snapshot()
        return success

    def cancel_approving_order(self, order_id: str) -> bool:
        """Release the approval lock if server provisioning encountered a failure."""
        with _lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE orders SET approving = 0 WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()
                success = cursor.rowcount > 0

        if success:
            self._export_json_snapshot()
        return success

    def approve_order(
        self,
        order_id: str,
        sub_url: str,
        delivered_subs: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Atomically set order to APPROVED and store delivery subscription records."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subs_payload = delivered_subs if delivered_subs else [{"sub_url": sub_url}]
        subs_json = json.dumps(subs_payload, ensure_ascii=False)

        with _lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'APPROVED',
                        approving = 0,
                        delivered_sub_url = ?,
                        delivered_subs_json = ?,
                        resolved_at = ?
                    WHERE order_id = ?
                    """,
                    (sub_url, subs_json, now_str, order_id),
                )
                conn.commit()
                success = cursor.rowcount > 0

        if success:
            self._export_json_snapshot()
            try:
                from club_manager import club_manager
                order = self.get_order(order_id)
                if order:
                    club_manager.record_order_approved(
                        user_id=order.get("user_id", 0),
                        duration_days=order.get("duration_days", 30),
                        toman_amount=order.get("price_toman", 0)
                    )
            except Exception as e:
                logger.warning("Could not record club loyalty for order %s: %s", order_id, e)
        return success

    def reject_order(self, order_id: str, reason: str = "") -> bool:
        """Atomically mark order as REJECTED."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'REJECTED',
                        approving = 0,
                        reject_reason = ?,
                        resolved_at = ?
                    WHERE order_id = ?
                    """,
                    (reason, now_str, order_id),
                )
                conn.commit()
                success = cursor.rowcount > 0

        if success:
            self._export_json_snapshot()
        return success

    def update_order_extra(self, order_id: str, key: str, value: Any) -> bool:
        """Update an arbitrary extra field (e.g. reminder flags, accounting checkpoint)."""
        with _lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT extra_data_json FROM orders WHERE order_id = ?", (order_id,)).fetchone()
                if not row:
                    return False
                extra_raw = row[0]
                extra = json.loads(extra_raw) if extra_raw else {}
                extra[key] = value
                conn.execute(
                    "UPDATE orders SET extra_data_json = ? WHERE order_id = ?",
                    (json.dumps(extra, ensure_ascii=False), order_id),
                )
                conn.commit()
        return True


# Global singleton
order_mgr = OrderManager()
