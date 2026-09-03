import base64
import html
import json
import logging
import os
import random
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
import geoip2.database

# --- LOGGING SETUP (timestamps + levels, ready for Railway logs) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ConfigBot")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("TeleBot").setLevel(logging.WARNING)

# --- CONTEXT-AWARE CONFIGURATION & ENV LOADING ---
script_dir = Path(__file__).parent
logger.info("Workspace active directory: %s", script_dir)

load_dotenv(dotenv_path=script_dir / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@litixconnect")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # optional: locks /post to one chat id

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
user_session_offsets = {}
offsets_lock = threading.Lock()
last_update_time = None  # human-readable UTC string of the last successful scan

MAX_TRACKED_USERS = 2000   # cap on user_session_offsets to bound memory
UPDATE_INTERVAL = 2 * 60 * 60  # 2 hours, aligned to even UTC hour boundaries
STATE_PATH = script_dir / "bot_state.json"
TOP_PICKS_COUNTRIES = 5      # quick-picks file covers the countries with the most live configs
TOP_PICKS_PER_COUNTRY = 50   # random picks per top country, never two on the same address


# --- STATE PERSISTENCE (survives process restarts) ---
def save_state():
    """Persist cache + rotation state so a restart doesn't start cold."""
    try:
        with offsets_lock:
            offsets_snapshot = {str(k): v for k, v in user_session_offsets.items()}
        state = {"nodes": categorized_nodes, "offsets": offsets_snapshot, "last_update": last_update_time}
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
        for key in categorized_nodes:
            if isinstance(nodes.get(key), list):
                categorized_nodes[key] = [l for l in nodes[key] if isinstance(l, str)]
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
        total = sum(len(v) for v in categorized_nodes.values())
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


# --- UTILITY PARSING AND TESTING PIPELINES ---
def extract_host_and_port(config_line):
    try:
        if config_line.startswith("vmess://"):
            b64_data = config_line.replace("vmess://", "").strip()
            b64_data += "=" * ((4 - len(b64_data) % 4) % 4)
            decoded = base64.b64decode(b64_data).decode('utf-8')
            data = json.loads(decoded)
            return data.get("add"), int(data.get("port"))

        elif any(config_line.startswith(p) for p in ["vless://", "ss://", "trojan://", "ssr://"]):
            match = re.search(r'@([^:]+):([0-9]+)', config_line)
            if match:
                return match.group(1), int(match.group(2))
    except Exception:
        pass
    return None, None


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
    scheme = config_line.split("://", 1)[0].lower()
    return f"{scheme}://{host.lower()}:{port}"


def test_single_node(line):
    host, port = extract_host_and_port(line)
    if not host or not port:
        return None

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))

        country_name = get_country_local(host)

        assigned_bucket = "Others"
        for bot_button, full_country_name in BUTTON_TO_COUNTRY.items():
            if country_name == full_country_name:
                assigned_bucket = bot_button
                break

        return {"bucket": assigned_bucket, "raw_line": line}
    except (socket.timeout, socket.error):
        return None


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

def fetch_au1rxx_configs():
    """Fetch v2ray configs from Au1rxx GitHub repo for all supported countries.

    Bigger countries publish multiple parts (v2ray-base64-0001.txt, -0002.txt, ...);
    we walk them until the first 404 so no configs are left behind.
    """
    configs_by_country = {country: [] for country in COUNTRY_DATA.keys()}

    for country_code, country_name in AU1RXX_COUNTRIES.items():
        fetched = 0
        for part in range(1, AU1RXX_PARTS + 1):
            url = f"{AU1RXX_BASE}/{country_code}/v2ray-base64-{part:04d}.txt"
            try:
                res = http_get(url, timeout=15)
                if res.status_code == 404:
                    break  # no more parts for this country
                if res.status_code != 200:
                    logger.warning("Au1rxx %s part %d: HTTP %d", country_code, part, res.status_code)
                    break
                lines = decode_base64_content(res.text)
                valid_lines = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
                configs_by_country[country_name].extend(valid_lines)
                fetched += len(valid_lines)
            except Exception as e:
                logger.warning("Au1rxx fetch error for %s (%s) part %d: %s", country_name, country_code, part, e)
                break
        if fetched:
            logger.info("Fetched %d configs for %s (%s) from Au1rxx", fetched, country_name, country_code)

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
    """Post configs for a country to the Telegram channel"""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set, skipping channel post")
        return False

    total = len(configs)
    if total == 0:
        return False

    try:
        meta = COUNTRY_DATA.get(country_name, COUNTRY_DATA["Others"])
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        txt_content = generate_txt_file(configs, country_name)
        filename = f"{meta['code'].lower()}_configs.txt"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(txt_content)
            temp_path = f.name

        caption = (
            f"{meta['flag']} <b>{country_name}</b> - {total} Working Configs\n"
            f"📅 Updated: {timestamp}\n"
            f"🔗 Channel: {CHANNEL_ID}"
        )

        with open(temp_path, 'rb') as doc:
            send_document_safe(
                CHANNEL_ID,
                doc,
                visible_file_name=filename,
                caption=caption,
                parse_mode="HTML"
            )

        os.unlink(temp_path)
        logger.info("Posted %s (%d configs) to channel", country_name, total)
        return True

    except Exception as e:
        logger.warning("Failed to post %s to channel: %s", country_name, e)
        return False


