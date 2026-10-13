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

from conpanel_api import conpanel_mgr, botpanel_mgr, freshpanel_mgr, asiapanel_mgr

logger = logging.getLogger("PanelSyncManager")

script_dir = Path(__file__).parent.resolve()
SYNC_INTERVAL_SEC = int(os.getenv("PANEL_SYNC_INTERVAL_SEC", "900"))  # Default: 15 minutes


class PanelSyncManager:
    """Production-grade multi-datacenter synchronization engine.
    Harmonizes client UUIDs, subIds, traffic quotas, expiry dates, and enabled states
    across Dual German Primary Datacenters (Main c23 + Bot c23)
    and Dual Domestic Bridges (Pars Online c13 Inbound 2 + Asiatech c11 Inbound 1)."""

    def __init__(
        self,
        german_panels=None,
        bridge_panels=None,
        primary=None,
        secondary=None,
        secondary_inbound_id: int = 2,
        alert_callback: Optional[Callable[[str], Any]] = None,
    ):
        if primary is not None or secondary is not None:
            self.german_panels = [primary] if primary else (german_panels or [conpanel_mgr, botpanel_mgr])
            self.bridge_panels = [(secondary, secondary_inbound_id)] if secondary else (bridge_panels or [(freshpanel_mgr, 2), (asiapanel_mgr, 1)])
        else:
            self.german_panels = german_panels or [conpanel_mgr, botpanel_mgr]
            self.bridge_panels = bridge_panels or [(freshpanel_mgr, 2), (asiapanel_mgr, 1)]

        self.alert_callback = alert_callback
        self.last_sync_time = 0.0
        self.last_sync_status: Dict[str, Any] = {
            "success": False,
            "added": 0,
            "updated": 0,
            "deleted": 0,
            "total_german": 0,
            "bridges": {},
            "last_error": None,
            "timestamp": None,
        }
        self._lock = threading.Lock()

    def sync_once(self) -> Dict[str, Any]:
        """Perform a single comprehensive synchronization and state reconciliation cycle across all datacenters."""
        with self._lock:
            result = {
                "success": False,
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "total_german": 0,
                "bridges": {},
                "last_error": None,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # 1. Authenticate German panels
            active_german = []
            for gp in self.german_panels:
                if gp.ensure_auth():
                    active_german.append(gp)
                else:
                    logger.warning("[Sync] German panel '%s' failed auth", gp.name)

            if not active_german:
                err_msg = "All German master panels failed authentication!"
                logger.error("[Sync] %s", err_msg)
                result["last_error"] = err_msg
                self.last_sync_status = result
                if self.alert_callback:
                    try:
                        self.alert_callback(f"⚠️ <b>هشدار هماهنگ‌سازی:</b> تمام سرورهای اصلی آلمان از دسترس خارج شدند.\n<code>{err_msg}</code>")
                    except Exception:
                        pass
                return result

            # 2. Collect master client catalog across German panels
            master_by_email: Dict[str, Dict[str, Any]] = {}
            for gp in active_german:
                try:
                    items = gp.export_clients()
                    for item in items:
                        cl = item.get("client")
                        if not cl or not cl.get("email"):
                            continue
                        em = cl["email"]
                        if em not in master_by_email:
                            master_by_email[em] = dict(cl)
                        else:
                            # Highest quota / latest expiry wins
                            cur = master_by_email[em]
                            cur["totalGB"] = max(cur.get("totalGB", 0), cl.get("totalGB", 0))
                            cur["expiryTime"] = max(cur.get("expiryTime", 0), cl.get("expiryTime", 0))
                            cur["enable"] = cur.get("enable", True) and cl.get("enable", True)
                except Exception as e:
                    logger.error("[Sync] Error exporting from %s: %s", gp.name, e)

            result["total_german"] = len(master_by_email)

            # 3. Synchronize to each domestic bridge panel
            fields_to_harmonize = ["id", "subId", "totalGB", "expiryTime", "enable", "limitHwid", "limitIp"]

            total_added = 0
            total_updated = 0
            total_deleted = 0

            for bridge, inbound_id in self.bridge_panels:
                bridge_name = bridge.name
                bridge_stat = {"added": 0, "updated": 0, "deleted": 0, "total": 0, "ok": False}
                result["bridges"][bridge_name] = bridge_stat

                if not bridge.ensure_auth():
                    logger.warning("[Sync] Bridge panel '%s' failed auth, skipping cycle", bridge_name)
                    continue

                try:
                    bridge_items = bridge.export_clients()
                    bridge_by_email = {
                        item["client"]["email"]: item["client"] for item in bridge_items if item.get("client", {}).get("email")
                    }
                    bridge_stat["total"] = len(bridge_by_email)

                    # A. Replicate missing clients into bridge
                    to_import = []
                    for email, m_client in master_by_email.items():
                        if email not in bridge_by_email:
                            to_import.append({
                                "client": dict(m_client),
                                "inboundIds": [inbound_id],
                            })

                    if to_import:
                        if bridge.import_clients(to_import):
                            cnt = len(to_import)
                            bridge_stat["added"] = cnt
                            total_added += cnt
                            logger.info("[Sync:Add] Replicated %d client(s) to %s (Inbound %d)", cnt, bridge_name, inbound_id)
                        else:
                            logger.error("[Sync:Add] Failed to import batch into %s", bridge_name)

                    # B. Harmonize modified attributes & quotas
                    b_updated = 0
                    for email, m_client in master_by_email.items():
                        if email in bridge_by_email:
                            b_client = bridge_by_email[email]
                            needs_update = False

                            for field in fields_to_harmonize:
                                if m_client.get(field) != b_client.get(field):
                                    needs_update = True
                                    break

                            if needs_update:
                                updated_payload = dict(b_client)
                                for f in fields_to_harmonize:
                                    if f in m_client:
                                        updated_payload[f] = m_client[f]

                                if bridge.update_client(email, updated_payload):
                                    b_updated += 1

                    bridge_stat["updated"] = b_updated
                    total_updated += b_updated

                    # C. Delete orphan clients (only tg_* bot clients removed from all German masters)
                    b_deleted = 0
                    for email, b_client in list(bridge_by_email.items()):
                        is_bot_client = email.startswith("tg_") or b_client.get("tgId", 0) > 0 or b_client.get("group") == "Customers"
                        if is_bot_client and email not in master_by_email:
                            if bridge.delete_client(email):
                                b_deleted += 1

                    bridge_stat["deleted"] = b_deleted
                    total_deleted += b_deleted
                    bridge_stat["ok"] = True

                except Exception as e_br:
                    logger.error("[Sync] Exception syncing to %s: %s", bridge_name, e_br)

            result["added"] = total_added
            result["updated"] = total_updated
            result["deleted"] = total_deleted
            result["success"] = True
            self.last_sync_time = time.time()
            self.last_sync_status = result

            if total_added > 0 or total_updated > 0 or total_deleted > 0:
                logger.info(
                    "[Sync Complete] Total German: %d | Added: %d, Updated: %d, Deleted: %d across %d bridges",
                    len(master_by_email), total_added, total_updated, total_deleted, len(self.bridge_panels)
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
