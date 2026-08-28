import os
import base64
import json
import re
import socket
import time
import threading
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import requests
import telebot
from telebot import types
import geoip2.database
from requests.exceptions import ProxyError

# --- CONTEXT-AWARE CONFIGURATION & ENV LOADING ---
script_dir = Path(__file__).parent
print(f"📁 Workspace active directory: {script_dir}")

load_dotenv(dotenv_path=script_dir / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@litixconnect")

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

# --- PROXY MIRROR TARGETS (Original Sources) ---
SOURCES = [
    "https://ghproxy.net/https://raw.githubusercontent.com/wenxig/free-nodes-sub/main/data/sub.txt",
    "https://ghproxy.net/https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/main/Z.txt",
    "https://ghproxy.net/https://raw.githubusercontent.com/roosterkid/openproxylist/main/V2RAY.txt"
]

# --- AU1RXX GITHUB SOURCE (Country-specific v2ray configs) ---
AU1RXX_BASE = "https://raw.githubusercontent.com/Au1rxx/free-vpn-subscriptions/main/output/country"
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

def test_single_node(line, current_idx, total_nodes):
    host, port = extract_host_and_port(line)
    if not host or not port:
        return None

    try:
        socket.setdefaulttimeout(2.0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
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
            return f"{base_part}#{new_remark}"
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
    """Fetch v2ray configs from Au1rxx GitHub repo for all supported countries"""
    configs_by_country = {country: [] for country in COUNTRY_DATA.keys()}

    for country_code, country_name in AU1RXX_COUNTRIES.items():
        try:
            url = f"{AU1RXX_BASE}/{country_code}/v2ray-base64-0001.txt"
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                lines = decode_base64_content(res.text)
                valid_lines = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
                configs_by_country[country_name].extend(valid_lines)
                print(f"✅ Fetched {len(valid_lines)} configs for {country_name} ({country_code}) from Au1rxx")
            else:
                print(f"⚠️ No Au1rxx config for {country_name} ({country_code}): HTTP {res.status_code}")
        except Exception as e:
            print(f"⚠️ Au1rxx fetch error for {country_name} ({country_code}): {e}")

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

def post_to_channel(country_name, configs):
    """Post configs for a country to the Telegram channel"""
    if not CHANNEL_ID:
        print("⚠️ CHANNEL_ID not set, skipping channel post")
        return False

    try:
        total = len(configs)
        if total == 0:
            return False

        meta = COUNTRY_DATA.get(country_name, COUNTRY_DATA["Others"])
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        # Generate .txt file content
        txt_content = generate_txt_file(configs, country_name)
        filename = f"{meta['code'].lower()}_configs.txt"

        # Create temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(txt_content)
            temp_path = f.name

        # Caption for the post
        caption = (
            f"{meta['flag']} <b>{country_name}</b> - {total} Working Configs\n"
            f"📅 Updated: {timestamp}\n"
            f"🔗 Channel: {CHANNEL_ID}"
        )

        with open(temp_path, 'rb') as doc:
            bot.send_document(
                CHANNEL_ID,
                doc,
                visible_file_name=filename,
                caption=caption,
                parse_mode="HTML"
            )

        os.unlink(temp_path)
        print(f"✅ Posted {country_name} ({total} configs) to channel")
        return True

    except Exception as e:
        print(f"⚠️ Failed to post {country_name} to channel: {e}")
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
    print(f"📢 Posting all countries to channel {CHANNEL_ID}...")
    posted = 0
    for country_name, lines in categorized_nodes.items():
        if lines and country_name != "Others":
            if post_to_channel(country_name, lines):
                posted += 1
                time.sleep(1)  # Rate limit protection

    try:
        banner_path = create_update_banner()
        total = sum(len(lines) for lines in categorized_nodes.values())
        caption = (
            f"✅ <b>Update Complete!</b>\n"
            f"📦 {total} verified configs posted across {posted} countries\n"
            f"🔗 {CHANNEL_ID}"
        )
        with open(banner_path, 'rb') as photo:
            bot.send_photo(CHANNEL_ID, photo, caption=caption, parse_mode="HTML")
        print("🖼️ Posted update banner to channel")
    except Exception as e:
        print(f"⚠️ Failed to post update banner: {e}")

    print(f"✅ Posted {posted} countries to channel")

# --- CORE ASYNCHRONOUS POOL ENGINE ---

def update_configs_loop():
    global categorized_nodes

    while True:
        print("\n🔄 Starting high-speed concurrent configuration update...")
        temp_storage = {k: [] for k in BUTTON_TO_COUNTRY.keys()}
        raw_lines = []

        # 1. Fetch from original sources
        for url in SOURCES:
            try:
                res = requests.get(url, timeout=10)
                if res.status_code == 200:
                    content = res.text
                    lines = decode_base64_content(content)
                    raw_lines.extend(lines)
            except Exception as e:
                print(f"⚠️ Original source read exception: {e}")

        # 2. Fetch from Au1rxx GitHub (country-specific)
        print("📥 Fetching from Au1rxx GitHub repository...")
        au1rxx_configs = fetch_au1rxx_configs()
        for country_name, lines in au1rxx_configs.items():
            if country_name in temp_storage:
                temp_storage[country_name].extend(lines)
            elif country_name == "Others" and "Others" in temp_storage:
                temp_storage["Others"].extend(lines)

        # 3. Test all unique configs from original sources
        unique_original = list(set([line.strip() for line in raw_lines if line.strip()]))
        total_count = len(unique_original)
        print(f"🔍 Discovered {total_count} original nodes. Launching multi-threaded pipeline...")

        active_found = 0
        with ThreadPoolExecutor(max_workers=70) as executor:
            futures = {executor.submit(test_single_node, line, i, total_count): line for i, line in enumerate(unique_original, 1)}

            for future in as_completed(futures):
                result = future.result()
                if result:
                    active_found += 1
                    bucket = result["bucket"]
                    temp_storage[bucket].append(result["raw_line"])

        # 4. Au1rxx configs are already country-sorted, just test connectivity
        print("🔍 Testing Au1rxx pre-sorted configs...")
        for country_name, lines in au1rxx_configs.items():
            if country_name not in temp_storage:
                continue
            bucket = country_name
            tested = 0
            with ThreadPoolExecutor(max_workers=30) as executor:
                futures = {executor.submit(test_single_node, line, i, len(lines)): line for i, line in enumerate(lines, 1)}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        active_found += 1
                        temp_storage[bucket].append(result["raw_line"])
                    tested += 1

        print(f"✅ Scanning complete. Found {active_found} live nodes. Rebranding lists...")

        # 5. Rebrand all configs
        for bucket, lines in temp_storage.items():
            country_data_key = BUTTON_TO_COUNTRY[bucket]
            temp_storage[bucket] = [rebrand_config(line, country_data_key, idx) for idx, line in enumerate(lines, 1)]

        categorized_nodes = temp_storage

        # 6. Generate and save .txt files for each country
        print("📄 Generating .txt files for each country...")
        for country_name, lines in categorized_nodes.items():
            if lines and country_name != "Others":
                txt_content = generate_txt_file(lines, country_name)
                filename = f"{COUNTRY_DATA[country_name]['code'].lower()}_configs.txt"
                filepath = script_dir / filename
                try:
                    filepath.write_text(txt_content, encoding='utf-8')
                    print(f"💾 Saved {len(lines)} configs to {filename}")
                except Exception as e:
                    print(f"⚠️ Failed to save {filename}: {e}")

        # 7. Post to Telegram channel
        post_all_countries_to_channel()

        print("🎉 High-speed background sync complete. Core memory updated. Next interval sweep in 2 hours.")
        time.sleep(7200)

# --- BOT COMMANDS ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    countries = [c for c in BUTTON_TO_COUNTRY.keys() if c != "Others"]
    markup = types.ReplyKeyboardMarkup(row_width=3, resize_keyboard=True)
    buttons = [types.KeyboardButton(c) for c in countries]
    buttons.append(types.KeyboardButton("Others"))
    markup.add(*buttons)

    country_list = ", ".join(countries[:10]) + "..."

    bot.reply_to(
        message,
        f"Welcome to the **LitixConnect Service**!\n\n"
        f"📍 **{len(countries)} Countries Available**: {country_list}\n\n"
        f"Select a location button to receive **3 fresh configs** + **full .txt file** with all configs for that country.\n\n"
        f"🔗 Channel: {CHANNEL_ID}",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['post'])
def manual_post(message):
    """Manual command to post all countries to channel"""
    if message.chat.type == 'private':
        bot.reply_to(message, "📢 Posting all countries to channel...")
        threading.Thread(target=post_all_countries_to_channel, daemon=True).start()
        bot.reply_to(message, "✅ Posting started in background!")

@bot.message_handler(func=lambda message: message.text in BUTTON_TO_COUNTRY.keys())
def handle_country_request(message):
    chat_id = message.chat.id
    selected_button = message.text

    master_nodes_list = categorized_nodes.get(selected_button, [])
    total_available = len(master_nodes_list)

    if total_available == 0:
        bot.reply_to(message, f"⚠️ There are currently zero verified working configs for **{selected_button}** in cache. Please try again later.")
        return

    with offsets_lock:
        if chat_id not in user_session_offsets:
            user_session_offsets[chat_id] = {k: 0 for k in BUTTON_TO_COUNTRY.keys()}
        current_offset = user_session_offsets[chat_id][selected_button]

    inform_msg = ""

    if current_offset >= total_available:
        inform_msg = f"⚠️ **Notice:** You have already seen all unique configurations for {selected_button}.\n🔄 *Resetting your rotation back to the beginning...*\n\n"
        current_offset = 0

    start_idx = current_offset
    end_idx = start_idx + 3
    nodes_to_serve = master_nodes_list[start_idx:end_idx]
    served_count = len(nodes_to_serve)

    if served_count < 3 and start_idx != 0:
        inform_msg = f"ℹ️ **Notice:** Only **{served_count}** new unique configs were remaining for {selected_button}. Running out of options soon!\n\n"

    if total_available < 3:
        inform_msg = f"ℹ️ **Notice:** There are only {total_available} total configurations available in the system for this country. Repetition is inevitable.\n\n"

    with offsets_lock:
        user_session_offsets[chat_id][selected_button] = start_idx + served_count

    # Send the 3 configs as text message
    response_text = f"{inform_msg}✨ **Your 3 Verified Configs for {selected_button}:**\n\n"
    for node in nodes_to_serve:
        response_text += f"`{node}`\n\n"

    bot.reply_to(message, response_text, parse_mode="Markdown")

    # Also send the full .txt file
    if total_available > 0:
        txt_content = generate_txt_file(master_nodes_list, selected_button)
        filename = f"{COUNTRY_DATA[selected_button]['code'].lower()}_configs.txt"

        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(txt_content)
                temp_path = f.name

            with open(temp_path, 'rb') as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    visible_file_name=filename,
                    caption=f"📄 **All {total_available} Configs for {selected_button}**\n"
                           f"📅 Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                           f"🔗 Channel: {CHANNEL_ID}"
                )

            os.unlink(temp_path)
        except Exception as e:
            print(f"⚠️ Failed to send .txt file: {e}")

if __name__ == "__main__":
    updater_thread = threading.Thread(target=update_configs_loop, daemon=True)
    updater_thread.start()

    print("🤖 Resilient Telegram operational routing loop initializing...")
    while True:
        try:
            print("Starting Telegram bot polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"Polling encountered an error: {e}")
            time.sleep(15)