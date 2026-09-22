import base64
import contextlib
import functools
import html
import itertools
import json
import logging
import os
import queue
import random
import re
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from urllib.parse import parse_qs, quote, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
import geoip2.database

import crypto_manager
from conpanel_api import conpanel_mgr
from order_manager import order_mgr
import persian_announcements

# --- FORCE IPv4 GLOBALLY TO PREVENT [Errno 101] Network is unreachable ON CLOUD HOSTS ---
import urllib3.util.connection as urllib3_conn

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo
urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

# --- LOGGING SETUP (timestamps + levels, ready for Railway logs) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ConfigBot")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("TeleBot").setLevel(logging.WARNING)

# Configure telebot apihelper with connection pooling & retries
import telebot.apihelper as apihelper
apihelper.RETRY_ON_ERROR = True
apihelper.CONNECT_TIMEOUT = 15
apihelper.READ_TIMEOUT = 30

telebot_session = requests.Session()
_telebot_adapter = HTTPAdapter(
    max_retries=Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
)
telebot_session.mount("https://", _telebot_adapter)
telebot_session.mount("http://", _telebot_adapter)
apihelper.session = telebot_session

# --- CONTEXT-AWARE CONFIGURATION & ENV LOADING ---
script_dir = Path(__file__).parent
logger.info("Workspace active directory: %s", script_dir)

load_dotenv(dotenv_path=script_dir / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@litixconnect")
UPDATE_INTERVAL_HOURS = max(1, int(os.getenv("UPDATE_INTERVAL_HOURS", "12")))
NODE_TEST_WORKERS = max(1, int(os.getenv("NODE_TEST_WORKERS", "12")))
bot_username = None

PLANS_FILE = script_dir / "plans_config.json"
user_pending_tx_order = {}  # chat_id -> {"order_id": str, "timestamp": float}


ADMIN_REGISTRY_FILE = script_dir / "admin_chat_registry.json"
DEFAULT_ADMIN_USERNAMES = {"awlinavakhtam"}


def _load_admin_registry() -> dict:
    if ADMIN_REGISTRY_FILE.exists():
        try:
            return json.loads(ADMIN_REGISTRY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ids": [], "usernames_to_ids": {}}


def _save_admin_registry(data: dict):
    try:
        ADMIN_REGISTRY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save admin registry: %s", e)


def get_admin_targets() -> dict:
    """Returns {'ids': set[str], 'usernames': set[str]} from env and defaults."""
    raw = (os.getenv("ADMIN_CHAT_ID") or "").strip()
    if not raw:
        env_file = script_dir / ".env"
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=True)
            raw = (os.getenv("ADMIN_CHAT_ID") or "").strip()

    usernames = set(DEFAULT_ADMIN_USERNAMES)
    ids = set()

    if raw:
        for item in raw.replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            if "t.me/" in item:
                item = item.split("t.me/")[-1].split("/")[0].strip()
            item_clean = item.lstrip("@").lower()
            if item.isdigit() or (item.startswith("-") and item[1:].isdigit()):
                ids.add(str(item))
            elif item_clean:
                usernames.add(item_clean)

    return {"ids": ids, "usernames": usernames}


def register_admin_chat(user_id: Optional[int], username: Optional[str] = None):
    """If user matches configured admin username or numeric ID, register their chat ID."""
    if not user_id:
        return
    username_clean = (username or "").strip().lstrip("@").lower()
    targets = get_admin_targets()

    is_match = False
    if str(user_id) in targets["ids"]:
        is_match = True
    if username_clean and username_clean in targets["usernames"]:
        is_match = True

    if is_match:
        registry = _load_admin_registry()
        changed = False
        if int(user_id) not in registry.get("ids", []):
            registry.setdefault("ids", []).append(int(user_id))
            changed = True
        if username_clean:
            registry.setdefault("usernames_to_ids", {})
            if registry["usernames_to_ids"].get(username_clean) != int(user_id):
                registry["usernames_to_ids"][username_clean] = int(user_id)
                changed = True
        if changed:
            _save_admin_registry(registry)
            logger.info("Registered admin chat: user_id=%s, username=@%s", user_id, username_clean)


def get_admin_chat_ids() -> list[int]:
    """Retrieve verified integer admin chat IDs for sending alerts."""
    targets = get_admin_targets()
    registry = _load_admin_registry()
    chat_ids = set()

    for id_str in targets["ids"]:
        try:
            chat_ids.add(int(id_str))
        except ValueError:
            pass

    for reg_id in registry.get("ids", []):
        try:
            chat_ids.add(int(reg_id))
        except (ValueError, TypeError):
            pass

    return list(chat_ids)


def is_admin(user_or_id, username: Optional[str] = None) -> bool:
    """Strict check for admin authorization. Fails CLOSED.
    Accepts telebot User object, int/str user ID, and optional username."""
    if not user_or_id:
        return False

    user_id = None
    u_name = None

    if hasattr(user_or_id, "id"):
        user_id = str(user_or_id.id)
        u_name = getattr(user_or_id, "username", None)
    elif isinstance(user_or_id, (int, str)):
        val = str(user_or_id).strip()
        if "t.me/" in val:
            val = val.split("t.me/")[-1].split("/")[0].strip()
        val_clean = val.lstrip("@").lower()
        if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
            user_id = val
        else:
            u_name = val_clean

    if username and not u_name:
        u_name = str(username).strip().lstrip("@").lower()
    elif u_name:
        u_name = str(u_name).strip().lstrip("@").lower()

    targets = get_admin_targets()

    # Check numeric ID match
    if user_id and user_id in targets["ids"]:
        if user_id.isdigit():
            register_admin_chat(int(user_id), u_name)
        return True

    # Check username match (e.g. awlinavakhtam)
    if u_name and u_name in targets["usernames"]:
        if user_id and user_id.isdigit():
            register_admin_chat(int(user_id), u_name)
        return True

    # Check registry mappings
    registry = _load_admin_registry()
    if user_id and int(user_id) in registry.get("ids", []):
        return True

    return False


def get_pending_tx_order(chat_id: int) -> Optional[str]:
    """Get pending order_id if not expired (30 minute TTL)."""
    data = user_pending_tx_order.get(chat_id)
    if not data:
        return None
    if isinstance(data, dict):
        if time.time() - data.get("timestamp", 0) > 1800:
            user_pending_tx_order.pop(chat_id, None)
            return None
        return data.get("order_id")
    return str(data)


def get_vip_plans():
    try:
        if PLANS_FILE.exists():
            return json.loads(PLANS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load plans_config.json: %s", e)
    return []


if not BOT_TOKEN:
    raise ValueError("❌ Error: BOT_TOKEN is missing! Check your .env file.")

bot = telebot.TeleBot(BOT_TOKEN)

# --- MAXMIND LOCAL DATABASE INITIALIZATION ---
MMDB_PATH = script_dir / "GeoLite2-Country.mmdb"
if not MMDB_PATH.exists():
    raise FileNotFoundError(
        f"❌ Missing local database file! Please place 'GeoLite2-Country.mmdb' here: {MMDB_PATH}"
    )

geo_reader = geoip2.database.Reader(str(MMDB_PATH))

# --- HTTP SESSION WITH RETRY/BACKOFF (survives GitHub blips) ---
http_session = requests.Session()
try:
    _retry_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
except TypeError:  # very old urllib3 without allowed_methods
    _retry_policy = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
http_session.mount("https://", HTTPAdapter(max_retries=_retry_policy))

# --- ORIGINAL SOURCES (direct GitHub first, mirror as automatic fallback) ---
SOURCES = [
    "https://raw.githubusercontent.com/wenxig/free-nodes-sub/main/data/sub.txt",
    "https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/main/Z.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/V2RAY.txt",
]
MIRROR_PREFIX = "https://ghproxy.net/"


def http_get(url, timeout=10):
    """GET with retry/backoff; falls back to the ghproxy mirror if raw GitHub is unreachable."""
    try:
        return http_session.get(url, timeout=timeout)
    except Exception as e:
        if "raw.githubusercontent.com/" in url:
            logger.warning("Direct fetch failed (%s) - retrying via ghproxy mirror", e)
            return http_session.get(MIRROR_PREFIX + url, timeout=timeout)
        raise


# --- AU1RXX GITHUB SOURCE (Country-specific v2ray configs) ---
AU1RXX_BASE = "https://raw.githubusercontent.com/Au1rxx/free-vpn-subscriptions/main/output/country"
AU1RXX_PARTS = 5  # fetch v2ray-base64-0001.txt ... -0005.txt, stop at the first 404
AU1RXX_COUNTRIES = {
    "DE": "Germany",
    "NL": "Netherlands",
    "SE": "Sweden",
    "US": "United States",
    "TR": "Turkey",
    "FR": "France",
    "JP": "Japan",
    "SG": "Singapore",
    "CA": "Canada",
    "GB": "United Kingdom",
    "AU": "Australia",
    "CH": "Switzerland",
    "HK": "Hong Kong",
    "KR": "South Korea",
    "BR": "Brazil",
    "IN": "India",
    "PL": "Poland",
    "RO": "Romania",
    "FI": "Finland",
    "NO": "Norway",
    "DK": "Denmark",
    "AT": "Austria",
    "BE": "Belgium",
    "IE": "Ireland",
    "ES": "Spain",
    "IT": "Italy",
    "CZ": "Czech Republic",
    "PT": "Portugal",
    "MX": "Mexico",
    "AR": "Argentina",
    "ZA": "South Africa",
    "AE": "United Arab Emirates"
}

# --- COUNTRY DATA FOR UI & REBRANDING ---
COUNTRY_DATA = {
    "Germany": {"abbrev": "DE", "flag": "🇩🇪", "code": "DE"},
    "Netherlands": {"abbrev": "NL", "flag": "🇳🇱", "code": "NL"},
    "Sweden": {"abbrev": "SE", "flag": "🇸🇪", "code": "SE"},
    "United States": {"abbrev": "US", "flag": "🇺🇸", "code": "US"},
    "Turkey": {"abbrev": "TR", "flag": "🇹🇷", "code": "TR"},
    "France": {"abbrev": "FR", "flag": "🇫🇷", "code": "FR"},
    "Japan": {"abbrev": "JP", "flag": "🇯🇵", "code": "JP"},
    "Singapore": {"abbrev": "SG", "flag": "🇸🇬", "code": "SG"},
    "Canada": {"abbrev": "CA", "flag": "🇨🇦", "code": "CA"},
    "United Kingdom": {"abbrev": "GB", "flag": "🇬🇧", "code": "GB"},
    "Australia": {"abbrev": "AU", "flag": "🇦🇺", "code": "AU"},
    "Switzerland": {"abbrev": "CH", "flag": "🇨🇭", "code": "CH"},
    "Hong Kong": {"abbrev": "HK", "flag": "🇭🇰", "code": "HK"},
    "South Korea": {"abbrev": "KR", "flag": "🇰🇷", "code": "KR"},
    "Brazil": {"abbrev": "BR", "flag": "🇧🇷", "code": "BR"},
    "India": {"abbrev": "IN", "flag": "🇮🇳", "code": "IN"},
    "Poland": {"abbrev": "PL", "flag": "🇵🇱", "code": "PL"},
    "Romania": {"abbrev": "RO", "flag": "🇷🇴", "code": "RO"},
    "Finland": {"abbrev": "FI", "flag": "🇫🇮", "code": "FI"},
    "Norway": {"abbrev": "NO", "flag": "🇳🇴", "code": "NO"},
    "Denmark": {"abbrev": "DK", "flag": "🇩🇰", "code": "DK"},
    "Austria": {"abbrev": "AT", "flag": "🇦🇹", "code": "AT"},
    "Belgium": {"abbrev": "BE", "flag": "🇧🇪", "code": "BE"},
    "Ireland": {"abbrev": "IE", "flag": "🇮🇪", "code": "IE"},
    "Spain": {"abbrev": "ES", "flag": "🇪🇸", "code": "ES"},
    "Italy": {"abbrev": "IT", "flag": "🇮🇹", "code": "IT"},
    "Czech Republic": {"abbrev": "CZ", "flag": "🇨🇿", "code": "CZ"},
    "Portugal": {"abbrev": "PT", "flag": "🇵🇹", "code": "PT"},
    "Mexico": {"abbrev": "MX", "flag": "🇲🇽", "code": "MX"},
    "Argentina": {"abbrev": "AR", "flag": "🇦🇷", "code": "AR"},
    "South Africa": {"abbrev": "ZA", "flag": "🇿🇦", "code": "ZA"},
    "United Arab Emirates": {"abbrev": "AE", "flag": "🇦🇪", "code": "AE"},
    "Others": {"abbrev": "XX", "flag": "🌐", "code": "XX"}
}

# Build button mappings
BUTTON_TO_COUNTRY = {v: v for v in COUNTRY_DATA.keys() if v != "Others"}
BUTTON_TO_COUNTRY["Others"] = "Others"

categorized_nodes = {k: [] for k in BUTTON_TO_COUNTRY.keys()}
nodes_lock = threading.Lock()
user_session_offsets = {}
offsets_lock = threading.Lock()
last_update_time = None  # human-readable UTC string of the last successful scan

MAX_TRACKED_USERS = 2000   # cap on user_session_offsets to bound memory
UPDATE_INTERVAL = UPDATE_INTERVAL_HOURS * 3600  # aligned to UTC hour boundaries (default: 12h)
STATE_PATH = script_dir / "bot_state.json"
TOP_PICKS_COUNTRIES = 5      # quick-picks file covers the countries with the most live configs
TOP_PICKS_PER_COUNTRY = 50   # random picks per top country, never two on the same address


# --- STATE PERSISTENCE (survives process restarts) ---
def save_state():
    """Persist cache + rotation state so a restart doesn't start cold."""
    try:
        with offsets_lock:
            offsets_snapshot = {str(k): v for k, v in user_session_offsets.items()}
        with nodes_lock:
            nodes_snapshot = {k: list(v) for k, v in categorized_nodes.items()}
        state = {"nodes": nodes_snapshot, "offsets": offsets_snapshot, "last_update": last_update_time}
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


def load_state():
    """Restore the cache + rotation state written by a previous run."""
    global categorized_nodes, user_session_offsets, last_update_time
    try:
        if not STATE_PATH.exists():
            return
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        nodes = state.get("nodes") or {}
        with nodes_lock:
            for key in categorized_nodes:
                if isinstance(nodes.get(key), list):
                    categorized_nodes[key] = [l for l in nodes[key] if isinstance(l, str)]
            total = sum(len(v) for v in categorized_nodes.values())
        offsets = state.get("offsets") or {}
        with offsets_lock:
            for chat_id, per_country in offsets.items():
                if isinstance(per_country, dict):
                    try:
                        user_session_offsets[int(chat_id)] = {
                            k: v for k, v in per_country.items() if isinstance(v, int)
                        }
                    except ValueError:
                        continue
        last_update_time = state.get("last_update")
        logger.info("Restored state: %d configs, %d users, last update %s",
                    total, len(user_session_offsets), last_update_time)
    except Exception as e:
        logger.warning("Failed to load state: %s", e)


def prune_offsets():
    """Drop the oldest tracked users if the rotation table grows unbounded."""
    with offsets_lock:
        excess = len(user_session_offsets) - MAX_TRACKED_USERS
        if excess > 0:
            for chat_id in list(user_session_offsets.keys())[:excess]:
                del user_session_offsets[chat_id]
            logger.info("Pruned %d stale user rotation entries", excess)


# --- REAL NODE VERIFICATION (Xray-core SOCKS handshake) ---
# An open TCP port says nothing: Cloudflare-fronted addresses, honeypots and
# half-dead nodes all accept connections. To know a config actually proxies
# traffic we run it through the real Xray-core binary as a local SOCKS proxy
# and fetch a connectivity-check URL through it - the same path a real
# client takes. Nodes that cannot carry an HTTP request are dropped.

XRAY_DIR = script_dir / "xray_bin"
XRAY_VERSION_FALLBACK = "v26.3.27"  # known-good STABLE release if 'latest' lookup fails
XRAY_DL_PREFIXES = [  # tried in order; mirrors dodge GitHub rate-limits on shared egress IPs
    "https://github.com/",
    "https://ghproxy.net/https://github.com/",
    "https://gh-proxy.com/https://github.com/",
]
XRAY_SETUP_FAILED_UNTIL = 0.0  # monotonic time before which binary setup is skipped (negative cache)
CONNECTIVITY_URLS = ["http://cp.cloudflare.com/generate_204", "http://www.gstatic.com/generate_204"]

# Thread-safe reusable port pool for verifier SOCKS inbounds (prevents >65535 integer overflow)
_port_pool = queue.Queue()
for _p in range(10001, 10001 + NODE_TEST_WORKERS * 3):
    _port_pool.put(_p)


@contextlib.contextmanager
def acquire_socks_port():
    """Borrow an available port from the pool and return it when done."""
    port = _port_pool.get()
    try:
        yield port
    finally:
        _port_pool.put(port)


def cleanup_xray_temp_files():
    """Remove any orphan temporary config files left in xray_bin from previous runs."""
    try:
        if XRAY_DIR.exists():
            for tmp_file in XRAY_DIR.glob("tmp*.json"):
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
    except Exception as e:
        logger.debug("Failed to clean up xray temp files: %s", e)


_xray_setup_lock = threading.Lock()
_xray_ready = False
XRAY_EXE = None  # set by ensure_xray_binary(); None = degraded TCP-only mode


def ensure_xray_binary():
    """Download + cache the Xray-core binary once per cold start.

    Returns the path to the executable, or None when setup failed. Failed
    setups are remembered for 10 minutes (negative cache) so ~4000 worker
    threads don't each re-attempt a 20 MB download in the same scan.
    """
    global XRAY_EXE, _xray_ready, XRAY_SETUP_FAILED_UNTIL
    exe_name = "xray.exe" if os.name == "nt" else "xray"
    exe_path = XRAY_DIR / exe_name
    if exe_path.exists():
        _xray_ready = True
        XRAY_EXE = exe_path
        return exe_path

    now = time.monotonic()
    if now < XRAY_SETUP_FAILED_UNTIL:
        return None  # recently failed; don't hammer the download again this scan

    with _xray_setup_lock:
        if exe_path.exists():
            _xray_ready = True
            XRAY_EXE = exe_path
            return exe_path
        if time.monotonic() < XRAY_SETUP_FAILED_UNTIL:
            return None
        try:
            XRAY_DIR.mkdir(parents=True, exist_ok=True)
            # resolve the latest stable release tag (pre-releases excluded)
            tag = XRAY_VERSION_FALLBACK
            try:
                api = http_get("https://api.github.com/repos/XTLS/Xray-core/releases/latest",
                               timeout=10)
                if api.status_code == 200:
                    latest = api.json().get("tag_name")
                    if latest and not api.json().get("prerelease"):
                        tag = latest
            except Exception:
                pass
            asset = "Xray-windows-64.zip" if os.name == "nt" else "Xray-linux-64.zip"
            zip_path = XRAY_DIR / "xray.zip"
            dl_errors = []
            content = None
            for prefix in XRAY_DL_PREFIXES:
                try:
                    res = http_session.get(f"{prefix}XTLS/Xray-core/releases/download/{tag}/{asset}",
                                           timeout=180)
                    if res.status_code == 200 and len(res.content) > 1_000_000:
                        content = res.content
                        break
                    dl_errors.append(f"{prefix} HTTP {res.status_code}")
                except Exception as e:
                    dl_errors.append(f"{prefix} {type(e).__name__}")
            if content is None:
                raise RuntimeError("download failed: " + "; ".join(dl_errors))
            zip_path.write_bytes(content)
            with zipfile.ZipFile(zip_path) as zf:
                member = exe_name if exe_name in zf.namelist() else zf.namelist()[0]
                zf.extract(member, XRAY_DIR)
                if member != exe_name:
                    (XRAY_DIR / member).rename(exe_path)
                if "geoip.dat" in zf.namelist():
                    zf.extract("geoip.dat", XRAY_DIR)
            zip_path.unlink()
            if os.name != "nt":
                exe_path.chmod(0o755)
            # sanity check: binary must run and report a version
            out = subprocess.run([str(exe_path), "version"], capture_output=True, timeout=15)
            if out.returncode != 0:
                raise RuntimeError("xray version check failed")
            _xray_ready = True
            XRAY_EXE = exe_path
            logger.info("Xray-core binary ready at %s (tag %s)", exe_path, tag)
            return exe_path
        except Exception as e:
            XRAY_SETUP_FAILED_UNTIL = time.monotonic() + 600  # retry in 10 minutes
            logger.warning("Xray binary setup failed (%s) - node verification degraded until %s",
                           e, time.strftime("%H:%M:%S", time.localtime(time.time() + 600)))
            return None


def _b64pad(data):
    """Standard-pad base64 of any variant (also handles urlsafe alphabets)."""
    data = data.replace("-", "+").replace("_", "/")
    return data + "=" * ((4 - len(data) % 4) % 4)


def parse_host_port(hostport):
    """Parse 'host:port' or '[v6]:port'. Returns (host, port) or (None, None)."""
    hostport = hostport.strip()
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0:
            return None, None
        host, rest = hostport[1:end], hostport[end + 1:]
        if not rest.startswith(":"):
            return None, None
        port = rest[1:]
    else:
        if ":" not in hostport:
            return None, None
        host, port = hostport.rsplit(":", 1)
    try:
        return host, int(port)
    except ValueError:
        return None, None


def parse_vmess_to_outbound(line):
    """vmess://<base64 JSON> -> Xray outbound dict, or None if unusable."""
    try:
        data = json.loads(base64.b64decode(_b64pad(line[8:])).decode("utf-8"))
        if not isinstance(data, dict):
            return None
        address = str(data.get("add") or "").strip()
        if not address or not str(data.get("id") or "").strip():
            return None
        port = int(data.get("port"))
        if not (0 < port < 65536):
            return None
        net = str(data.get("net") or "tcp").lower()
        if net == "http":
            net = "h2"
        security = str(data.get("tls") or "").lower()
        stream = {"network": net, "security": "none"}
        if security in ("tls", "reality"):
            stream["security"] = "tls"
            sni = str(data.get("sni") or data.get("host") or "").strip()
            if sni:
                stream["tlsSettings"] = {"serverName": sni}
            fp = str(data.get("fp") or "").strip()
            if fp:
                stream["tlsSettings"]["fingerprint"] = fp
        if net == "ws":
            ws = {"path": str(data.get("path") or "/")}
            host_header = str(data.get("host") or "").strip()
            if host_header:
                ws["headers"] = {"Host": host_header}
            stream["wsSettings"] = ws
        elif net == "h2":
            h2 = {"path": str(data.get("path") or "/")}
            host_header = str(data.get("host") or "").strip()
            if host_header:
                h2["host"] = [host_header]
            stream["httpSettings"] = h2
        elif net == "grpc":
            stream["grpcSettings"] = {"serviceName": str(data.get("path") or "")}
        elif net == "httpupgrade":
            stream["httpupgradeSettings"] = {"path": str(data.get("path") or "/")}

        return {
            "tag": "test",
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": address,
                    "port": port,
                    "users": [{"id": str(data.get("id")), "alterId": int(data.get("aid") or 0)}],
                }],
            },
            "streamSettings": stream,
        }
    except Exception:
        return None