def create_update_banner():
    """Generate a visual banner image summarizing the latest config update"""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1280, 800
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    top = (15, 23, 42)
    bottom = (76, 29, 149)
    for y in range(height):
        t = y / height
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    def font(size):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    title_font = font(64)
    sub_font = font(32)
    small_font = font(26)
    count_font = font(28)

    draw.text((width // 2, 80), "LitixConnect", font=title_font, fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, 145), "Fresh VPN Configs Updated", font=sub_font, fill=(165, 180, 252), anchor="mm")
    draw.text((width // 2, 190), time.strftime("%Y-%m-%d  %H:%M UTC", time.gmtime()), font=small_font, fill=(148, 163, 184), anchor="mm")

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

    total = sum(len(lines) for lines in categorized_nodes.values())
    draw.text((width // 2, height - 110), f"{total} verified configs across {len(entries)} countries", font=sub_font, fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, height - 55), CHANNEL_ID, font=small_font, fill=(165, 180, 252), anchor="mm")

    banner_path = script_dir / "update_banner.png"
    img.save(banner_path)
    return banner_path


def post_all_countries_to_channel():
    """Post all countries with configs to the channel"""
    logger.info("Posting all countries to channel %s...", CHANNEL_ID)
    posted = 0
    for country_name, lines in categorized_nodes.items():
        if lines and country_name != "Others":
            if post_to_channel(country_name, lines):
                posted += 1
                time.sleep(3.5)  # Telegram channels allow ~20 msgs/min; 3.5s keeps us under it

    try:
        banner_path = create_update_banner()
        total = sum(len(lines) for lines in categorized_nodes.values())
        caption = (
            f"✅ <b>Update Complete!</b>\n"
            f"📦 {total} verified configs posted across {posted} countries\n"
            f"🔗 {CHANNEL_ID}"
        )
        with open(banner_path, 'rb') as photo:
            send_photo_safe(CHANNEL_ID, photo, caption=caption, parse_mode="HTML")
        logger.info("Posted update banner to channel")
    except Exception as e:
        logger.warning("Failed to post update banner: %s", e)

    try:
        top_picks = build_top_picks()
        if top_picks:
            picks_line = ", ".join(f"{COUNTRY_DATA[name]['flag']} {name} ×{n}" for name, n in top_picks["countries"])
            caption = (
                "⚡ <b>Quick Picks - Top 5 Countries</b>\n"
                f"{picks_line}\n\n"
                "50 hand-picked configs per country, no duplicate servers - "
                "a small file for users who don't want to scan the big ones.\n"
                f"🔗 {CHANNEL_ID}"
            )
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(top_picks["content"])
                picks_path = f.name
            with open(picks_path, 'rb') as doc:
                send_document_safe(CHANNEL_ID, doc, visible_file_name="top5_quick_picks.txt",
                                   caption=caption, parse_mode="HTML")
            os.unlink(picks_path)
            logger.info("Posted top-5 quick picks file to channel")
    except Exception as e:
        logger.warning("Failed to post quick picks file: %s", e)

    try:
        sub_content = generate_subscription_content()
        if sub_content:
            sub_name = "litixconnect_subscription.txt"
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(sub_content)
                sub_path = f.name
            caption = (
                "📦 <b>All-in-one Subscription File</b>\n"
                "Import this file in v2rayNG / V2Box / Nekoray to load every config at once.\n"
                f"🔗 {CHANNEL_ID}"
            )
            with open(sub_path, 'rb') as doc:
                send_document_safe(CHANNEL_ID, doc, visible_file_name=sub_name, caption=caption, parse_mode="HTML")
            os.unlink(sub_path)
            logger.info("Posted combined subscription file to channel")
    except Exception as e:
        logger.warning("Failed to post subscription file: %s", e)

    logger.info("Posted %d countries to channel", posted)

# --- CORE ASYNCHRONOUS POOL ENGINE ---
def seconds_until_next_aligned_slot(now=None):
    """Wait until the next even UTC hour boundary (00:00, 02:00, 04:00 ...) so posts land on a predictable schedule."""
    now = now or time.time()
    next_slot = (int(now // UPDATE_INTERVAL) + 1) * UPDATE_INTERVAL
    return max(1.0, next_slot - now)


def update_configs_loop():
    global categorized_nodes, last_update_time

    while True:
        logger.info("Starting high-speed concurrent configuration update...")
        temp_storage = {k: [] for k in BUTTON_TO_COUNTRY.keys()}
        raw_lines = []
        seen_keys = set()  # global dedup across ALL sources (same host:port never served twice)

        # 1. Fetch from original sources
        for url in SOURCES:
            try:
                res = http_get(url, timeout=10)
                if res.status_code == 200:
                    lines = decode_base64_content(res.text)
                    raw_lines.extend(lines)
                else:
                    logger.warning("Original source %s returned HTTP %d", url, res.status_code)
            except Exception as e:
                logger.warning("Original source read exception: %s", e)

        # 2. Fetch from Au1rxx GitHub (country-specific, multi-part)
        logger.info("Fetching from Au1rxx GitHub repository...")
        au1rxx_configs = fetch_au1rxx_configs()
        for country_name, lines in au1rxx_configs.items():
            if country_name in temp_storage:
                temp_storage[country_name].extend(lines)

        # 3. Test all unique configs from original sources
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

        logger.info("Discovered %d original nodes. Launching multi-threaded pipeline...", len(unique_original))

        active_found = 0
        with ThreadPoolExecutor(max_workers=70) as executor:
            futures = [executor.submit(test_single_node, line) for line in unique_original]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    active_found += 1
                    temp_storage[result["bucket"]].append(result["raw_line"])

        # 4. Au1rxx configs: already country-sorted, test connectivity + dedup against everything above
        logger.info("Testing Au1rxx pre-sorted configs...")
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
            with ThreadPoolExecutor(max_workers=30) as executor:
                futures = [executor.submit(test_single_node, line) for line in bucket_lines]
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        active_found += 1
                        temp_storage[country_name].append(result["raw_line"])

        total_found = sum(len(v) for v in temp_storage.values())

        # 5. Empty-scan guard: never wipe the channel's content because one source hiccupped
        if total_found == 0:
            logger.warning("Scan found 0 live nodes - keeping previous cache (%d configs) and retrying sooner",
                           sum(len(v) for v in categorized_nodes.values()))
            time.sleep(120)
            continue

        # 6. Rebrand all configs
        for bucket, lines in temp_storage.items():
            country_data_key = BUTTON_TO_COUNTRY[bucket]
            temp_storage[bucket] = [rebrand_config(line, country_data_key, idx) for idx, line in enumerate(lines, 1)]

        categorized_nodes = temp_storage
        last_update_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

        # 7. Generate and save .txt files for each country
        logger.info("Generating .txt files for each country...")
        for country_name, lines in categorized_nodes.items():
            if lines and country_name != "Others":
                txt_content = generate_txt_file(lines, country_name)
                filename = f"{COUNTRY_DATA[country_name]['code'].lower()}_configs.txt"
                filepath = script_dir / filename
                try:
                    filepath.write_text(txt_content, encoding='utf-8')
                    logger.info("Saved %d configs to %s", len(lines), filename)
                except Exception as e:
                    logger.warning("Failed to save %s: %s", filename, e)

        # 8. Persist state, then post to Telegram channel
        prune_offsets()
        save_state()
        post_all_countries_to_channel()

        logger.info("Background sync complete. %d live nodes cached. Next sweep at the next even UTC hour.",
                    total_found)
        time.sleep(seconds_until_next_aligned_slot())


# --- BOT COMMANDS ---
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
    return markup


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    countries = [c for c in BUTTON_TO_COUNTRY.keys() if c != "Others"]
    last_line = f"🕒 Last update: {last_update_time}" if last_update_time else "🕒 First scan still in progress..."

    bot.reply_to(
        message,
        f"Welcome to the LitixConnect Service!\n\n"
        f"📍 <b>{len(countries)} Countries Available</b> - tap one below to receive <b>3 fresh configs</b> "
        f"plus the <b>full .txt file</b> with every config for that country.\n\n"
        f"⚡ Short on time? Send <b>/top</b> for a small file with 50 diverse configs from each of the "
        f"top 5 countries.\n"
        f"📊 Send <b>/status</b> to see live counts per country.\n\n"
        f"{last_line}\n"
        f"🔗 Channel: {CHANNEL_ID}",
        reply_markup=build_country_inline_keyboard(),
        parse_mode="HTML"
    )


@bot.message_handler(commands=['status'])
def send_status(message):
    """Per-country counts + last update time so users know how fresh configs are."""
    lines = []
    total = 0
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

    last_line = f"🕒 Last update: {last_update_time}" if last_update_time else "🕒 First scan still in progress..."
    bot.reply_to(
        message,
        f"📊 <b>Current Cache Status</b>\n\n{body}\n\n📦 Total: {total} configs\n{last_line}\n🔗 Channel: {CHANNEL_ID}",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['post'])
def manual_post(message):
    """Manual command to post all countries to channel"""
    if ADMIN_CHAT_ID and str(message.chat.id) != ADMIN_CHAT_ID:
        bot.reply_to(message, "⛔ This command is restricted.")
        return
    bot.reply_to(message, "📢 Posting all countries to channel...")
    threading.Thread(target=post_all_countries_to_channel, daemon=True).start()
    bot.reply_to(message, "✅ Posting started in background!")


@bot.message_handler(commands=['top'])
def send_top_picks(message):
    """On-demand top-5 quick picks file (small, diverse, no duplicate servers)"""
    total = sum(len(v) for v in categorized_nodes.values())
    if total == 0:
        bot.reply_to(message, "⚠️ No configs cached yet - the first scan may still be running. Try again in a few minutes.")
        return

    top_picks = build_top_picks()
    if not top_picks:
        bot.reply_to(message, "⚠️ Couldn't build quick picks right now. Try again after the next update.")
        return

    picks_line = ", ".join(f"{COUNTRY_DATA[name]['flag']} {name} ×{n}" for name, n in top_picks["countries"])
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(top_picks["content"])
            temp_path = f.name

        with open(temp_path, 'rb') as doc:
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
        os.unlink(temp_path)
    except Exception as e:
        logger.warning("Failed to send quick picks: %s", e)
        bot.reply_to(message, "⚠️ Couldn't send the quick picks file. Try again shortly.")


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
    master_nodes_list = categorized_nodes.get(selected_button, [])
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
        current_offset = user_session_offsets[chat_id][selected_button]

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

    # Send the 3 configs as a monospace text block (plain text - no parse errors possible)
    response_text = f"{inform_msg}✨ <b>Your 3 Verified Configs for {selected_button}:</b>\n\n"
    bot.send_message(chat_id, response_text, parse_mode="HTML")

    for node in nodes_to_serve:
        # plain text, no parse mode: configs can contain any characters without breaking Telegram
        bot.send_message(chat_id, node)

    # Also send the full .txt file
    txt_content = generate_txt_file(master_nodes_list, selected_button)
    filename = f"{COUNTRY_DATA[selected_button]['code'].lower()}_configs.txt"

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(txt_content)
            temp_path = f.name

        with open(temp_path, 'rb') as doc:
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

        os.unlink(temp_path)
    except Exception as e:
        logger.warning("Failed to send .txt file: %s", e)


if __name__ == "__main__":
    load_state()

    updater_thread = threading.Thread(target=update_configs_loop, daemon=True)
    updater_thread.start()

    logger.info("Resilient Telegram operational routing loop initializing...")
    while True:
        try:
            logger.info("Starting Telegram bot polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error("Polling encountered an error: %s", e)
            time.sleep(15)
