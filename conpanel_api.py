import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("ConpanelAPI")

script_dir = Path(__file__).parent.resolve()
load_dotenv(dotenv_path=script_dir / ".env")

# Primary Panel Config (Germany c23 Cluster)
DEFAULT_BASE = "https://conpanel.litontheix.ir/tK9mWq2ZxR7bN4vL"
DEFAULT_USER = "admin"
DEFAULT_PASS = "ConPanel-2026-x7Q"

CON_BASE = os.getenv("CONPANEL_URL", DEFAULT_BASE).rstrip("/")
CON_USER = os.getenv("CONPANEL_USER", DEFAULT_USER)
CON_PASS = os.getenv("CONPANEL_PASS", DEFAULT_PASS)
PUBLIC_DOMAIN = os.getenv("CONPANEL_DOMAIN", "conpanel.litontheix.ir")
BRIDGE_DOMAIN = os.getenv("CONPANEL_BRIDGE_DOMAIN", os.getenv("BRIDGE_DOMAIN", "conpanel.85-10-197-124.nip.io"))

# Secondary Panel Config (Domestic freshbridge c13 Cluster)
SECONDARY_BASE = (os.getenv("FRESHPANEL_URL") or os.getenv("SECONDARY_PANEL_URL") or "https://freshbridge.darkube.ir/panel").rstrip("/")
SECONDARY_USER = os.getenv("FRESHPANEL_USER") or os.getenv("SECONDARY_PANEL_USER") or "admin"
SECONDARY_PASS = os.getenv("FRESHPANEL_PASS") or os.getenv("SECONDARY_PANEL_PASS") or "nima1312560"