def parse_vless_trojan_to_outbound(line, proto):
    """vless:// or trojan:// URI -> Xray outbound dict, or None if unusable."""
    try:
        rest = unquote(line.split("://", 1)[1])
        if "#" in rest:
            rest = rest.split("#", 1)[0]  # strip remark/fragment
        hostpart = rest.split("@", 1)[1] if "@" in rest else rest
        hostpart = hostpart.split("?", 1)[0]
        host, port = parse_host_port(hostpart)
        if not host or port is None:
            return None
        userinfo = rest.split("@", 1)[0]
        if not userinfo:
            return None
        params = parse_qs(rest.split("?", 1)[1]) if "?" in rest else {}
        params = {k.lower(): v[-1] for k, v in params.items()}

        net = (params.get("type") or "tcp").lower()
        if net == "http":
            net = "h2"
        security = (params.get("security") or "none").lower()
        stream = {"network": net, "security": "none"}
        if security in ("tls", "reality"):
            stream["security"] = "tls"
            tls = {"serverName": params.get("sni") or host}
            if params.get("fp"):
                tls["fingerprint"] = params.get("fp")
            if security == "reality":
                if not params.get("pbk") or not params.get("sid"):
                    return None  # reality without keys can't work
                tls["realitySettings"] = {
                    "show": False,
                    "fingerprint": params.get("fp") or "chrome",
                    "serverName": params.get("sni") or host,
                    "publicKey": params.get("pbk"),
                    "shortId": params.get("sid") or "",
                    "spiderX": params.get("spx") or "",
                }
            stream["tlsSettings"] = tls
        if net == "ws":
            ws = {"path": unquote(params.get("path") or "/")}
            if params.get("host"):
                ws["headers"] = {"Host": params.get("host")}
            stream["wsSettings"] = ws
        elif net == "h2":
            h2 = {"path": unquote(params.get("path") or "/")}
            if params.get("realIP") or params.get("host"):
                h2["host"] = [params.get("realIP") or params.get("host")]
            stream["httpSettings"] = h2
        elif net == "grpc":
            stream["grpcSettings"] = {"serviceName": unquote(params.get("serviceName") or "")}
        elif net == "httpupgrade":
            stream["httpupgradeSettings"] = {"path": unquote(params.get("path") or "/")}

        if proto == "vless":
            return {
                "tag": "test",
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": host,
                        "port": port,
                        "users": [{
                            "id": userinfo,
                            "encryption": "none",
                            "flow": params.get("flow") or "",
                        }],
                    }],
                },
                "streamSettings": stream,
            }
        return {
            "tag": "trojan",
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "password": unquote(userinfo),
                }],
            },
            "streamSettings": stream,
        }
    except Exception:
        return None


