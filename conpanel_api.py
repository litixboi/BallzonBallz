import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import requests
from dotenv import load_dotenv

logger = logging.getLogger("ConpanelAPI")

script_dir = Path(__file__).parent
load_dotenv(dotenv_path=script_dir / ".env")

DEFAULT_BASE = "https://conpanel.litontheix.ir/tK9mWq2ZxR7bN4vL"
DEFAULT_USER = "admin"
DEFAULT_PASS = "ConPanel-2026-x7Q"

CON_BASE = os.getenv("CONPANEL_URL", DEFAULT_BASE).rstrip("/")
CON_USER = os.getenv("CONPANEL_USER", DEFAULT_USER)
CON_PASS = os.getenv("CONPANEL_PASS", DEFAULT_PASS)
PUBLIC_DOMAIN = os.getenv("CONPANEL_DOMAIN", "conpanel.litontheix.ir")


class ConpanelClient:
    def __init__(self, base_url: str = CON_BASE, username: str = CON_USER, password: str = CON_PASS):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.trust_env = False
        self.csrf_token = ""
        self.last_login = 0.0

    def _get_csrf_and_login(self) -> bool:
        """Authenticate with 3x-ui panel and acquire CSRF token + session cookie."""
        try:
            r_csrf = self.session.get(f"{self.base_url}/csrf-token", timeout=10)
            if r_csrf.status_code != 200:
                logger.error("Failed to fetch CSRF token (HTTP %d)", r_csrf.status_code)
                return False

            self.csrf_token = r_csrf.json().get("obj", "")
            headers = {"X-CSRF-Token": self.csrf_token}

            r_login = self.session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                headers=headers,
                timeout=10,
            )
            data = r_login.json()
            if r_login.status_code == 200 and data.get("success"):
                self.last_login = time.time()
                logger.info("Successfully authenticated to Conpanel 3x-ui at %s", self.base_url)
                return True
            else:
                logger.error("Conpanel login failed: %s", data.get("msg", "unknown error"))
                return False
        except Exception as e:
            logger.error("Exception during Conpanel authentication: %s", e)
            return False

    def ensure_auth(self) -> bool:
        """Ensure active login session (refreshing every 20 minutes)."""
        if time.time() - self.last_login < 1200 and self.csrf_token:
            return True
        return self._get_csrf_and_login()

    def create_customer_subscription(
        self,
        email: str,
        total_gb: int,
        expiry_days: int,
        limit_hwid: int = 1,
        tg_id: Optional[int] = None,
        inbound_id: int = 1,
    ) -> Dict[str, Any]:
        """Create a client record in 3x-ui and return subscription links.

        Args:
            email: Unique client identifier (e.g. tg_12345678_30gb)
            total_gb: Traffic limit in gigabytes (e.g. 30, 60, 100)
            expiry_days: Validity duration in days
            limit_hwid: Max concurrent hardware devices (1, 2, 3)
            tg_id: Customer's Telegram user ID
            inbound_id: Inbound ID (default: 1, in-8080-tcp edge VLESS)
        """
        if not self.ensure_auth():
            return {"success": False, "error": "Could not authenticate to panel"}

        headers = {"X-CSRF-Token": self.csrf_token}
        client_uuid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)  # 16-char hex

        now_ms = int(time.time() * 1000)
        expiry_ms = now_ms + int(expiry_days * 86400 * 1000)
        total_bytes = int(total_gb * 1024 * 1024 * 1024)

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
            "comment": f"Bought via Bot | {datetime.now().strftime('%Y-%m-%d')}",
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
                timeout=12,
            )
            res_data = r.json()
            if r.status_code == 200 and res_data.get("success"):
                logger.info("Successfully created client '%s' (UUID: %s, subId: %s)", email, client_uuid, sub_id)
                sub_url = f"https://{PUBLIC_DOMAIN}/sub/{sub_id}"
                json_url = f"https://{PUBLIC_DOMAIN}/json/{sub_id}"
                clash_url = f"https://{PUBLIC_DOMAIN}/clash/{sub_id}"
                return {
                    "success": True,
                    "email": email,
                    "uuid": client_uuid,
                    "subId": sub_id,
                    "sub_url": sub_url,
                    "json_url": json_url,
                    "clash_url": clash_url,
                    "total_gb": total_gb,
                    "expiry_days": expiry_days,
                    "limit_hwid": limit_hwid,
                }
            else:
                logger.error("Client import returned failure: %s", res_data)
                return {"success": False, "error": res_data.get("msg", "Unknown error")}
        except Exception as e:
            logger.error("Exception creating client: %s", e)
            return {"success": False, "error": str(e)}

    def get_client(self, email: str) -> Optional[Dict[str, Any]]:
        """Fetch client record and live usage stats."""
        if not self.ensure_auth():
            return None
        headers = {"X-CSRF-Token": self.csrf_token}
        try:
            r = self.session.get(f"{self.base_url}/panel/api/clients/get/{email}", headers=headers, timeout=10)
            if r.status_code == 200 and r.json().get("success"):
                return r.json().get("obj", {}).get("client")
        except Exception as e:
            logger.error("Error fetching client '%s': %s", email, e)
        return None


# Global singleton instance
conpanel_mgr = ConpanelClient()
