import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("OrderManager")

script_dir = Path(__file__).parent
ORDERS_FILE = script_dir / "orders.json"
_lock = threading.Lock()


class OrderManager:
    def __init__(self, filepath: Path = ORDERS_FILE):
        self.filepath = filepath
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        with _lock:
            if not self.filepath.exists():
                self._orders = {}
                return
            try:
                data = json.loads(self.filepath.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._orders = data
                else:
                    self._orders = {}
            except Exception as e:
                logger.warning("Could not read orders file (%s): %s", self.filepath, e)
                self._orders = {}

    def _save(self):
        try:
            tmp = self.filepath.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._orders, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.filepath)
        except Exception as e:
            logger.error("Failed to save orders file: %s", e)

    def create_order(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        plan: Dict[str, Any],
        crypto_network: str,
        crypto_currency: str,
        crypto_amount: float,
    ) -> str:
        """Create a new pending order and return its order_id."""
        with _lock:
            # Generate readable order ID e.g. ORD-63821
            while True:
                oid = f"ORD-{random.randint(10000, 99999)}"
                if oid not in self._orders:
                    break

            order = {
                "order_id": oid,
                "user_id": user_id,
                "username": username or "",
                "first_name": first_name or "",
                "plan_id": plan["id"],
                "plan_name": plan.get("name_fa", plan.get("name_en", "VIP Plan")),
                "volume_gb": plan.get("volume_gb", 30),
                "duration_days": plan.get("duration_days", 30),
                "devices": plan.get("devices", 1),
                "price_usd": plan.get("price_usd", 0.0),
                "crypto_network": crypto_network,
                "crypto_currency": crypto_currency,
                "crypto_amount": crypto_amount,
                "tx_hash": None,
                "photo_file_id": None,
                "status": "AWAITING_PAYMENT",  # AWAITING_PAYMENT -> PENDING_VERIFICATION -> APPROVED/REJECTED
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "resolved_at": None,
                "delivered_sub_url": None,
            }
            self._orders[oid] = order
            self._save()
            return oid

    def submit_payment_proof(
        self,
        order_id: str,
        tx_hash: Optional[str] = None,
        photo_file_id: Optional[str] = None,
    ) -> bool:
        """Record user's transaction hash or receipt photo and advance to PENDING_VERIFICATION."""
        with _lock:
            if order_id not in self._orders:
                return False
            order = self._orders[order_id]
            if tx_hash:
                order["tx_hash"] = tx_hash.strip()
            if photo_file_id:
                order["photo_file_id"] = photo_file_id
            order["status"] = "PENDING_VERIFICATION"
            order["submitted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return True

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with _lock:
            return self._orders.get(order_id)

    def get_user_latest_unpaid_order(self, user_id: int) -> Optional[Dict[str, Any]]:
        with _lock:
            for oid in reversed(list(self._orders.keys())):
                o = self._orders[oid]
                if o["user_id"] == user_id and o["status"] in ("AWAITING_PAYMENT", "PENDING_VERIFICATION"):
                    return o
            return None

    def get_user_orders(self, user_id: int) -> List[Dict[str, Any]]:
        with _lock:
            return [o for o in self._orders.values() if o["user_id"] == user_id]

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        with _lock:
            return [o for o in self._orders.values() if o["status"] == "PENDING_VERIFICATION"]

    def approve_order(self, order_id: str, sub_url: str) -> bool:
        with _lock:
            if order_id not in self._orders:
                return False
            order = self._orders[order_id]
            order["status"] = "APPROVED"
            order["delivered_sub_url"] = sub_url
            order["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return True

    def reject_order(self, order_id: str, reason: str = "") -> bool:
        with _lock:
            if order_id not in self._orders:
                return False
            order = self._orders[order_id]
            order["status"] = "REJECTED"
            order["reject_reason"] = reason
            order["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return True


# Global singleton
order_mgr = OrderManager()