def parse_ss_to_outbound(line):
    """ss:// URI (plain or legacy whole-payload base64) -> Xray outbound, or None."""
    try:
        rest = line.split("://", 1)[1]
        if "#" in rest:
            rest = rest.split("#", 1)[0]  # strip remark/fragment
        if "@" in rest:
            userinfo, hostport = rest.rsplit("@", 1)
            hostport = hostport.split("?", 1)[0]
            host, port = parse_host_port(hostport)
            if not host or port is None:
                return None
            if ":" in userinfo:
                method, password = userinfo.split(":", 1)
                method, password = unquote(method), unquote(password)
            else:
                decoded = base64.b64decode(_b64pad(unquote(userinfo))).decode("utf-8")
                method, password = decoded.split(":", 1)
        else:
            # legacy: whole payload after ss:// is base64(method:password@host:port)
            decoded = base64.b64decode(_b64pad(rest.split("?", 1)[0])).decode("utf-8")
            if "@" not in decoded:
                return None
            userinfo, hostport = decoded.rsplit("@", 1)
            if ":" not in userinfo:
                return None
            method, password = userinfo.split(":", 1)
            host, port = parse_host_port(hostport)
            if not host or port is None:
                return None
        method = method.strip()
        if not method or not password:
            return None
        if "plugin=" in line:
            return None  # obfs plugins (simple-obfs, v2ray-plugin) aren't supported by Xray
        return {
            "tag": "test",
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "method": method,
                    "password": password,
                }],
            },
            "streamSettings": {"network": "tcp", "security": "none"},
        }
    except Exception:
        return None


def parse_config_to_outbound(line):
    scheme = line.split("://", 1)[0].lower()
    if scheme == "vmess":
        return parse_vmess_to_outbound(line)
    if scheme == "vless":
        return parse_vless_trojan_to_outbound(line, "vless")
    if scheme == "trojan":
        return parse_vless_trojan_to_outbound(line, "trojan")
    if scheme == "ss":
        return parse_ss_to_outbound(line)
    return None