class ConpanelClient:
    """Production-grade 3X-UI panel API client with resilient connection pooling,
    automatic re-authentication, idempotent provisioning, and lifecycle management."""

    def __init__(self, base_url: str = CON_BASE, username: str = CON_USER, password: str = CON_PASS, name: str = "Primary"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.name = name

        self.session = requests.Session()
        self.session.trust_env = False
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=5, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.csrf_token = ""
        self.last_login = 0.0

    def _get_csrf_and_login(self) -> bool:
        """Authenticate with 3X-UI panel and acquire CSRF token + session cookie."""
        try:
            r_csrf = self.session.get(f"{self.base_url}/csrf-token", timeout=(5.0, 10.0))
            if r_csrf.status_code != 200:
                logger.error("[%s] Failed to fetch CSRF token (HTTP %d)", self.name, r_csrf.status_code)
                return False

            self.csrf_token = r_csrf.json().get("obj", "")
            headers = {"X-CSRF-Token": self.csrf_token}

            r_login = self.session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                headers=headers,
                timeout=(5.0, 10.0),
            )
            data = r_login.json()
            if r_login.status_code == 200 and data.get("success"):
                self.last_login = time.time()
                logger.info("[%s] Successfully authenticated to 3X-UI panel at %s", self.name, self.base_url)
                return True
            else:
                logger.error("[%s] 3X-UI login failed: %s", self.name, data.get("msg", "unknown error"))
                return False
        except Exception as e:
            logger.error("[%s] Exception during authentication: %s", self.name, e)
            return False

    def ensure_auth(self) -> bool:
        """Ensure active login session (refreshing token every 20 minutes)."""
        if time.time() - self.last_login < 1200 and self.csrf_token:
            return True
        return self._get_csrf_and_login()

    def get_client(self, email: str) -> Optional[Dict[str, Any]]:
        """Fetch client record and live usage stats by email."""
        if not self.ensure_auth():
            return None
        headers = {"X-CSRF-Token": self.csrf_token}
        try:
            r = self.session.get(
                f"{self.base_url}/panel/api/clients/get/{email}",
                headers=headers,
                timeout=(5.0, 10.0),
            )
            if r.status_code == 200 and r.json().get("success"):
                return r.json().get("obj", {}).get("client")
        except Exception as e:
            logger.error("[%s] Error fetching client '%s': %s", self.name, email, e)
        return None

    def export_clients(self) -> List[Dict[str, Any]]:
        """Export all clients and their assigned inbound IDs."""
        if not self.ensure_auth():
            return []
        headers = {"X-CSRF-Token": self.csrf_token}
        try:
            r = self.session.get(
                f"{self.base_url}/panel/api/clients/export",
                headers=headers,
                timeout=(5.0, 10.0),
            )
            if r.status_code == 200 and r.json().get("success"):
                return r.json().get("obj", [])
            elif r.status_code in (401, 403):
                self.last_login = 0.0  # Force re-auth next call
        except Exception as e:
            logger.error("[%s] Error exporting clients: %s", self.name, e)
        return []

    def import_clients(self, client_inbound_items: List[Dict[str, Any]]) -> bool:
        """Import a batch of clients into the panel."""
        if not client_inbound_items:
            return True
        if not self.ensure_auth():
            return False
        headers = {"X-CSRF-Token": self.csrf_token}
        payload = {"data": json.dumps(client_inbound_items)}
        try:
            r = self.session.post(
                f"{self.base_url}/panel/api/clients/import",
                json=payload,
                headers=headers,
                timeout=(5.0, 15.0),
            )
            return r.status_code == 200 and r.json().get("success", False)
        except Exception as e:
            logger.error("[%s] Error importing clients: %s", self.name, e)
            return False

    def update_client(self, email: str, client_payload: Dict[str, Any]) -> bool:
        """Update an existing client record in 3X-UI."""
        if not self.ensure_auth():
            return False
        headers = {"X-CSRF-Token": self.csrf_token}
        try:
            r = self.session.post(
                f"{self.base_url}/panel/api/clients/update/{email}",
                json=client_payload,
                headers=headers,
                timeout=(5.0, 10.0),
            )
            return r.status_code == 200 and r.json().get("success", False)
        except Exception as e:
            logger.error("[%s] Error updating client '%s': %s", self.name, email, e)
            return False

    def delete_client(self, email: str) -> bool:
        """Delete a client from the panel."""
        if not self.ensure_auth():
            return False
        headers = {"X-CSRF-Token": self.csrf_token}
        try:
            r = self.session.post(
                f"{self.base_url}/panel/api/clients/del/{email}",
                headers=headers,
                timeout=(5.0, 10.0),
            )
            return r.status_code == 200 and r.json().get("success", False)
        except Exception as e:
            logger.error("[%s] Error deleting client '%s': %s", self.name, email, e)
            return False

    def set_client_enabled(self, email: str, enabled: bool) -> bool:
        """Convenience method to enable or disable client (e.g. upon quota exhaustion)."""
        client = self.get_client(email)
        if not client:
            return False
        if client.get("enable") == enabled:
            return True
        client["enable"] = enabled
        return self.update_client(email, client)

    def create_customer_subscription(
        self,
        email: str,
        total_gb: int,
        expiry_days: int,
        limit_hwid: int = 1,
        tg_id: Optional[int] = None,
        inbound_id: int = 1,
        is_renewal: bool = False,
    ) -> Dict[str, Any]:
        """Create or idempotently renew a customer client record in 3X-UI.
        Returns complete subscription links for Base64, Sing-box JSON, Clash, and Bridge mirrors."""
        if not self.ensure_auth():
            return {"success": False, "error": "Could not authenticate to 3X-UI panel"}

        headers = {"X-CSRF-Token": self.csrf_token}
        now_ms = int(time.time() * 1000)
        total_bytes = int(total_gb * 1024 * 1024 * 1024)

        # Check if client already exists (Renewal vs New Slot)
        existing_client = self.get_client(email) if email else None

        if existing_client and is_renewal:
            # Idempotent renewal: extend expiry and increase/reset total quota
            client_uuid = existing_client.get("id")
            sub_id = existing_client.get("subId")
            existing_expiry = existing_client.get("expiryTime", 0)
            base_expiry = max(existing_expiry, now_ms)
            new_expiry = base_expiry + int(expiry_days * 86400 * 1000)
            new_total = existing_client.get("totalGB", 0) + total_bytes

            existing_client["expiryTime"] = new_expiry
            existing_client["totalGB"] = new_total
            existing_client["enable"] = True
            existing_client["limitHwid"] = max(existing_client.get("limitHwid", 1), limit_hwid)

            if self.update_client(email, existing_client):
                logger.info("[%s] Renewed client '%s': +%dGB, new_expiry=%d", self.name, email, total_gb, new_expiry)
                return self._build_sub_response(email, client_uuid, sub_id, total_gb, expiry_days, limit_hwid)
            else:
                return {"success": False, "error": "Failed to update existing client for renewal"}

        # Distinct new subscription slot
        if existing_client and not is_renewal:
            email = f"{email}_{secrets.token_hex(2)}"

        client_uuid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)  # 16-char hex subId

        if not email:
            if tg_id:
                base_email = f"tg_{tg_id}"
                check = self.get_client(base_email)
                email = f"tg_{tg_id}_{total_gb}GB_{secrets.token_hex(2)}" if check else base_email
            else:
                email = f"user_{sub_id[:8]}"

        expiry_ms = now_ms + int(expiry_days * 86400 * 1000)

        client_payload = {
            "id": client_uuid,
            "security": "",
            "email": email,
            "limitIp": 0,
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": int(tg_id) if tg_id else 0,
            "subId": sub_id,
            "group": "Customers",
            "comment": f"TG ID: {tg_id} | {total_gb}GB-{expiry_days}d | {datetime.now().strftime('%Y-%m-%d')}",
            "reset": 0,
            "resetDay": 0,
            "resetMax": 0,
            "trafficReset": "never",
            "trafficResetDay": 1,
            "limitHwid": limit_hwid,
        }

        import_body = {
            "data": json.dumps([
                {
                    "client": client_payload,
                    "inboundIds": [inbound_id],
                }
            ])
        }

        try:
            r = self.session.post(
                f"{self.base_url}/panel/api/clients/import",
                json=import_body,
                headers=headers,
                timeout=(5.0, 15.0),
            )
            res_data = r.json()
            if r.status_code == 200 and res_data.get("success"):
                logger.info("[%s] Successfully created client '%s' (UUID: %s, subId: %s)", self.name, email, client_uuid, sub_id)
                return self._build_sub_response(email, client_uuid, sub_id, total_gb, expiry_days, limit_hwid)
            else:
                logger.error("[%s] Client import returned failure: %s", self.name, res_data)
                return {"success": False, "error": res_data.get("msg", "Unknown error")}
        except Exception as e:
            logger.error("[%s] Exception creating client: %s", self.name, e)
            return {"success": False, "error": str(e)}

    def _build_sub_response(
        self,
        email: str,
        client_uuid: str,
        sub_id: str,
        total_gb: int,
        expiry_days: int,
        limit_hwid: int,
    ) -> Dict[str, Any]:
        """Construct response dictionary containing Base64, Sing-box JSON, Clash, and Bridge mirror links."""
        sub_url = f"https://{PUBLIC_DOMAIN}/sub/{sub_id}"
        json_url = f"https://{PUBLIC_DOMAIN}/json/{sub_id}"
        clash_url = f"https://{PUBLIC_DOMAIN}/clash/{sub_id}"
        bridge_url = f"https://{BRIDGE_DOMAIN}/sub/{sub_id}"
        bridge_json_url = f"https://{BRIDGE_DOMAIN}/json/{sub_id}"

        return {
            "success": True,
            "email": email,
            "uuid": client_uuid,
            "subId": sub_id,
            "sub_url": sub_url,
            "json_url": json_url,
            "clash_url": clash_url,
            "bridge_url": bridge_url,
            "bridge_json_url": bridge_json_url,
            "total_gb": total_gb,
            "expiry_days": expiry_days,
            "limit_hwid": limit_hwid,
        }


# Global singleton instances
conpanel_mgr = ConpanelClient(CON_BASE, CON_USER, CON_PASS, name="Primary-Germany")
freshpanel_mgr = ConpanelClient(SECONDARY_BASE, SECONDARY_USER, SECONDARY_PASS, name="Secondary-Domestic")
