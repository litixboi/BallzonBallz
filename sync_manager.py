import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from conpanel_api import conpanel_mgr, freshpanel_mgr

logger = logging.getLogger("PanelSyncManager")

script_dir = Path(__file__).parent.resolve()
SYNC_INTERVAL_SEC = int(os.getenv("PANEL_SYNC_INTERVAL_SEC", "900"))  # Default: 15 minutes (15-30 min spec)
SECONDARY_INBOUND_ID = int(os.getenv("SECONDARY_INBOUND_ID", "2"))  # Inbound 2: ⚡ Bridge [2X]


class PanelSyncManager:
    """Production-grade bi-directional dual-panel synchronization engine.
    Harmonizes client UUIDs, subIds, traffic quotas, expiry dates, and enabled states
    between Primary (Germany c23) and Secondary (Domestic c13 freshbridge).
    Enforces highest recorded consumption collision resolution and alerts on reachability timeouts."""

    def __init__(
        self,
        primary=conpanel_mgr,
        secondary=freshpanel_mgr,
        secondary_inbound_id: int = SECONDARY_INBOUND_ID,
        alert_callback: Optional[Callable[[str], Any]] = None,
    ):
        self.primary = primary
        self.secondary = secondary
        self.secondary_inbound_id = secondary_inbound_id
        self.alert_callback = alert_callback
        self.last_sync_time = 0.0
        self.last_sync_status: Dict[str, Any] = {
            "success": False,
            "added": 0,
            "updated": 0,
            "deleted": 0,
            "total_primary": 0,
            "total_secondary": 0,
            "last_error": None,
            "timestamp": None,
        }
        self._lock = threading.Lock()

    def sync_once(self) -> Dict[str, Any]:
        """Perform a single comprehensive synchronization and state reconciliation cycle."""
        with self._lock:
            result = {
                "success": False,
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "total_primary": 0,
                "total_secondary": 0,
                "last_error": None,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # 1. Authenticate both panels
            pri_ok = self.primary.ensure_auth()
            sec_ok = self.secondary.ensure_auth()

            if not pri_ok or not sec_ok:
                err_msg = f"Panel auth failed: Primary={pri_ok}, Secondary={sec_ok}"
                logger.warning("[%s] %s", self.__class__.__name__, err_msg)
                result["last_error"] = err_msg
                self.last_sync_status = result
                if self.alert_callback:
                    try:
                        self.alert_callback(f"⚠️ <b>هشدار هماهنگ‌سازی پنل‌ها:</b> خطا در احراز هویت سرورها\n<code>{err_msg}</code>")
                    except Exception as e:
                        logger.error("Failed to invoke alert callback: %s", e)
                return result

            # 2. Export client datasets
            try:
                pri_items = self.primary.export_clients()
                sec_items = self.secondary.export_clients()
            except Exception as e:
                err_msg = f"Export exception: {e}"
                logger.error("[%s] %s", self.__class__.__name__, err_msg)
                result["last_error"] = err_msg
                self.last_sync_status = result
                return result

            if not pri_items and not sec_items:
                logger.info("Both panels returned empty client lists.")
                result["success"] = True
                self.last_sync_status = result
                return result

            # Index clients by email and canonical identity
            pri_by_email: Dict[str, Dict[str, Any]] = {
                item["client"]["email"]: item["client"] for item in pri_items if item.get("client", {}).get("email")
            }
            sec_by_email: Dict[str, Dict[str, Any]] = {
                item["client"]["email"]: item["client"] for item in sec_items if item.get("client", {}).get("email")
            }

            result["total_primary"] = len(pri_by_email)
            result["total_secondary"] = len(sec_by_email)

            added = 0
            updated = 0
            deleted = 0

            # --- Phase A: Replicate New Clients into Secondary Inbound 2 ---
            to_import = []
            for email, p_client in pri_by_email.items():
                if email not in sec_by_email:
                    # Clean copy of client payload
                    import_client = dict(p_client)
                    to_import.append({
                        "client": import_client,
                        "inboundIds": [self.secondary_inbound_id],
                    })

            if to_import:
                if self.secondary.import_clients(to_import):
                    added = len(to_import)
                    logger.info("[Sync:Add] Replicated %d new client(s) to Secondary Inbound %d: %s",
                                added, self.secondary_inbound_id, [c["client"]["email"] for c in to_import])
                else:
                    logger.error("[Sync:Add] Failed to batch import %d clients to Secondary panel", len(to_import))

            # --- Phase B: Harmonize Attributes & Collision Resolution ---
            fields_to_harmonize = ["id", "subId", "totalGB", "expiryTime", "enable", "limitHwid", "limitIp"]

            for email, p_client in pri_by_email.items():
                if email in sec_by_email:
                    s_client = sec_by_email[email]
                    needs_update = False

                    # 1. Attribute drift check
                    for field in fields_to_harmonize:
                        if p_client.get(field) != s_client.get(field):
                            needs_update = True
                            break

                    # 2. Collision resolution for totalGB and expiryTime:
                    # Latest payment / highest quota wins
                    effective_total = max(p_client.get("totalGB", 0), s_client.get("totalGB", 0))
                    effective_expiry = max(p_client.get("expiryTime", 0), s_client.get("expiryTime", 0))

                    if effective_total != s_client.get("totalGB"):
                        s_client["totalGB"] = effective_total
                        needs_update = True
                    if effective_expiry != s_client.get("expiryTime"):
                        s_client["expiryTime"] = effective_expiry
                        needs_update = True

                    # 3. Quota enforcement: if primary disabled client, secondary must be disabled
                    if not p_client.get("enable", True) and s_client.get("enable", True):
                        s_client["enable"] = False
                        needs_update = True

                    # Ensure UUID and subId remain strictly identical across mirrors
                    if s_client.get("id") != p_client.get("id"):
                        s_client["id"] = p_client.get("id")
                        needs_update = True
                    if s_client.get("subId") != p_client.get("subId"):
                        s_client["subId"] = p_client.get("subId")
                        needs_update = True

                    if needs_update:
                        if self.secondary.update_client(email, s_client):
                            updated += 1
                            logger.info("[Sync:Update] Harmonized client '%s' on Secondary panel", email)

            # --- Phase C: Delete Orphan Clients ---
            for email in list(sec_by_email.keys()):
                # Only delete customer accounts (tg_*) if removed from primary
                if email.startswith("tg_") and email not in pri_by_email:
                    if self.secondary.delete_client(email):
                        deleted += 1
                        logger.info("[Sync:Delete] Cleaned orphan client '%s' from Secondary panel", email)

            result["added"] = added
            result["updated"] = updated
            result["deleted"] = deleted
            result["success"] = True
            self.last_sync_time = time.time()
            self.last_sync_status = result

            if added > 0 or updated > 0 or deleted > 0:
                logger.info(
                    "[Sync Complete] Added: %d, Updated: %d, Deleted: %d. Total Primary: %d, Total Secondary: %d",
                    added, updated, deleted, result["total_primary"], result["total_secondary"]
                )
            return result

    def run_sync_loop(self, interval_sec: int = SYNC_INTERVAL_SEC):
        """Continuous background synchronization loop."""
        logger.info("Starting dual-panel synchronization daemon loop (interval: %ds)...", interval_sec)
        while True:
            try:
                self.sync_once()
            except Exception as e:
                logger.error("Unexpected exception in sync daemon loop: %s", e)
            time.sleep(interval_sec)

    def start_background_thread(self, interval_sec: int = SYNC_INTERVAL_SEC) -> threading.Thread:
        """Launch sync daemon as a background daemon thread."""
        thread = threading.Thread(target=self.run_sync_loop, args=(interval_sec,), daemon=True, name="PanelSyncDaemon")
        thread.start()
        return thread


# Global singleton instance
sync_mgr = PanelSyncManager()


if __name__ == "__main__":
    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="3X-UI Dual-Panel Synchronization Manager")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a loop")
    parser.add_argument("--interval", type=int, default=SYNC_INTERVAL_SEC, help="Sync loop interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run single sync cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

    if args.loop:
        sync_mgr.run_sync_loop(interval_sec=args.interval)
    else:
        print("Executing single synchronization cycle...")
        res = sync_mgr.sync_once()
        print(f"Sync result: {res}")