def verify_node(line):
    """Run one config through real Xray-core and fetch a URL through it.

    Returns (ok: bool, latency_ms: int | None); (False, None) = untestable line.
    """
    outbound = parse_config_to_outbound(line)
    if outbound is None:
        return False, None
    exe = XRAY_EXE or ensure_xray_binary()
    if exe is None:
        # binary unavailable and this worker was reached anyway: nothing trustworthy
        # can be said about the node; the scan loop already refused to publish.
        return False, None

    # Cheap pre-gate: if the TCP port is closed there is no point spawning a
    # whole Xray instance just to watch it time out. Costs 2s max instead of 10-15s.
    # Pull address/port straight from the parsed outbound (same authority the
    # test config uses) - avoids the legacy regex that breaks on IPv6.
    try:
        if outbound.get("protocol") in ("vmess", "vless"):
            host = outbound["settings"]["vnext"][0]["address"]
            port = int(outbound["settings"]["vnext"][0]["port"])
        else:
            host = outbound["settings"]["servers"][0]["address"]
            port = int(outbound["settings"]["servers"][0]["port"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except OSError:
        return False, None

    with acquire_socks_port() as port:
        test_cfg = {
            "log": {"loglevel": "error"},
            "inbounds": [{
                "tag": "in0",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }],
            "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
            "routing": {"rules": [{"type": "field", "inboundTag": ["in0"], "outboundTag": "test"}]},
        }
        cfg_path = None
        proc = None
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                              encoding="utf-8", dir=str(XRAY_DIR)) as f:
                json.dump(test_cfg, f)
                cfg_path = Path(f.name)
            proc = subprocess.Popen([str(exe), "run", "-c", str(cfg_path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=creationflags)
            # wait for the SOCKS port to come up (xray binds almost instantly)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                return False, None
            start = time.monotonic()
            for url in CONNECTIVITY_URLS:
                try:
                    res = requests.get(url, proxies={
                        "http": f"socks5h://127.0.0.1:{port}",
                        "https": f"socks5h://127.0.0.1:{port}",
                    }, timeout=(5, 5))
                    # 2xx/3xx through the tunnel = the node really proxies traffic.
                    # Any other status means the tunnel itself is alive but the
                    # exit rejected the request - keep trying the other URLs.
                    if res.status_code < 300:
                        return True, int((time.monotonic() - start) * 1000)
                except Exception:
                    continue
            return False, None
        except Exception:
            return False, None
        finally:
            if proc is not None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            if cfg_path is not None:
                try:
                    cfg_path.unlink()
                except OSError:
                    pass


# --- UTILITY PARSING AND TESTING PIPELINES ---
def extract_host_and_port(config_line):
    """Extract (host, port) from any vmess/vless/ss/trojan URI with full IPv6 support."""
    try:
        config_line = config_line.strip()
        scheme = config_line.split("://", 1)[0].lower() if "://" in config_line else ""
        if scheme == "vmess":
            b64_data = _b64pad(config_line[8:].strip())
            data = json.loads(base64.b64decode(b64_data).decode('utf-8'))
            add = data.get("add")
            port = data.get("port")
            if add and port:
                return str(add).strip(), int(port)
        elif scheme in ("vless", "trojan"):
            rest = unquote(config_line.split("://", 1)[1])
            if "#" in rest:
                rest = rest.split("#", 1)[0]
            hostpart = rest.split("@", 1)[1] if "@" in rest else rest
            hostpart = hostpart.split("?", 1)[0]
            return parse_host_port(hostpart)
        elif scheme == "ss":
            rest = config_line.split("://", 1)[1]
            if "#" in rest:
                rest = rest.split("#", 1)[0]
            if "@" in rest:
                _, hostport = rest.rsplit("@", 1)
                hostport = hostport.split("?", 1)[0]
                return parse_host_port(hostport)
            else:
                decoded = base64.b64decode(_b64pad(rest.split("?", 1)[0])).decode("utf-8")
                if "@" in decoded:
                    _, hostport = decoded.rsplit("@", 1)
                    return parse_host_port(hostport)
    except Exception:
        pass
    return None, None


@functools.lru_cache(maxsize=8192)
def get_country_local(host):
    try:
        ip = socket.gethostbyname(host)
        match = geo_reader.country(ip)
        country_name = match.country.name
        if country_name in COUNTRY_DATA:
            return country_name
        return "Others"
    except Exception:
        return "Others"


def node_key(config_line):
    """Stable identity for a node: scheme + host + port (ignores remark variations)."""
    host, port = extract_host_and_port(config_line)
    if not host or not port:
        return None
    scheme = config_line.split("://", 1)[0].lower() if "://" in config_line else "unknown"
    return f"{scheme}://{host.lower()}:{port}"


def test_single_node(line, known_country=None):
    """Real end-to-end verification: Xray-core SOCKS handshake + HTTP fetch.
    If known_country is given (e.g. Au1rxx), skips DNS resolution and GeoIP lookup."""
    host, port = extract_host_and_port(line)
    if not host or not port:
        return None

    ok, latency = verify_node(line)
    if not ok:
        return None

    if known_country and known_country in BUTTON_TO_COUNTRY:
        assigned_bucket = known_country
    else:
        country_name = get_country_local(host)
        assigned_bucket = country_name if country_name in BUTTON_TO_COUNTRY else "Others"

    return {"bucket": assigned_bucket, "raw_line": line, "latency": latency}


def rebrand_config(config_line, country_key, index):
    meta = COUNTRY_DATA.get(country_key, COUNTRY_DATA["Others"])
    new_remark = f"{meta['flag']} {meta['abbrev']} | litixconnect #{index} | {CHANNEL_ID}"

    try:
        if config_line.startswith("vmess://"):
            b64_data = config_line.replace("vmess://", "").strip()
            b64_data += "=" * ((4 - len(b64_data) % 4) % 4)
            data = json.loads(base64.b64decode(b64_data).decode('utf-8'))
            data["ps"] = new_remark
            updated_json = json.dumps(data).encode('utf-8')
            return f"vmess://{base64.b64encode(updated_json).decode('utf-8')}"

        elif any(config_line.startswith(p) for p in ["vless://", "ss://", "trojan://"]):
            base_part = config_line.split("#")[0]
            # The remark is a URI fragment: v2ray-style clients (v2rayNG etc.)
            # parse these links with strict java.net.URI, which rejects a second
            # '#' or any illegal character and silently drops the whole line on
            # import. Percent-encode the remark so every client accepts it.
            return f"{base_part}#{quote(new_remark, safe='')}"
    except Exception:
        pass
    return config_line


def decode_base64_content(content):
    """Try to decode base64 content, return original if not base64"""
    try:
        content = content.strip()
        if not content:
            return []
        padded_content = content + "=" * ((4 - len(content) % 4) % 4)
        decoded = base64.b64decode(padded_content).decode('utf-8')
        return decoded.splitlines()
    except Exception:
        return content.splitlines()

def fetch_sources_configs():
    """Fetch all original SOURCES concurrently with automatic mirror fallback."""
    raw_lines = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
        futures = {executor.submit(http_get, url, 12): url for url in SOURCES}
        for future in as_completed(futures):
            url = futures[future]
            try:
                res = future.result()
                if res.status_code == 200:
                    lines = decode_base64_content(res.text)
                    raw_lines.extend(lines)
                    logger.info("Fetched %d lines from source %s", len(lines), url.split('/')[-1])
                else:
                    logger.warning("Original source %s returned HTTP %d", url, res.status_code)
            except Exception as e:
                logger.warning("Original source read exception (%s): %s", url, e)
    return raw_lines


def _fetch_country_parts(country_code, country_name):
    """Worker function to fetch all parts for a single country."""
    valid_lines = []
    for part in range(1, AU1RXX_PARTS + 1):
        url = f"{AU1RXX_BASE}/{country_code}/v2ray-base64-{part:04d}.txt"
        try:
            res = http_get(url, timeout=12)
            if res.status_code == 404:
                break  # no more parts for this country
            if res.status_code != 200:
                logger.warning("Au1rxx %s part %d: HTTP %d", country_code, part, res.status_code)
                break
            lines = decode_base64_content(res.text)
            part_lines = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
            valid_lines.extend(part_lines)
        except Exception as e:
            logger.warning("Au1rxx fetch error for %s (%s) part %d: %s", country_name, country_code, part, e)
            break
    return country_name, country_code, valid_lines


def fetch_au1rxx_configs():
    """Fetch v2ray configs from Au1rxx GitHub repo concurrently for all supported countries."""
    configs_by_country = {country: [] for country in COUNTRY_DATA.keys()}
    total_fetched = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_country_parts, code, name): (code, name)
            for code, name in AU1RXX_COUNTRIES.items()
        }
        for future in as_completed(futures):
            try:
                c_name, c_code, lines = future.result()
                if lines:
                    configs_by_country[c_name].extend(lines)
                    total_fetched += len(lines)
            except Exception as e:
                code, name = futures[future]
                logger.warning("Error fetching Au1rxx configs for %s (%s): %s", name, code, e)

    logger.info("Au1rxx concurrent fetch completed: %d total configs across countries", total_fetched)
    return configs_by_country


def generate_txt_file(configs, country_name):
    """Generate a .txt file with all configs for a country"""
    meta = COUNTRY_DATA.get(country_name, COUNTRY_DATA["Others"])
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    content = f"# LitixConnect - {country_name} Configs\n"
    content += f"# Generated: {timestamp}\n"
    content += f"# Country: {country_name} ({meta['abbrev']})\n"
    content += f"# Total Configs: {len(configs)}\n"
    content += f"# Channel: {CHANNEL_ID}\n\n"

    for i, config in enumerate(configs, 1):
        rebranded = rebrand_config(config, country_name, i)
        content += f"{rebranded}\n"

    return content


def generate_subscription_content():
    """One combined base64 file: v2rayNG/V2Box/Nekoray can import it as a subscription."""
    all_lines = []
    for country_name, lines in categorized_nodes.items():
        if lines and country_name != "Others":
            all_lines.extend(lines)
    if not all_lines:
        return None
    joined = "\n".join(all_lines)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")


def pick_diverse_configs(lines, count):
    """Pick up to `count` configs with all-distinct server addresses.

    Groups configs by host address, keeps one per address (random within the group),
    then samples across addresses - so 50 picks means 50 different servers,
    spread over the whole address range instead of clustering on duplicates.
    """
    by_host = {}
    for line in lines:
        host, _port = extract_host_and_port(line)
        if not host:
            continue
        by_host.setdefault(host.lower(), []).append(line)

    if not by_host:
        return []

    # one random config per unique address, then shuffle so the sample
    # isn't biased toward addresses that happened to appear first
    one_per_host = [random.choice(group) for group in by_host.values()]
    random.shuffle(one_per_host)

    if len(one_per_host) <= count:
        return one_per_host
    return random.sample(one_per_host, count)


def build_top_picks():
    """Build the quick-picks content: TOP_PICKS_COUNTRIES countries with the most
    live configs, TOP_PICKS_PER_COUNTRY randomly-chosen diverse configs each."""
    ranked = sorted(
        ((name, lines) for name, lines in categorized_nodes.items()
         if lines and name != "Others"),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    if not ranked:
        return None

    top = ranked[:TOP_PICKS_COUNTRIES]
    sections = []
    picked_countries = []
    for country_name, lines in top:
        picks = pick_diverse_configs(lines, TOP_PICKS_PER_COUNTRY)
        if not picks:
            continue
        meta = COUNTRY_DATA[country_name]
        section = [f"# {meta['flag']} {country_name} ({meta['abbrev']}) - {len(picks)} configs"]
        for i, config in enumerate(picks, 1):
            section.append(rebrand_config(config, country_name, i))
        sections.append("\n".join(section))
        picked_countries.append((country_name, len(picks)))

    if not sections:
        return None
    return {"countries": picked_countries, "content": "\n\n".join(sections)}


# --- SAFE TELEGRAM SENDERS (Markdown/HTML parse errors and 429 flood waits) ---
def extract_retry_after(e):
    """Pull retry_after out of an ApiTelegramException, tolerating library version differences."""
    payload = getattr(e, "result_json", None) or {}
    parameters = payload.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    if retry_after:
        try:
            return int(retry_after)
        except (TypeError, ValueError):
            pass
    # last resort: parse "retry after N" from the human-readable description
    m = re.search(r"retry after (\d+)", str(e), re.IGNORECASE)
    return int(m.group(1)) if m else 0


def safe_api_call(func, *args, **kwargs):
    """Call a Telegram API method; on 429 sleep exactly as long as Telegram asks,
    on parse errors retry without parse_mode. Never gives up on flood waits."""
    flood_waits = 0
    net_retries = 0
    while True:
        try:
            return func(*args, **kwargs)
        except ApiTelegramException as e:
            retry_after = extract_retry_after(e)
            if retry_after:
                flood_waits += 1
                logger.warning("Flood limit hit, waiting %ds (wait #%d)", retry_after, flood_waits)
                time.sleep(retry_after + 1)
                continue  # flood waits are always retried, never counted against attempts
            if "parse" in str(e).lower() or "can't parse" in str(e).lower():
                logger.warning("Parse error, retrying without parse_mode: %s", e)
                kwargs.pop("parse_mode", None)
                continue
            raise
        except (requests.exceptions.RequestException, Exception) as e:
            err_str = str(e).lower()
            if net_retries < 5 and ("network" in err_str or "connection" in err_str or "timeout" in err_str or "errno" in err_str):
                net_retries += 1
                logger.warning("Network glitch in safe_api_call (%s), retry %d/5 in 2s...", e, net_retries)
                time.sleep(2)
                continue
            raise


def send_message_safe(chat_id, text, **kwargs):
    """Send a text message; flood-wait aware, parse-error fallback."""
    return safe_api_call(bot.send_message, chat_id, text, **kwargs)


def send_document_safe(chat_id, doc, **kwargs):
    """Send a document; flood-wait aware, parse-error fallback."""
    return safe_api_call(bot.send_document, chat_id, doc, **kwargs)


def send_photo_safe(chat_id, photo, **kwargs):
    """Send a photo; flood-wait aware, parse-error fallback."""
    return safe_api_call(bot.send_photo, chat_id, photo, **kwargs)


def post_to_channel(country_name, configs):
    """Post configs for a country to the Telegram channel using pre-saved file."""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set, skipping channel post")
        return False

    total = len(configs)
    if total == 0:
        return False

    try:
        meta = COUNTRY_DATA.get(country_name, COUNTRY_DATA["Others"])
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        filename = f"{meta['code'].lower()}_configs.txt"
        filepath = script_dir / filename

        if not filepath.exists():
            txt_content = generate_txt_file(configs, country_name)
            filepath.write_text(txt_content, encoding='utf-8')

        caption = (
            f"{meta['flag']} <b>{country_name}</b> - {total} Working Configs\n"
            f"📅 Updated: {timestamp}\n"
            f"🔗 Channel: {CHANNEL_ID}"
        )

        with open(filepath, 'rb') as doc:
            send_document_safe(
                CHANNEL_ID,
                doc,
                visible_file_name=filename,
                caption=caption,
                parse_mode="HTML"
            )

        logger.info("Posted %s (%d configs) to channel", country_name, total)
        return True

    except Exception as e:
        logger.warning("Failed to post %s to channel: %s", country_name, e)
        return False


def get_best_font(size, bold=False):
    """Try to load clean TrueType fonts from system, falling back to default."""
    from PIL import ImageFont
    candidates = [
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
        "calibrib.ttf" if bold else "calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def create_update_banner():
    """Generate a visual banner image summarizing the latest config update"""
    from PIL import Image, ImageDraw

    width, height = 1280, 800
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    top = (15, 23, 42)
    bottom = (76, 29, 149)
    for y in range(height):
        t = y / height
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    title_font = get_best_font(64, bold=True)
    sub_font = get_best_font(32)
    small_font = get_best_font(24)
    count_font = get_best_font(28, bold=True)

    draw.text((width // 2, 80), "LitixConnect", font=title_font, fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, 145), "Fresh VPN Configs Updated", font=sub_font, fill=(165, 180, 252), anchor="mm")
    draw.text((width // 2, 190), time.strftime("%Y-%m-%d  %H:%M UTC", time.gmtime()), font=small_font, fill=(148, 163, 184), anchor="mm")

    with nodes_lock:
        entries = [(name, len(lines)) for name, lines in categorized_nodes.items() if lines and name != "Others"]
    entries.sort(key=lambda e: e[1], reverse=True)

    cols = 4
    cell_w, cell_h = 290, 62
    start_x = (width - cols * cell_w) // 2
    start_y = 240

    for i, (name, count) in enumerate(entries[:24]):
        code = COUNTRY_DATA[name]["abbrev"]
        col, row = i % cols, i // cols
        x = start_x + col * cell_w + cell_w // 2
        y = start_y + row * cell_h + cell_h // 2
        draw.rounded_rectangle([x - 130, y - 24, x + 130, y + 24], radius=12, fill=(30, 41, 59), outline=(99, 102, 241), width=1)
        draw.text((x, y), f"{code}   {count}", font=count_font, fill=(226, 232, 240), anchor="mm")

    if len(entries) > 24:
        draw.text((width // 2, start_y + 6 * cell_h + 20), f"+{len(entries) - 24} more countries", font=small_font, fill=(148, 163, 184), anchor="mm")

    with nodes_lock:
        total = sum(len(lines) for lines in categorized_nodes.values())
    draw.text((width // 2, height - 110), f"{total} verified configs across {len(entries)} countries", font=sub_font, fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, height - 55), CHANNEL_ID, font=small_font, fill=(165, 180, 252), anchor="mm")

    banner_path = script_dir / "update_banner.png"
    img.save(banner_path)
    return banner_path


def post_all_countries_to_channel():
    """Broadcast update to channel: header announcement with 1-tap configs, subscription, quick-picks, and country files."""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set, skipping channel post")
        return

    with nodes_lock:
        active_entries = [(name, list(lines)) for name, lines in categorized_nodes.items()
                          if lines and name != "Others"]

    if not active_entries:
        logger.warning("No active configs to post to channel")
        return

    total_configs = sum(len(lines) for _, lines in active_entries)
    logger.info("Broadcasting update to channel %s (%d configs across %d countries)...",
                CHANNEL_ID, total_configs, len(active_entries))

    timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    # 1. Collect top 3 fastest configs across top countries for 1-tap mobile clipboard copy
    top_3_configs = []
    for _, lines in active_entries[:3]:
        if lines:
            top_3_configs.append(lines[0])

    fastest_block = ""
    if top_3_configs:
        code_lines = "\n\n".join(f"<code>{html.escape(cfg)}</code>" for cfg in top_3_configs)
        fastest_block = f"\n\n⚡ <b>Top Fastest Configs (Tap to Copy):</b>\n{code_lines}"

    # 2. Generate and post update banner image as the main announcement
    try:
        banner_path = create_update_banner()
        banner_caption = (
            f"🚀 <b>LitixConnect | Fresh Config Update</b>\n"
            f"📅 <b>Time:</b> {timestamp}\n"
            f"📦 <b>Verified:</b> {total_configs} working configs across {len(active_entries)} countries\n"
            f"⏱ <b>Push Schedule:</b> Every {UPDATE_INTERVAL_HOURS} Hours"
            f"{fastest_block}\n\n"
            f"💎 <b>سرورهای پرسرعت و بدون قطعی VIP با آی‌پی تمیز فعال شد!</b>\n"
            f"📥 <i>فایل‌های رایگان کشورها و سابسکریپشن کامل در ادامه پیوست شده است.</i>\n"
            f"🔗 {CHANNEL_ID}"
        )
        markup = None
        if bot_username:
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("💎 خرید کانفیگ اختصاصی VIP (بدون قطعی)", url=f"https://t.me/{bot_username}?start=buy"),
                types.InlineKeyboardButton("🤖 ورود به ربات برای دریافت کانفیگ", url=f"https://t.me/{bot_username}")
            )

        with open(banner_path, 'rb') as photo:
            send_photo_safe(CHANNEL_ID, photo, caption=banner_caption, parse_mode="HTML", reply_markup=markup)
        logger.info("Posted update banner to channel")
        time.sleep(3.5)
    except Exception as e:
        logger.warning("Failed to post update banner: %s", e)

    # 3. Post all-in-one subscription file
    try:
        sub_content = generate_subscription_content()
        if sub_content:
            sub_name = "litixconnect_subscription.txt"
            sub_path = script_dir / sub_name
            sub_path.write_text(sub_content, encoding="utf-8")
            caption = (
                "📦 <b>All-in-One Subscription File</b>\n"
                "Import this file in <b>v2rayNG</b> / <b>V2Box</b> / <b>Nekoray</b> / <b>Streisand</b> to load every verified config at once.\n\n"
                f"🔗 {CHANNEL_ID}"
            )
            with open(sub_path, 'rb') as doc:
                send_document_safe(CHANNEL_ID, doc, visible_file_name=sub_name, caption=caption, parse_mode="HTML")
            logger.info("Posted combined subscription file to channel")
            time.sleep(3.5)
    except Exception as e:
        logger.warning("Failed to post subscription file: %s", e)

    # 4. Post top-5 quick picks file
    try:
        top_picks = build_top_picks()
        if top_picks:
            picks_line = ", ".join(f"{COUNTRY_DATA[name]['flag']} {name} ×{n}" for name, n in top_picks["countries"])
            caption = (
                "⚡ <b>Quick Picks - Top 5 Countries</b>\n"
                f"{picks_line}\n\n"
                "50 hand-picked configs per country, no duplicate servers - "
                "a lightweight file for quick access.\n\n"
                f"🔗 {CHANNEL_ID}"
            )
            picks_path = script_dir / "top5_quick_picks.txt"
            picks_path.write_text(top_picks["content"], encoding="utf-8")
            with open(picks_path, 'rb') as doc:
                send_document_safe(CHANNEL_ID, doc, visible_file_name="top5_quick_picks.txt",
                                   caption=caption, parse_mode="HTML")
            logger.info("Posted top-5 quick picks file to channel")
            time.sleep(3.5)
    except Exception as e:
        logger.warning("Failed to post quick picks file: %s", e)

    # 5. Post individual country .txt files
    posted_countries = 0
    for country_name, lines in active_entries:
        if post_to_channel(country_name, lines):
            posted_countries += 1
            time.sleep(3.5)

    logger.info("Channel broadcast complete: %d country files posted", posted_countries)

    # 6. Post a rotating Persian VIP feature announcement to channel
    try:
        announcement_idx = (int(time.time() // max(1, UPDATE_INTERVAL)) % len(persian_announcements.VIP_ANNOUNCEMENTS)) + 1
        persian_announcements.send_persian_announcement(bot, CHANNEL_ID, bot_username, template_id=announcement_idx)
    except Exception as e:
        logger.warning("Failed to post rotating Persian VIP announcement: %s", e)


# --- CORE ASYNCHRONOUS POOL ENGINE ---
def seconds_until_next_aligned_slot(now=None):
    """Wait until the next aligned UTC hour boundary (e.g., 00:00, 12:00 UTC for 12h intervals)
    so channel pushes land on a predictable schedule."""
    now = now or time.time()
    next_slot = (int(now // UPDATE_INTERVAL) + 1) * UPDATE_INTERVAL
    return max(1.0, next_slot - now)


def get_next_update_time_str():
    """Return human-readable UTC string of the next scheduled update time."""
    next_timestamp = (int(time.time() // UPDATE_INTERVAL) + 1) * UPDATE_INTERVAL
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(next_timestamp))


def update_configs_loop():
    global categorized_nodes, last_update_time

    while True:
        logger.info("Starting high-speed concurrent configuration update (interval: %dh)...", UPDATE_INTERVAL_HOURS)
        cleanup_xray_temp_files()
        temp_storage = {k: [] for k in BUTTON_TO_COUNTRY.keys()}
        seen_keys = set()  # global dedup across ALL sources (same host:port never served twice)

        # 1. Concurrent fetch from original sources
        logger.info("Fetching from original sources in parallel...")
        raw_lines = fetch_sources_configs()

        # 2. Concurrent fetch from Au1rxx GitHub (country-specific, multi-part)
        logger.info("Fetching from Au1rxx GitHub repository across countries...")
        au1rxx_configs = fetch_au1rxx_configs()
        for country_name, lines in au1rxx_configs.items():
            if country_name in temp_storage:
                temp_storage[country_name].extend(lines)

        # 3. Test unique configs from original sources
        unique_original = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            key = node_key(line)
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            unique_original.append(line)

        logger.info("Discovered %d original nodes. Launching verification pipeline...", len(unique_original))

        exe = ensure_xray_binary()
        if exe is None:
            with nodes_lock:
                cached_count = sum(len(v) for v in categorized_nodes.values())
            logger.warning("Xray binary unavailable - SKIPPING scan (previous cache kept, %d configs). Retry in 10 min.",
                           cached_count)
            time.sleep(600)
            continue

        active_found = 0
        latency_map = {}  # node_key -> measured latency_ms
        with ThreadPoolExecutor(max_workers=NODE_TEST_WORKERS) as executor:
            futures = [executor.submit(test_single_node, line) for line in unique_original]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    active_found += 1
                    temp_storage[result["bucket"]].append(result["raw_line"])
                    if result.get("latency") is not None:
                        latency_map[node_key(result["raw_line"])] = result["latency"]

        # 4. Au1rxx configs: pre-sorted, bypass redundant GeoIP, test connectivity
        logger.info("Testing Au1rxx configs (%d countries)...", len(au1rxx_configs))
        for country_name, lines in au1rxx_configs.items():
            bucket_lines = []
            for line in lines:
                key = node_key(line)
                if key and key in seen_keys:
                    continue  # already present from another source
                if key:
                    seen_keys.add(key)
                bucket_lines.append(line)
            if not bucket_lines:
                continue
            with ThreadPoolExecutor(max_workers=NODE_TEST_WORKERS) as executor:
                futures = [executor.submit(test_single_node, line, country_name) for line in bucket_lines]
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        active_found += 1
                        temp_storage[country_name].append(result["raw_line"])
                        if result.get("latency") is not None:
                            latency_map[node_key(result["raw_line"])] = result["latency"]

        total_found = sum(len(v) for v in temp_storage.values())

        # 5. Empty-scan guard: never wipe the channel's content because one source hiccupped
        if total_found == 0:
            with nodes_lock:
                cached_count = sum(len(v) for v in categorized_nodes.values())
            logger.warning("Scan found 0 live nodes - keeping previous cache (%d configs) and retrying in 2 min",
                           cached_count)
            time.sleep(120)
            continue

        # 6. Sort each bucket fastest-first (verified nodes with no latency keep their order)
        for bucket, lines in temp_storage.items():
            temp_storage[bucket] = sorted(
                lines,
                key=lambda l: (
                    latency_map.get(node_key(l)) is None,  # measured nodes first
                    latency_map.get(node_key(l)) or 10**9,
                ),
            )

        # Rebrand all configs
        for bucket, lines in temp_storage.items():
            country_data_key = BUTTON_TO_COUNTRY[bucket]
            temp_storage[bucket] = [rebrand_config(line, country_data_key, idx) for idx, line in enumerate(lines, 1)]

        with nodes_lock:
            categorized_nodes = temp_storage
            last_update_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

        # 7. Generate and save .txt files for each country
        logger.info("Saving .txt files for each country...")
        for country_name, lines in categorized_nodes.items():
            if lines and country_name != "Others":
                txt_content = generate_txt_file(lines, country_name)
                filename = f"{COUNTRY_DATA[country_name]['code'].lower()}_configs.txt"
                filepath = script_dir / filename
                try:
                    filepath.write_text(txt_content, encoding='utf-8')
                except Exception as e:
                    logger.warning("Failed to save %s: %s", filename, e)

        # 8. Persist state, then broadcast to Telegram channel
        prune_offsets()
        save_state()
        post_all_countries_to_channel()

        cleanup_xray_temp_files()
        next_wait = seconds_until_next_aligned_slot()
        logger.info("Background sync complete. %d live nodes cached. Next sweep in %.1f hours (at %s).",
                    total_found, next_wait / 3600, get_next_update_time_str())
        time.sleep(next_wait)


# --- BOT COMMANDS & INTERACTIVE UI ---
def build_country_inline_keyboard():
    """Inline keyboard: flag + name per button, 3 columns, Others last."""
    countries = [c for c in BUTTON_TO_COUNTRY.keys() if c != "Others"]
    markup = types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for country in countries:
        meta = COUNTRY_DATA[country]
        buttons.append(types.InlineKeyboardButton(
            f"{meta['flag']} {country}", callback_data=f"country:{country}"
        ))
    buttons.append(types.InlineKeyboardButton("🌐 Others", callback_data="country:Others"))
    markup.add(*buttons)
    markup.row(types.InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:main"))
    return markup


def build_main_menu_keyboard():
    """Main bilingual interactive menu keyboard."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💎 خرید کانفیگ اختصاصی (VIP)", callback_data="menu:buy"),
        types.InlineKeyboardButton("💖 حمایت مالی (Donation)", callback_data="menu:donate"),
    )
    markup.add(
        types.InlineKeyboardButton("🌐 کانفیگ‌های رایگان کشورها", callback_data="menu:free_countries"),
        types.InlineKeyboardButton("⚡ ۵ کشور برتر (Top 5)", callback_data="menu:top"),
    )
    markup.add(
        types.InlineKeyboardButton("📦 سابسکریپشن رایگان", callback_data="menu:sub"),
        types.InlineKeyboardButton("📊 وضعیت سرورها", callback_data="menu:status"),
    )
    markup.add(
        types.InlineKeyboardButton("📱 راهنمای اتصال و دانلود", callback_data="menu:guide"),
        types.InlineKeyboardButton("👤 پیگیری سفارشات من", callback_data="menu:my_orders"),
    )
    return markup


def get_welcome_text():
    countries = [c for c in BUTTON_TO_COUNTRY.keys() if c != "Others"]
    last_line = f"🕒 آخرین آپدیت رایگان: <code>{last_update_time}</code>" if last_update_time else "🕒 اولین اسکن در حال انجام..."
    next_line = f"⏳ اسکن بعدی: <code>{get_next_update_time_str()}</code>"
    verify_line = ("✅ تست زنده با هسته Xray-Core فعال است"
                   if _xray_ready else "⚠️ تستر Xray موقتاً در حال آماده‌سازی است")

    return (
        f"👋 <b>به ربات هوشمند LitixConnect خوش آمدید!</b>\n\n"
        f"این سرویس جامع برای دسترسی به اینترنت آزاد و پرسرعت طراحی شده است:\n\n"
        f"1️⃣ <b>کانفیگ‌های رایگان و روزانه ({len(countries)} کشور):</b>\n"
        f"اسکن، فیلتر و راستی‌آزمایی خودکار هر ۱۲ ساعت از معتبرترین سورس‌ها با پینگ واقعی.\n\n"
        f"2️⃣ <b>سرورهای اختصاصی و پرسرعت VIP:</b>\n"
        f"▫️ اتصال پایدار روی تمامی اپراتورها (همراه اول، ایرانسل، رایتل و نت خانگی)\n"
        f"▫️ مجهز به تکنولوژی ضد فیلتر TLS Fragmentation و خروجی Direct IP\n"
        f"▫️ آی‌پی تمیز و بدون قطعی مخصوص گیمینگ، استریم و هوش مصنوعی (ChatGPT/Gemini)\n"
        f"▫️ مسیریابی هوشمند Iran-Safe (باز شدن مستقیم سایت‌های بانکی و ایرانی بدون قطع فیلترشکن)\n\n"
        f"{last_line}\n{next_line}\n{verify_line}\n\n"
        f"👇 <i>جهت ادامه یکی از گزینه‌های زیر را انتخاب فرمایید:</i>\n\n"
        f"🔗 کانال رسمی تلگرام: {CHANNEL_ID}"
    )


def show_buy_menu(chat_id, message_id=None):
    plans = get_vip_plans()
    if not plans:
        send_message_safe(chat_id, "⚠️ در حال حاضر هیچ پلنی فعال نیست. لطفاً بعداً مراجعه فرمایید.")
        return

    available_devices = sorted(list(set(p.get("devices", 1) for p in plans)))
    device_names = {
        1: "👤 پلن‌های ۱ کاربره (تک کاربره)",
        2: "👥 پلن‌های ۲ کاربره (دو کاربره)",
        3: "👨‍👩‍👧 پلن‌های ۳ کاربره (سه کاربره)",
        4: "👨‍👩‍👦‍👦 پلن‌های ۴ کاربره (چهار کاربره)",
        5: "🏢 پلن‌های ۵ کاربره (پنج کاربره / تیمی)",
    }

    text = (
        "💎 <b>خرید اشتراک اختصاصی و فوق پرسرعت VIP:</b>\n\n"
        "▫️ سرورهای اختصاصی تانل با پینگ بسیار پایین و پایدار\n"
        "▫️ مجهز به تکنولوژی ضد فیلتر و TLS Fragmentation (همراه اول، ایرانسل، رایتل و نت خانگی)\n"
        "▫️ آی‌پی تمیز و ثابت، مناسب ترید، صرافی‌های بین‌المللی و هوش مصنوعی\n"
        "▫️ ارائه در دوره‌های ۱ ماهه، ۳ ماهه و ۶ ماهه (از ۱۰ تا ۲۰۰ گیگابایت)\n\n"
        "👇 <i>لطفاً دسته اشتراک مورد نظر خود را بر اساس تعداد دستگاه/کاربر انتخاب فرمایید:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    for dev in available_devices:
        title = device_names.get(dev, f"👥 پلن‌های {dev} کاربره")
        markup.add(types.InlineKeyboardButton(title, callback_data=f"buy_cat:{dev}"))
    markup.add(types.InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:main"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


def show_volume_menu(chat_id, devices: int, message_id=None):
    plans = get_vip_plans()
    matching_plans = [p for p in plans if p.get("devices", 1) == devices]
    if not matching_plans:
        send_message_safe(chat_id, "⚠️ پلنی برای این دسته‌بندی یافت نشد.")
        return

    volumes = sorted(list(set(p["volume_gb"] for p in matching_plans)))
    device_titles = {
        1: "۱ کاربره",
        2: "۲ کاربره",
        3: "۳ کاربره",
        4: "۴ کاربره",
        5: "۵ کاربره",
    }
    dev_str = device_titles.get(devices, f"{devices} کاربره")
    text = (
        f"⚡ <b>انتخاب حجم ترافیک (پلن‌های {dev_str}):</b>\n\n"
        f"برای اشتراک‌های {dev_str}، حجم‌های زیر در دوره‌های ۱، ۳ و ۶ ماهه در دسترس هستند.\n\n"
        f"👇 <i>لطفاً حجم ترافیک مورد نظر خود را انتخاب فرمایید:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    vol_buttons = []
    for v in volumes:
        label = f"▫️ {v} گیگابایت" if v != 200 else "▫️ ۲۰۰ گیگ (نامحدود)"
        vol_buttons.append(types.InlineKeyboardButton(label, callback_data=f"buy_vol:{devices}:{v}"))

    markup.add(*vol_buttons)
    markup.row(types.InlineKeyboardButton("🔙 بازگشت به انتخاب تعداد کاربر", callback_data="menu:buy"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


def show_duration_menu(chat_id, devices: int, volume_gb: int, message_id=None):
    plans = get_vip_plans()
    matching_plans = [p for p in plans if p.get("devices", 1) == devices and p.get("volume_gb") == volume_gb]
    if not matching_plans:
        send_message_safe(chat_id, "⚠️ پلنی با این مشخصات یافت نشد.")
        return

    matching_plans.sort(key=lambda x: x.get("duration_days", 30))
    dur_labels = {
        30: "۱ ماهه",
        90: "۳ ماهه",
        180: "۶ ماهه",
    }

    vol_title = f"{volume_gb} گیگابایت" if volume_gb != 200 else "۲۰۰ گیگابایت (نامحدود)"
    text = (
        f"⚡ <b>انتخاب مدت زمان اشتراک:</b>\n\n"
        f"▫️ <b>دسته‌بندی:</b> {devices} کاربره همزمان\n"
        f"▫️ <b>حجم ترافیک:</b> {vol_title}\n\n"
        f"👇 <i>مدت زمان اشتراک مورد نظر خود را انتخاب نمایید:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in matching_plans:
        days = p.get("duration_days", 30)
        dur_name = dur_labels.get(days, f"{days} روز")
        toman = p.get("price_toman", 0)
        usd = p.get("price_usd", 0.0)
        btn_title = f"▫️ {dur_name}: {toman:,} تومان (~${usd:.0f} USD)"
        markup.add(types.InlineKeyboardButton(btn_title, callback_data=f"plan_sel:{p['id']}"))

    markup.add(types.InlineKeyboardButton("🔙 بازگشت به انتخاب حجم", callback_data=f"buy_cat:{devices}"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


def show_donation_menu(chat_id, message_id=None):
    text = (
        "💖 <b>حمایت مالی از پروژه آزاد LitixConnect</b>\n\n"
        "ما برای زنده نگه داشتن اینترنت آزاد، هر روز صدها سرور رایگان را اسکن و فیلتر می‌کنیم. "
        "اجاره سرورهای قدرتمند تست و پنل‌های تانل هزینه‌های بالایی دارد. "
        "اگر از سرویس‌های رایگان ما رضایت دارید، حمایت‌های کریپتویی شما انرژی‌بخش ادامه این مسیر خواهد بود:\n\n"
        + crypto_manager.get_wallet_info_text()
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔺 بارکد QR ترون (TRX)", callback_data="donate_qr:tron"),
        types.InlineKeyboardButton("🔹 بارکد QR اتریوم (ETH)", callback_data="donate_qr:eth"),
    )
    markup.row(types.InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:main"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


def show_guide_menu(chat_id, message_id=None):
    text = (
        "📱 <b>راهنمای اتصال و دانلود برنامه‌های رسمی:</b>\n\n"
        "🔹 <b>اندروید (Android):</b>\n"
        "• نرم‌افزار پیشنهادی: <b>v2rayNG</b> (دانلود از گوگل پلی یا گیت‌هاب)\n"
        "• راهنما: پس از کپی کردن لینک سابسکریپشن، وارد منوی ۳ خط ☰ شده، گزینه Subscription group setting را بزنید، "
        "با زدن دکمه ➕ لینک را اضافه کنید و در صفحه اصلی با زدن ۳ نقطه روی Update subscription کلیک کنید.\n"
        "• <b>رفع کندی روی همراه اول/ایرانسل:</b> در تنظیمات (Settings) برنامه، گزینه <b>Fragment</b> را روشن کنید.\n\n"
        "🔹 <b>آیفون و آیپد (iOS):</b>\n"
        "• نرم‌افزارهای پیشنهادی: <b>V2Box</b> یا <b>Streisand</b> یا <b>Shadowrocket</b> (از اپ استور)\n"
        "• راهنما: در برنامه V2Box به بخش Configs رفته، ➕ را بزنید و Add Subscription را انتخاب کرده و لینک را پیست کنید.\n\n"
        "🔹 <b>ویندوز (Windows):</b>\n"
        "• نرم‌افزار پیشنهادی: <b>v2rayN</b> یا <b>Nekoray</b> یا <b>Sing-box</b>\n"
        "• راهنما: از منوی Subscription Group گزینه Add را بزنید و لینک ساب را وارد نمایید.\n\n"
        "💡 تمامی اشتراک‌های ما سازگار با هر دو نوع لینک استاندارد VLESS و سابسکریپشن Sing-box/Clash هستند."
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:main"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


def show_my_orders_menu(chat_id, user_id, message_id=None):
    orders = order_mgr.get_user_orders(user_id)
    if not orders:
        text = "👤 <b>سفارش‌های شما:</b>\n\nشما هنوز هیچ سفارشی ثبت نکرده‌اید."
    else:
        lines = ["👤 <b>تاریخچه سفارش‌های شما:</b>\n"]
        for o in orders[-5:]:
            status_fa = {
                "AWAITING_PAYMENT": "⏳ در انتظار واریز",
                "PENDING_VERIFICATION": "🔍 در حال بررسی مدیریت",
                "APPROVED": "✅ تایید و فعال شده",
                "REJECTED": "❌ رد شده",
            }.get(o["status"], o["status"])

            line = f"▫️ کد سفارش: <code>{o['order_id']}</code> | پلن: <b>{o['plan_name']}</b>\n   وضعیت: {status_fa}"
            if o.get("delivered_sub_url"):
                line += f"\n   🔗 لینک سابسکریپشن: <code>{o['delivered_sub_url']}</code>"
            lines.append(line)
        text = "\n\n".join(lines)

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="menu:main"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    send_message_safe(chat_id, text, reply_markup=markup, parse_mode="HTML")


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    parts = message.text.split()
    if len(parts) > 1:
        param = parts[1].lower()
        if param == "buy":
            show_buy_menu(message.chat.id)
            return
        elif param == "donate":
            show_donation_menu(message.chat.id)
            return

    send_message_safe(
        message.chat.id,
        get_welcome_text(),
        reply_markup=build_main_menu_keyboard(),
        parse_mode="HTML"
    )


@bot.message_handler(commands=['status'])
def send_status(message):
    """Per-country counts + last and next update times."""
    lines = []
    total = 0
    with nodes_lock:
        for country, nodes in categorized_nodes.items():
            if country != "Others" and nodes:
                meta = COUNTRY_DATA[country]
                lines.append(f"{meta['flag']} {country}: <b>{len(nodes)}</b>")
                total += len(nodes)
        others = len(categorized_nodes.get("Others", []))
        if others:
            lines.append(f"🌐 Others: <b>{others}</b>")
            total += others

    if not lines:
        body = "No configs cached yet - the first scan may still be running. Try again in a few minutes."
    else:
        body = "\n".join(lines)

    last_line = f"🕒 Last update: {last_update_time}" if last_update_time else "🕒 First scan in progress..."
    next_line = f"⏳ Next update: {get_next_update_time_str()}"
    verify_line = ("✅ End-to-end verified (real Xray-core handshakes)"
                   if _xray_ready else "⚠️ Verifier degraded - Xray binary unavailable, cache may be stale")
    bot.reply_to(
        message,
        f"📊 <b>LitixConnect Cache Status</b>\n\n{body}\n\n📦 Total: {total} configs\n{last_line}\n{next_line}\n{verify_line}\n\n🔗 Channel: {CHANNEL_ID}",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['post'])
def manual_post(message):
    """Manual command to post all countries to channel"""
    if not is_admin(message.from_user) and not is_admin(message.chat.id):
        bot.reply_to(message, "⛔ این دستور مخصوص مدیریت است.")
        return
    bot.reply_to(message, "📢 Posting all countries to channel...")
    threading.Thread(target=post_all_countries_to_channel, daemon=True).start()
    bot.reply_to(message, "✅ Posting started in background!")


@bot.message_handler(commands=['top'])
def send_top_picks(message):
    """On-demand top-5 quick picks file (small, diverse, no duplicate servers)"""
    with nodes_lock:
        total = sum(len(v) for v in categorized_nodes.values())
    if total == 0:
        bot.reply_to(message, "⚠️ No configs cached yet - the first scan may still be running. Try again in a few minutes.")
        return

    top_picks = build_top_picks()
    if not top_picks:
        bot.reply_to(message, "⚠️ Couldn't build quick picks right now. Try again after the next update.")
        return

    picks_line = ", ".join(f"{COUNTRY_DATA[name]['flag']} {name} ×{n}" for name, n in top_picks["countries"])
    picks_path = script_dir / "top5_quick_picks.txt"
    try:
        if not picks_path.exists():
            picks_path.write_text(top_picks["content"], encoding="utf-8")

        with open(picks_path, 'rb') as doc:
            send_document_safe(
                message.chat.id,
                doc,
                visible_file_name="top5_quick_picks.txt",
                caption=(
                    f"⚡ <b>Quick Picks - Top 5 Countries</b>\n"
                    f"{picks_line}\n\n"
                    f"🔗 Channel: {CHANNEL_ID}"
                ),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning("Failed to send quick picks: %s", e)
        bot.reply_to(message, "⚠️ Couldn't send the quick picks file. Try again shortly.")


@bot.message_handler(commands=['sub', 'subscription'])
def send_subscription_file(message):
    """Send all-in-one subscription file directly to user in chat."""
    sub_content = generate_subscription_content()
    if not sub_content:
        bot.reply_to(message, "⚠️ No configs cached yet. Please try again shortly.")
        return

    sub_name = "litixconnect_subscription.txt"
    sub_path = script_dir / sub_name
    try:
        sub_path.write_text(sub_content, encoding="utf-8")
        caption = (
            "📦 <b>All-in-One Subscription File</b>\n\n"
            "Import this file into <b>v2rayNG</b>, <b>V2Box</b>, <b>Streisand</b>, or <b>Nekoray</b> "
            "to load all verified configs at once.\n\n"
            f"🔗 Channel: {CHANNEL_ID}"
        )
        with open(sub_path, 'rb') as doc:
            send_document_safe(
                message.chat.id,
                doc,
                visible_file_name=sub_name,
                caption=caption,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning("Failed to send subscription file: %s", e)
        bot.reply_to(message, "⚠️ Could not send subscription file right now. Please try again.")


@bot.message_handler(commands=['buy', 'vip', 'plans'])
def cmd_buy(message):
    show_buy_menu(message.chat.id)


@bot.message_handler(commands=['donate', 'donation'])
def cmd_donate(message):
    show_donation_menu(message.chat.id)


@bot.message_handler(commands=['admin_id', 'my_id'])
def cmd_admin_id(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    u_name = message.from_user.username
    admin_active = is_admin(message.from_user) or is_admin(chat_id)
    if admin_active:
        register_admin_chat(user_id, u_name)
        status_text = "✅ <b>حساب کاربری شما به عنوان مدیریت رسمی ربات فعال است.</b>"
    else:
        status_text = "⚠️ <b>شما دسترسی مدیریت ندارید. دسترسی مدیریت منحصراً به @awlinavakhtam اختصاص دارد.</b>"

    bot.reply_to(
        message,
        f"🆔 <b>شناسه عددی کاربری شما (User ID):</b> <code>{user_id}</code>\n"
        f"💬 <b>شناسه چت (Chat ID):</b> <code>{chat_id}</code>\n"
        f"👤 <b>نام کاربری:</b> @{html.escape(u_name or 'ندارد')}\n"
        f"🛡 <b>وضعیت دسترسی:</b> {status_text}",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['orders'])
def cmd_orders(message):
    admin_active = is_admin(message.from_user) or is_admin(message.chat.id)
    if admin_active:
        pending = order_mgr.get_pending_orders()
        if not pending:
            bot.reply_to(message, "✅ در حال حاضر هیچ سفارش معلقی برای بررسی وجود ندارد.")
            return

        for o in pending[:5]:
            safe_oid = html.escape(str(o['order_id']))
            safe_user = html.escape(str(o.get('username') or 'ندارد'))
            safe_uid = html.escape(str(o['user_id']))
            safe_plan = html.escape(str(o['plan_name']))
            safe_net = html.escape(str(o.get('crypto_network') or 'نامشخص'))
            safe_tx = html.escape(str(o.get('tx_hash') or 'ثبت شده با عکس'))
            text = (
                f"🔔 <b>سفارش در انتظار تایید:</b>\n"
                f"▫️ کد سفارش: <code>{safe_oid}</code>\n"
                f"▫️ کاربر: @{safe_user} (ID: <code>{safe_uid}</code>)\n"
                f"▫️ پلن: {safe_plan}\n"
                f"▫️ مبلغ: {o['crypto_amount']} {o['crypto_currency']} ({safe_net})\n"
                f"▫️ شناسه تراکنش (TxID): <code>{safe_tx}</code>"
            )
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("✅ تایید و صدور خودکار", callback_data=f"admin_approve:{o['order_id']}"),
                types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"admin_reject:{o['order_id']}"),
            )
            if o.get("photo_file_id"):
                try:
                    send_photo_safe(message.chat.id, o["photo_file_id"], caption=text, reply_markup=markup, parse_mode="HTML")
                    continue
                except Exception:
                    pass
            send_message_safe(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    else:
        show_my_orders_menu(message.chat.id, message.from_user.id)


@bot.message_handler(commands=['post_vip', 'post_announcement'])
def cmd_post_vip(message):
    """Admin command to post a Persian feature announcement to the channel."""
    if not is_admin(message.from_user) and not is_admin(message.chat.id):
        bot.reply_to(message, "⛔ این دستور مخصوص مدیریت است.")
        return

    parts = message.text.split()
    tmpl_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    if tmpl_id not in persian_announcements.VIP_ANNOUNCEMENTS:
        tmpl_id = 1

    ok = persian_announcements.send_persian_announcement(bot, CHANNEL_ID, bot_username, template_id=tmpl_id)
    if ok:
        bot.reply_to(message, f"📢 اعلان شماره {tmpl_id} با موفقیت به کانال ارسال شد.")
    else:
        bot.reply_to(message, "❌ خطا در ارسال اعلان به کانال. لاگ‌ها را بررسی کنید.")


@bot.message_handler(commands=['announce'])
def cmd_announce(message):
    """Admin command to post custom Persian announcement with VIP CTA buttons."""
    if not is_admin(message.from_user) and not is_admin(message.chat.id):
        bot.reply_to(message, "⛔ این دستور مخصوص مدیریت است.")
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            message,
            "⚠️ لطفاً متن اعلان را وارد فرمایید:\n<code>/announce متن پیام اعلان شما</code>",
            parse_mode="HTML"
        )
        return

    custom_text = parts[1].strip()
    markup = persian_announcements.build_channel_vip_markup(bot_username)
    try:
        send_message_safe(CHANNEL_ID, custom_text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        bot.reply_to(message, "📢 اعلان سفارشی با موفقیت به کانال ارسال شد.")
    except Exception as e:
        bot.reply_to(message, f"❌ خطا در ارسال پیام به کانال: {e}")


# --- MENU & ORDER CALLBACK QUERY HANDLERS ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu:"))
def handle_menu_callbacks(call):
    action = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if action == "main":
        try:
            bot.edit_message_text(get_welcome_text(), chat_id, msg_id, reply_markup=build_main_menu_keyboard(), parse_mode="HTML")
        except Exception:
            send_message_safe(chat_id, get_welcome_text(), reply_markup=build_main_menu_keyboard(), parse_mode="HTML")
    elif action == "buy":
        show_buy_menu(chat_id, msg_id)
    elif action == "donate":
        show_donation_menu(chat_id, msg_id)
    elif action == "free_countries":
        text = "📍 <b>لطفاً کشور مورد نظر خود را جهت دریافت ۳ کانفیگ تازه به همراه فایل کامل انتخاب کنید:</b>"
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=build_country_inline_keyboard(), parse_mode="HTML")
        except Exception:
            send_message_safe(chat_id, text, reply_markup=build_country_inline_keyboard(), parse_mode="HTML")
    elif action == "top":
        send_top_picks(call.message)
    elif action == "sub":
        send_subscription_file(call.message)
    elif action == "status":
        send_status(call.message)
    elif action == "guide":
        show_guide_menu(chat_id, msg_id)
    elif action == "my_orders":
        show_my_orders_menu(chat_id, call.from_user.id, msg_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("donate_qr:"))
def handle_donate_qr(call):
    network = call.data.split(":", 1)[1]
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if network == "tron":
        addr = crypto_manager.TRON_WALLET
        title = "شبکه ترون (Tron Network - TRX / USDT-TRC20)"
    else:
        addr = crypto_manager.ETH_WALLET
        title = "شبکه اتریوم (Ethereum Network - ETH / USDT-ERC20)"

    qr_bytes = crypto_manager.generate_qr_bytes(addr)
    caption = (
        f"💖 <b>بارکد واریز دونیت - {title}</b>\n\n"
        f"📍 <b>آدرس کیف پول:</b>\n"
        f"<code>{addr}</code>\n\n"
        f"<i>(روی آدرس ضربه بزنید تا کپی شود)</i>\n"
        f"از همراهی و حمایت شما بی‌نهایت سپاسگزاریم! 🙏"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔙 بازگشت به بخش حمایت مالی", callback_data="menu:donate"))
    send_photo_safe(call.message.chat.id, qr_bytes, caption=caption, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_cat:"))
def handle_buy_cat_callback(call):
    dev_str = call.data.split(":", 1)[1]
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if dev_str.isdigit():
        show_volume_menu(call.message.chat.id, int(dev_str), message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_vol:"))
def handle_buy_vol_callback(call):
    parts = call.data.split(":")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        show_duration_menu(call.message.chat.id, int(parts[1]), int(parts[2]), message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("plan_sel:"))
def handle_plan_selection(call):
    plan_id = call.data.split(":", 1)[1]
    plans = {p["id"]: p for p in get_vip_plans()}
    plan = plans.get(plan_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if not plan:
        send_message_safe(call.message.chat.id, "⚠️ پلن مورد نظر یافت نشد.")
        return

    calc = crypto_manager.calculate_adaptive_prices(plan["price_usd"])
    devices = plan.get("devices", 1)
    volume_gb = plan.get("volume_gb", 30)
    dur_days = plan.get("duration_days", 30)
    dur_map = {30: "۱ ماهه", 90: "۳ ماهه", 180: "۶ ماهه"}
    dur_str = dur_map.get(dur_days, f"{dur_days} روز")
    toman_price = plan.get("price_toman", 0)

    text = (
        f"💎 <b>جزئیات و انتخاب روش پرداخت:</b>\n\n"
        f"▫️ <b>تعداد کاربر همزمان:</b> {devices} کاربره\n"
        f"▫️ <b>حجم ترافیک:</b> {volume_gb} گیگابایت\n"
        f"▫️ <b>مدت اعتبار:</b> {dur_str} ({dur_days} روز)\n"
        f"▫️ <b>مبلغ تومانی:</b> {toman_price:,} تومان\n\n"
        f"💰 <b>مبلغ قابل پرداخت با رمزارز (نرخ لحظه‌ای بازار):</b>\n"
        f"💵 <b>معادل تتر (USDT):</b> ${calc['usdt']:.2f} USDT\n"
        f"🔺 <b>معادل ترون (TRX):</b> ~{calc['trx']} TRX\n"
        f"🔹 <b>معادل اتریوم (ETH):</b> ~{calc['eth']} ETH\n\n"
        f"👇 <i>لطفاً شبکه انتقال رمزارز مورد نظر خود را انتخاب نمایید:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔺 پرداخت در شبکه ترون (TRX / USDT-TRC20)", callback_data=f"pay_net:{plan_id}:tron"),
        types.InlineKeyboardButton("🔹 پرداخت در شبکه اتریوم (ETH / USDT-ERC20)", callback_data=f"pay_net:{plan_id}:eth"),
        types.InlineKeyboardButton("🔙 بازگشت به انتخاب مدت زمان", callback_data=f"buy_vol:{devices}:{volume_gb}")
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception:
        send_message_safe(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_net:"))
def handle_payment_network(call):
    _, plan_id, network = call.data.split(":", 2)
    plans = {p["id"]: p for p in get_vip_plans()}
    plan = plans.get(plan_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if not plan:
        send_message_safe(call.message.chat.id, "⚠️ پلن مورد نظر یافت نشد.")
        return

    calc = crypto_manager.calculate_adaptive_prices(plan["price_usd"])

    if network == "tron":
        wallet_address = crypto_manager.TRON_WALLET
        net_title = "شبکه ترون (Tron Network - TRC20)"
        crypto_curr = "TRX / USDT-TRC20"
        amount_str = f"<b>{calc['trx']} TRX</b> یا <b>${calc['usdt']:.2f} USDT-TRC20</b>"
        crypto_amount = calc['trx']
    else:
        wallet_address = crypto_manager.ETH_WALLET
        net_title = "شبکه اتریوم (Ethereum Network - ERC20)"
        crypto_curr = "ETH / USDT-ERC20"
        amount_str = f"<b>{calc['eth']} ETH</b> یا <b>${calc['usdt']:.2f} USDT-ERC20</b>"
        crypto_amount = calc['eth']

    # Create order in order_manager
    order_id = order_mgr.create_order(
        user_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        plan=plan,
        crypto_network=net_title,
        crypto_currency=crypto_curr,
        crypto_amount=crypto_amount,
    )

    toman_price = plan.get("price_toman", 0)
    dur_days = plan.get("duration_days", 30)
    dur_map = {30: "۱ ماهه", 90: "۳ ماهه", 180: "۶ ماهه"}
    dur_str = dur_map.get(dur_days, f"{dur_days} روز")

    qr_bytes = crypto_manager.generate_qr_bytes(wallet_address)
    caption = (
        f"🧾 <b>فاکتور پرداخت سفارش <code>{order_id}</code></b>\n\n"
        f"📦 <b>پلن:</b> {plan.get('volume_gb', 30)} گیگ | {dur_str} ({plan.get('devices', 1)} کاربره)\n"
        f"💵 <b>مبلغ سفارش:</b> {toman_price:,} تومان (~${plan['price_usd']:.0f} USD)\n"
        f"🌐 <b>شبکه انتقال:</b> {net_title}\n"
        f"💰 <b>مبلغ قابل واریز:</b> {amount_str}\n\n"
        f"📍 <b>آدرس کیف پول جهت واریز:</b>\n"
        f"<code>{wallet_address}</code>\n"
        f"<i>(روی آدرس ضربه بزنید تا کپی شود)</i>\n\n"
        f"⚠️ <b>راهنمای تکمیل خرید:</b>\n"
        f"۱. مبلغ مشخص‌شده را به آدرس بالا انتقال دهید.\n"
        f"۲. پس از انجام انتقال، دکمه <b>«ثبت رسید / شناسه تراکنش (TxID)»</b> را بزنید و کد هش (TxID) یا عکس رسید را ارسال نمایید.\n"
        f"۳. پس از تایید مدیریت، کانفیگ اختصاصی شما به صورت خودکار صادر خواهد شد."
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✅ ثبت رسید / شناسه تراکنش (TxID)", callback_data=f"submit_tx:{order_id}"),
        types.InlineKeyboardButton("🔙 بازگشت به جزئیات پلن", callback_data=f"plan_sel:{plan_id}")
    )
    send_photo_safe(call.message.chat.id, qr_bytes, caption=caption, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_tx:"))
def handle_submit_tx(call):
    order_id = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    order = order_mgr.get_order(order_id)
    if not order:
        send_message_safe(chat_id, "⚠️ سفارش مورد نظر یافت نشد.")
        return

    # IDOR check: Verify order ownership
    if order.get("user_id") != user_id:
        send_message_safe(chat_id, "⛔ شما مجاز به ویرایش این سفارش نیستید.")
        return

    # Disallow submitting for already approved orders
    if order.get("status") == "APPROVED":
        send_message_safe(chat_id, "✅ این سفارش قبلاً تایید و فعال شده است.")
        return

    user_pending_tx_order[chat_id] = {"order_id": order_id, "timestamp": time.time()}
    text = (
        f"📥 <b>ارسال مدرک پرداخت برای سفارش <code>{html.escape(order_id)}</code>:</b>\n\n"
        f"لطفاً در پاسخ به این پیام، <b>کد پیگیری تراکنش (TxID / Hash)</b> یا <b>عکس اسکرین‌شات رسید واریز</b> را ارسال فرمایید.\n\n"
        f"<i>(برای لغو فرآیند ارسال رسید می‌توانید دستور /cancel را ارسال کنید)</i>"
    )
    send_message_safe(chat_id, text, parse_mode="HTML")


@bot.message_handler(commands=['cancel'])
def cmd_cancel_receipt_upload(message):
    chat_id = message.chat.id
    if chat_id in user_pending_tx_order:
        user_pending_tx_order.pop(chat_id, None)
        send_message_safe(chat_id, "❌ فرآیند ارسال رسید پرداخت لغو شد.")
    else:
        send_message_safe(chat_id, "عملیات فعالی جهت لغو وجود ندارد.")


# --- ADMIN ORDER APPROVAL / REJECTION CALLBACKS ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_approve:") or call.data.startswith("admin_reject:"))
def handle_admin_decision(call):
    # STRICT ADMIN AUTHORIZATION CHECK - Fail CLOSED
    if not is_admin(call.from_user):
        try:
            bot.answer_callback_query(call.id, "⛔ شما دسترسی مدیریت ندارید.", show_alert=True)
        except Exception:
            pass
        return

    action, order_id = call.data.split(":", 1)
    order = order_mgr.get_order(order_id)
    if not order:
        try:
            bot.answer_callback_query(call.id, "⚠️ سفارش یافت نشد.")
        except Exception:
            pass
        return

    if order["status"] == "APPROVED":
        try:
            bot.answer_callback_query(call.id, "این سفارش قبلاً تایید شده است.", show_alert=True)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    if order["status"] == "REJECTED":
        try:
            bot.answer_callback_query(call.id, "این سفارش قبلاً رد شده است.", show_alert=True)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    # Immediately remove buttons to prevent double-click race conditions
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if action == "admin_reject":
        order_mgr.reject_order(order_id, reason="رد توسط ادمین")
        try:
            bot.answer_callback_query(call.id, f"سفارش {order_id} رد شد.")
        except Exception:
            pass
        send_message_safe(order["user_id"], f"❌ سفارش <code>{html.escape(order_id)}</code> توسط مدیریت تایید نشد. در صورت بروز هرگونه مشکل با پشتیبانی در ارتباط باشید.", parse_mode="HTML")
        send_message_safe(call.message.chat.id, f"❌ سفارش <code>{html.escape(order_id)}</code> رد شد.", parse_mode="HTML")
        return

    # admin_approve: acquire atomic approval lock
    if not order_mgr.start_approving_order(order_id):
        try:
            bot.answer_callback_query(call.id, "این سفارش در حال پردازش یا قبلاً تکمیل شده است.", show_alert=True)
        except Exception:
            pass
        return

    try:
        bot.answer_callback_query(call.id, f"در حال ایجاد کانفیگ برای {order_id}...")
    except Exception:
        pass

    email_tag = f"tg_{order['user_id']}"
    creation_res = conpanel_mgr.create_customer_subscription(
        email=email_tag,
        total_gb=order["volume_gb"],
        expiry_days=order["duration_days"],
        limit_hwid=order.get("devices", 1),
        tg_id=order["user_id"]
    )

    if not creation_res.get("success"):
        order_mgr.cancel_approving_order(order_id)
        err_msg = creation_res.get("error", "Unknown panel error")
        logger.error("Failed to auto-create client for %s: %s", order_id, err_msg)
        # Re-attach markup so admin can retry after fixing panel
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔄 تلاش مجدد برای تایید", callback_data=f"admin_approve:{order_id}"),
            types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"admin_reject:{order_id}"),
        )
        send_message_safe(call.message.chat.id, f"❌ خطا در ساخت کانفیگ روی سرور برای سفارش <code>{html.escape(order_id)}</code>:\n{html.escape(str(err_msg))}", reply_markup=markup, parse_mode="HTML")
        return

    sub_url = creation_res["sub_url"]
    json_url = creation_res["json_url"]
    order_mgr.approve_order(order_id, sub_url)

    # Deliver to customer
    try:
        qr_bytes = crypto_manager.generate_qr_bytes(sub_url)
        cust_msg = (
            f"🎉 <b>سفارش شما با موفقیت تایید و فعال شد!</b>\n\n"
            f"🆔 <b>کد پیگیری:</b> <code>{html.escape(order_id)}</code>\n"
            f"📦 <b>پلن:</b> {html.escape(str(order['plan_name']))}\n"
            f"📊 <b>حجم ترافیک:</b> {order['volume_gb']} گیگابایت\n"
            f"⏳ <b>مدت اعتبار:</b> {order['duration_days']} روز\n"
            f"👥 <b>تعداد کاربر مجاز:</b> {order.get('devices', 1)} دستگاه\n\n"
            f"🔗 <b>لینک سابسکریپشن اختصاصی شما (برای کپی لمس کنید):</b>\n"
            f"<code>{html.escape(sub_url)}</code>\n\n"
            f"📱 <b>لینک سابسکریپشن مخصوص Sing-box / Clash:</b>\n"
            f"<code>{html.escape(json_url)}</code>\n\n"
            f"💡 <b>راهنمای اتصال:</b>\n"
            f"۱. لینک فوق را کپی کنید یا بارکد QR زیر را در برنامه اسکن فرمایید.\n"
            f"۲. در برنامه <b>v2rayNG</b> یا <b>V2Box</b> یا <b>Streisand</b> به بخش Subscription رفته و Update را بزنید.\n"
            f"۳. <b>نکته مهم برای همراه اول و ایرانسل:</b> در صورت اختلال، در تنظیمات برنامه گزینه <b>Fragment</b> را فعال نمایید.\n\n"
            f"از اعتماد شما به LitixConnect سپاسگزاریم! ❤️"
        )
        send_photo_safe(order["user_id"], qr_bytes, caption=cust_msg, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to deliver subscription to user %s: %s", order["user_id"], e)
        send_message_safe(order["user_id"], f"✅ کانفیگ شما ساخته شد:\n<code>{html.escape(sub_url)}</code>", parse_mode="HTML")

    send_message_safe(call.message.chat.id, f"✅ سفارش <code>{html.escape(order_id)}</code> با موفقیت تایید شد و لینک برای کاربر ارسال گردید.", parse_mode="HTML")


# --- USER PAYMENT RECEIPT LISTENER (Photo or Text TxID) ---
@bot.message_handler(content_types=['text', 'photo'], func=lambda m: bool(get_pending_tx_order(m.chat.id)))
def handle_payment_receipt_upload(message):
    chat_id = message.chat.id
    order_id = get_pending_tx_order(chat_id)
    if not order_id:
        return

    # Check if message is a command
    if message.content_type == 'text' and message.text and message.text.startswith('/'):
        cmd_text = message.text.strip().lower()
        if cmd_text in ('/cancel', '/start', '/help', '/buy', '/orders', '/status'):
            user_pending_tx_order.pop(chat_id, None)
            if cmd_text == '/cancel':
                bot.reply_to(message, "❌ فرآیند ارسال رسید پرداخت لغو شد.")
                return
            # Let other registered handlers process standard commands
            return

    # Clear pending status now that receipt is received
    user_pending_tx_order.pop(chat_id, None)

    order = order_mgr.get_order(order_id)
    if not order:
        bot.reply_to(message, "⚠️ سفارش معتبری یافت نشد.")
        return

    # Check ownership
    if order.get("user_id") != message.from_user.id:
        bot.reply_to(message, "⚠️ این سفارش متعلق به حساب کاربری شما نیست.")
        return

    if order.get("status") == "APPROVED":
        bot.reply_to(message, "✅ این سفارش قبلاً تایید و فعال شده است.")
        return

    tx_hash = None
    photo_file_id = None

    if message.content_type == 'photo':
        photo_file_id = message.photo[-1].file_id
        tx_hash = (message.caption or "ارسالی از طریق عکس رسید").strip()
    else:
        tx_hash = message.text.strip()

    order_mgr.submit_payment_proof(order_id, tx_hash=tx_hash, photo_file_id=photo_file_id, user_id=message.from_user.id)

    # 1. Confirm ONLY to the user (NEVER send admin approval buttons here!)
    bot.reply_to(
        message,
        f"✅ <b>رسید پرداخت شما برای سفارش <code>{html.escape(order_id)}</code> دریافت شد!</b>\n\n"
        f"اطلاعات پرداخت جهت بررسی و تایید به مدیریت ارسال گردید. "
        f"به محض تایید، کانفیگ اختصاصی شما به صورت خودکار در همین چت تحویل داده خواهد شد. "
        f"از صبوری شما سپاسگزاریم! 🙏",
        parse_mode="HTML"
    )

    # 2. Dispatch alert STRICTLY and ONLY to verified Admin(s)
    admin_chats = get_admin_chat_ids()
    if not admin_chats:
        logger.critical(
            "⚠️ CRITICAL ALERT: Payment proof submitted for order %s, "
            "but no active chat found for admin (@awlinavakhtam). "
            "Order is safely saved as PENDING_VERIFICATION in orders.json.",
            order_id
        )
        send_message_safe(
            chat_id,
            "⚠️ <i>رسید پرداخت شما برای سفارش با موفقیت ثبت شد و در صف بررسی مدیریت (@awlinavakhtam) قرار گرفت. "
            "به محض تایید، کانفیگ اختصاصی شما به صورت خودکار در همین چت تحویل داده خواهد شد.</i>",
            parse_mode="HTML"
        )
        return

    safe_oid = html.escape(str(order_id))
    safe_user = html.escape(str(order.get('username') or 'ندارد'))
    safe_uid = html.escape(str(order['user_id']))
    safe_first = html.escape(str(order.get('first_name') or 'کاربر'))
    safe_plan = html.escape(str(order['plan_name']))
    safe_net = html.escape(str(order.get('crypto_network') or 'نامشخص'))
    safe_tx = html.escape(str(tx_hash)[:500])

    admin_alert = (
        f"🔔 <b>رسید پرداخت جدید دریافت شد!</b>\n\n"
        f"🆔 <b>شناسه سفارش:</b> <code>{safe_oid}</code>\n"
        f"👤 <b>کاربر:</b> @{safe_user} (ID: <code>{safe_uid}</code> | نام: {safe_first})\n"
        f"📦 <b>پلن انتخابی:</b> {safe_plan} ({order['volume_gb']}GB / {order['duration_days']} روز)\n"
        f"🌐 <b>شبکه:</b> {safe_net}\n"
        f"💰 <b>مبلغ مورد انتظار:</b> {order['crypto_amount']} {order['crypto_currency']}\n"
        f"🔗 <b>شناسه تراکنش (TxID):</b>\n<code>{safe_tx}</code>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ تایید و صدور خودکار", callback_data=f"admin_approve:{order_id}"),
        types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"admin_reject:{order_id}"),
    )

    for admin_chat in admin_chats:
        if photo_file_id:
            try:
                # If caption fits Telegram's 1024-char limit, send together
                if len(admin_alert) <= 950:
                    send_photo_safe(admin_chat, photo_file_id, caption=admin_alert, reply_markup=markup, parse_mode="HTML")
                else:
                    short_caption = f"🧾 عکس رسید پرداخت سفارش <code>{safe_oid}</code> از @{safe_user}"
                    send_photo_safe(admin_chat, photo_file_id, caption=short_caption, parse_mode="HTML")
                    send_message_safe(admin_chat, admin_alert, reply_markup=markup, parse_mode="HTML")
                continue
            except Exception as e:
                logger.warning("Could not send receipt photo to admin %s: %s", admin_chat, e)

        send_message_safe(admin_chat, admin_alert, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("country:"))
def handle_country_request(call):
    chat_id = call.message.chat.id
    selected_button = call.data.split(":", 1)[1]

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    serve_country_to_chat(chat_id, selected_button)


@bot.message_handler(func=lambda message: message.text in BUTTON_TO_COUNTRY.keys())
def handle_legacy_keyboard(message):
    """Users who still hold the old reply keyboard send plain country text - serve them too."""
    serve_country_to_chat(message.chat.id, message.text.strip())


def serve_country_to_chat(chat_id, selected_button):
    with nodes_lock:
        master_nodes_list = list(categorized_nodes.get(selected_button, []))
    total_available = len(master_nodes_list)

    if total_available == 0:
        bot.send_message(
            chat_id,
            f"⚠️ There are currently zero verified working configs for <b>{selected_button}</b> in cache. Please try again later.",
            parse_mode="HTML"
        )
        return

    with offsets_lock:
        if chat_id not in user_session_offsets:
            user_session_offsets[chat_id] = {k: 0 for k in BUTTON_TO_COUNTRY.keys()}
        current_offset = user_session_offsets[chat_id].get(selected_button, 0)

    inform_msg = ""

    if current_offset >= total_available:
        inform_msg = f"⚠️ <b>Notice:</b> You have already seen all unique configurations for {selected_button}.\n🔄 <i>Resetting your rotation back to the beginning...</i>\n\n"
        current_offset = 0

    start_idx = current_offset
    end_idx = start_idx + 3
    nodes_to_serve = master_nodes_list[start_idx:end_idx]
    served_count = len(nodes_to_serve)

    if served_count < 3 and start_idx != 0:
        inform_msg = f"ℹ️ <b>Notice:</b> Only <b>{served_count}</b> new unique configs were remaining for {selected_button}. Running out of options soon!\n\n"

    if total_available < 3:
        inform_msg = f"ℹ️ <b>Notice:</b> There are only {total_available} total configurations available in the system for this country. Repetition is inevitable.\n\n"

    with offsets_lock:
        user_session_offsets[chat_id][selected_button] = start_idx + served_count

    meta = COUNTRY_DATA.get(selected_button, COUNTRY_DATA["Others"])

    # Send introductory notice
    response_text = f"{inform_msg}✨ <b>Your 3 Verified Configs for {meta['flag']} {selected_button} (Tap to Copy):</b>"
    bot.send_message(chat_id, response_text, parse_mode="HTML")

    # Send each config inside a code block for 1-tap copy on mobile Telegram
    for node in nodes_to_serve:
        send_message_safe(chat_id, f"<code>{html.escape(node)}</code>", parse_mode="HTML")

    # Send full .txt file from disk
    filename = f"{meta['code'].lower()}_configs.txt"
    filepath = script_dir / filename
    if not filepath.exists():
        txt_content = generate_txt_file(master_nodes_list, selected_button)
        filepath.write_text(txt_content, encoding='utf-8')

    try:
        with open(filepath, 'rb') as doc:
            send_document_safe(
                chat_id,
                doc,
                visible_file_name=filename,
                caption=(
                    f"📄 <b>All {total_available} Configs for {selected_button}</b>\n"
                    f"📅 Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"🔗 Channel: {CHANNEL_ID}"
                ),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning("Failed to send .txt file: %s", e)


if __name__ == "__main__":
    cleanup_xray_temp_files()
    load_state()

    for attempt in range(5):
        try:
            me = bot.get_me()
            bot_username = me.username
            logger.info("Connected to Telegram Bot: @%s (ID: %s)", bot_username, me.id)
            break
        except Exception as e:
            logger.warning("Could not fetch bot identity (attempt %d/5): %s", attempt + 1, e)
            time.sleep(2)

    # Register Bot Menu Commands with Telegram
    try:
        bot.set_my_commands([
            types.BotCommand("start", "منوی اصلی ربات | Main Menu"),
            types.BotCommand("buy", "خرید کانفیگ اختصاصی VIP | Buy VIP"),
            types.BotCommand("donate", "حمایت مالی از سرورها | Donation"),
            types.BotCommand("orders", "پیگیری سفارشات من | My Orders"),
            types.BotCommand("top", "دریافت فایل ۵ کشور برتر | Top 5"),
            types.BotCommand("sub", "سابسکریپشن همگانی رایگان | All-in-One Sub"),
            types.BotCommand("status", "وضعیت سرورهای رایگان | Server Status"),
            types.BotCommand("help", "راهنمای اتصال و دانلود برنامه‌ها | Guide"),
        ])
        logger.info("Registered Telegram bot commands menu")
    except Exception as e:
        logger.warning("Failed to register bot commands: %s", e)

    updater_thread = threading.Thread(target=update_configs_loop, daemon=True)
    updater_thread.start()

    logger.info("Resilient Telegram operational routing loop initializing...")
    threading.Thread(target=ensure_xray_binary, daemon=True).start()
    while True:
        try:
            logger.info("Starting Telegram bot polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error("Polling encountered an error: %s", e)
            time.sleep(15)
