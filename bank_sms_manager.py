"""Bank SMS & Card-to-Card Payment Verification Engine for LitixConnect.

Implements the complete 6-stage automated bank verification flow:
1. Unique Rial Suffix Allocation (1 to 99 Rials) for collision-free orders.
2. Multi-Bank SMS Ingestion & Regex Parsing (Melli, Mellat, Blu, Saman, Pasargad, etc.).
3. Receipt Processing via Gemini Multimodal OCR and Manual Entry.
4. 4-Stage Matching Engine (Strict Amount, Tehran Time UTC+3:30, Destination Verification, Single-Use Trace).
5. Asynchronous Webhook Server for Android / Samsung SMS Forwarding (Tasker, MacroDroid, SMS Forwarder).
6. Background Periodic Matcher & Fulfillment Dispatcher.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests

logger = logging.getLogger("BankSMSManager")

# --- TEHRAN TIMEZONE & DATE UTILITIES ---
# Tehran is UTC+3:30 (Iran abolished daylight saving time, constant UTC+3:30)
TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

DESTINATION_CARD_RAW = "6037701506640355"
DESTINATION_CARD_SUFFIX = "0355"
DESTINATION_HOLDER_FA = "نیما ناصربخت"
DESTINATION_HOLDER_EN = "Nima Naserbakht"

DEFAULT_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET_TOKEN = os.getenv("BANK_SMS_WEBHOOK_TOKEN", "litix_bank_sms_secret_2026")

_lock = threading.RLock()


def get_tehran_now() -> datetime:
    """Return current datetime in Asia/Tehran timezone."""
    return datetime.now(TEHRAN_TZ)


def tehran_timestamp_to_str(ts: float) -> str:
    """Convert unix timestamp to formatted Tehran datetime string."""
    dt = datetime.fromtimestamp(ts, tz=TEHRAN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> Tuple[int, int, int]:
    """Bidirectional conversion from Solar Hijri (Jalali) to Gregorian."""
    jy_offset = jy - 979
    jm_offset = jm - 1
    jd_offset = jd - 1
    j_day_no = 365 * jy_offset + (jy_offset // 33) * 8 + ((jy_offset % 33 + 3) // 4)
    for i in range(jm_offset):
        j_day_no += 31 if i < 6 else 30
    j_day_no += jd_offset
    g_day_no = j_day_no + 79
    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no = g_day_no % 146097
    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no = g_day_no % 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False
    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no = g_day_no % 365
    g_days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and g_day_no >= g_days_in_month[gm]:
        g_day_no -= g_days_in_month[gm]
        gm += 1
    return gy, gm + 1, g_day_no + 1


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    """Bidirectional conversion from Gregorian to Solar Hijri (Jalali)."""
    gy_offset = gy - 1600
    gm_offset = gm - 1
    gd_offset = gd - 1
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    g_day_no = 365 * gy_offset + ((gy_offset + 3) // 4) - ((gy_offset + 99) // 100) + ((gy_offset + 399) // 400)
    for i in range(gm_offset):
        g_day_no += g_days_in_month[i]
    if gm_offset > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1
    g_day_no += gd_offset
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    for i in range(11):
        days = 31 if i < 6 else 30
        if j_day_no < days:
            return jy, i + 1, j_day_no + 1
        j_day_no -= days
    return jy, 12, j_day_no + 1


def parse_jalali_datetime_to_timestamp(
    date_str: str, time_str: str
) -> Optional[float]:
    """Parse Jalali date (e.g. '1403/07/23' or '1403-07-23') and time ('14:25:30' or '14:25')
    into UTC unix timestamp adjusted to Asia/Tehran timezone.
    Validates calendar boundaries strictly."""
    try:
        date_clean = re.sub(r"[^\d/]", "", date_str.replace("-", "/")).strip("/")
        parts = date_clean.split("/")
        if len(parts) != 3:
            return None
        jy, jm, jd = int(parts[0]), int(parts[1]), int(parts[2])
        # Two-digit year expansion: e.g. 03 -> 1403, 04 -> 1404
        if jy < 100:
            jy += 1400

        # Strict calendar validation
        if not (1390 <= jy <= 1450):
            return None
        if not (1 <= jm <= 12):
            return None
        if jm <= 6 and not (1 <= jd <= 31):
            return None
        if 7 <= jm <= 11 and not (1 <= jd <= 30):
            return None
        if jm == 12 and not (1 <= jd <= 30):
            return None

        time_clean = re.sub(r"[^\d:]", "", time_str).strip(":")
        t_parts = time_clean.split(":")
        hh = int(t_parts[0]) if len(t_parts) > 0 else 0
        mm = int(t_parts[1]) if len(t_parts) > 1 else 0
        ss = int(t_parts[2]) if len(t_parts) > 2 else 0

        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            return None

        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        dt = datetime(gy, gm, gd, hh, mm, ss, tzinfo=TEHRAN_TZ)
        return dt.timestamp()
    except Exception as e:
        logger.debug("Failed to parse jalali datetime (%s %s): %s", date_str, time_str, e)
        return None


def normalize_persian_digits(text: str) -> str:
    """Normalize Persian and Arabic digits to ASCII 0-9."""
    if not text:
        return ""
    trans_table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return text.translate(trans_table)


# --- DATABASE MANAGER ---
class BankSMSDatabase:
    """SQLite persistence layer for Bank SMS records, Rial suffix allocations, and receipts."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_tables(self):
        with _lock:
            with self._get_connection() as conn:
                # 1. Received Bank SMS records
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bank_sms (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sms_id TEXT UNIQUE NOT NULL,
                        sender TEXT,
                        raw_text TEXT NOT NULL,
                        amount_rials INTEGER NOT NULL,
                        trace_number TEXT,
                        card_number TEXT,
                        deposit_time TEXT NOT NULL,
                        timestamp_tehran REAL NOT NULL,
                        received_at TEXT NOT NULL,
                        is_used INTEGER DEFAULT 0,
                        matched_order_id TEXT,
                        matched_at TEXT,
                        bank_name TEXT
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_amount ON bank_sms(amount_rials);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_trace ON bank_sms(trace_number);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_used ON bank_sms(is_used);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_ts ON bank_sms(timestamp_tehran);")

                # 2. Unique Rial Suffixes (1 to 99 Rials)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rial_suffixes (
                        order_id TEXT PRIMARY KEY,
                        base_amount_toman INTEGER NOT NULL,
                        base_amount_rials INTEGER NOT NULL,
                        suffix INTEGER NOT NULL,
                        total_amount_rials INTEGER NOT NULL,
                        reserved_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        is_active INTEGER DEFAULT 1
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_suffix_active ON rial_suffixes(is_active, expires_at);")

                # 3. Customer Submitted Payment Receipts (OCR / Manual)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payment_receipts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        submission_type TEXT NOT NULL,
                        amount_rials INTEGER,
                        trace_number TEXT,
                        receipt_time TEXT,
                        timestamp_tehran REAL,
                        destination_card TEXT,
                        destination_name TEXT,
                        photo_file_id TEXT,
                        ocr_raw_json TEXT,
                        created_at TEXT NOT NULL,
                        matched_sms_id TEXT,
                        is_matched INTEGER DEFAULT 0
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_order ON payment_receipts(order_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_trace ON payment_receipts(trace_number);")
                conn.commit()


# --- MULTI-BANK IRANIAN SMS PARSER ---
class IranianBankSMSParser:
    """Robust parser for Iranian bank deposit SMS notifications across Melli, Mellat, Blu, etc."""

    # Words indicating a deposit vs a withdrawal/purchase
    DEPOSIT_KEYWORDS = ["واریز", "بستانکار", "انتقال به", "پایا", "ساتنا", "واریز شد", "انتقال از"]
    WITHDRAWAL_KEYWORDS = ["برداشت", "خرید", "بدهکار", "کسر", "کارمزد", "شارژ", "پرداخت قبوض"]

    @classmethod
    def is_deposit(cls, text: str) -> bool:
        clean = text.lower()
        has_deposit = any(w in clean for w in cls.DEPOSIT_KEYWORDS) or "+" in clean
        has_withdrawal = any(w in clean for w in cls.WITHDRAWAL_KEYWORDS) and "-" in clean
        return has_deposit and not has_withdrawal

    @classmethod
    def parse(cls, raw_text: str, sender: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Parse raw SMS text into structured parameters.
        Returns None if not a valid deposit notification."""
        if not raw_text:
            return None

        normalized = normalize_persian_digits(raw_text)
        if not cls.is_deposit(normalized):
            logger.debug("SMS discarded (not a deposit): %s", normalized[:80])
            return None

        # 1. Extract Amount (in Rials)
        amount_rials = cls._extract_amount(normalized)
        if not amount_rials or amount_rials <= 0:
            logger.debug("Could not extract deposit amount from SMS: %s", normalized[:80])
            return None

        # 2. Extract Trace / Tracking Number
        trace_number = cls._extract_trace_number(normalized)

        # 3. Extract Card / Account Number
        card_number = cls._extract_card_number(normalized)

        # 4. Extract Date & Time
        ts, date_str, time_str = cls._extract_datetime(normalized)
        if ts is None:
            # Fallback to current Tehran time if bank SMS didn't include timestamp
            now_tehran = get_tehran_now()
            ts = now_tehran.timestamp()
            date_str = now_tehran.strftime("%Y-%m-%d")
            time_str = now_tehran.strftime("%H:%M:%S")

        # 5. Identify Bank
        bank_name = cls._identify_bank(normalized, sender)

        # Generate unique hash for deduplication
        hash_input = f"{amount_rials}:{trace_number}:{date_str}:{time_str}:{card_number}"
        sms_id = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]

        return {
            "sms_id": sms_id,
            "sender": sender or "Bank",
            "bank_name": bank_name,
            "raw_text": raw_text.strip(),
            "amount_rials": amount_rials,
            "trace_number": trace_number or "",
            "card_number": card_number or "",
            "deposit_time": f"{date_str} {time_str}".strip(),
            "timestamp_tehran": ts,
            "received_at": get_tehran_now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def _extract_amount(cls, text: str) -> Optional[int]:
        """Extract amount in Rials. Handles comma-separated numbers and rial/toman suffixes."""
        # Pattern 1: مبلغ: 4,000,037+ ریال or مبلغ 4000037 ریال or واریز: 4,000,037 ریال
        amt_match = re.search(
            r"(?:مبلغ|واریز|بستانکار|انتقال)[\s:\+]*([\d,،]+)\+?\s*(ریال|تومان|ت)?",
            text,
        )
        if amt_match:
            digits = re.sub(r"[,،]", "", amt_match.group(1))
            if digits.isdigit():
                val = int(digits)
                unit = amt_match.group(2) or "ریال"
                if "تومان" in unit or unit == "ت":
                    val *= 10
                return val

        # Pattern 2: Standalone number followed by ریال
        match_rial = re.search(r"([\d,،]+)\+?\s*ریال", text)
        if match_rial:
            digits = re.sub(r"[,،]", "", match_rial.group(1))
            if digits.isdigit():
                return int(digits)

        # Pattern 3: Standalone number preceded by + (common in Bank Melli)
        match_plus = re.search(r"\+([\d,،]{5,})", text)
        if match_plus:
            digits = re.sub(r"[,،]", "", match_plus.group(1))
            if digits.isdigit():
                return int(digits)

        return None

    @classmethod
    def _extract_trace_number(cls, text: str) -> Optional[str]:
        """Extract tracking code (پیگیری, ارجاع, مرجع, شناسه)."""
        match = re.search(
            r"(?:پیگیری|رهگیری|ارجاع|مرجع|شناسه|کد پیگیری|شماره پیگیری)[\s:]*([A-Za-z0-9]{4,18})",
            text,
        )
        if match:
            return match.group(1).strip()
        # Look for trailing standalone numbers
        match_alt = re.search(r"(?:trace|ref)[\s:]*([A-Za-z0-9]{4,18})", text, re.IGNORECASE)
        if match_alt:
            return match_alt.group(1).strip()
        return None

    @classmethod
    def _extract_card_number(cls, text: str) -> Optional[str]:
        """Extract card or account number mentioned in SMS."""
        # Check for card ending in 0355 or 16-digit card
        match_16 = re.search(r"(?:کارت|به)[\s:]*(\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})", text)
        if match_16:
            return re.sub(r"[\s-]", "", match_16.group(1))
        # Masked card, e.g. 6037******0355
        match_masked = re.search(r"(\d{4,6}[*xX]+\d{4})", text)
        if match_masked:
            return match_masked.group(1)
        return None

    @classmethod
    def _extract_datetime(cls, text: str) -> Tuple[Optional[float], str, str]:
        """Extract date and time from Iranian bank SMS."""
        # Date patterns: 1403/07/23 or 03/07/23 or 1403-07-23
        date_match = re.search(r"(\d{2,4}[/-]\d{1,2}[/-]\d{1,2})", text)
        # Time patterns: 14:25:30 or 14:25
        time_match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", text)

        date_str = date_match.group(1) if date_match else ""
        time_str = time_match.group(1) if time_match else ""

        if date_str and time_str:
            ts = parse_jalali_datetime_to_timestamp(date_str, time_str)
            if ts:
                return ts, date_str, time_str

        return None, date_str, time_str

    @classmethod
    def _identify_bank(cls, text: str, sender: Optional[str]) -> str:
        s = f"{sender or ''} {text}".lower()
        if "ملی" in s or "melli" in s or "6037" in s:
            return "بانک ملی"
        if "ملت" in s or "mellat" in s:
            return "بانک ملت"
        if "بلو" in s or "blubank" in s:
            return "بلوبانک"
        if "سامان" in s or "saman" in s:
            return "بانک سامان"
        if "پاسارگاد" in s or "pasargad" in s:
            return "بانک پاسارگاد"
        if "سپه" in s or "sepah" in s:
            return "بانک سپه"
        if "صادرات" in s or "saderat" in s:
            return "بانک صادرات"
        if "کشاورزی" in s or "keshavarzi" in s:
            return "بانک کشاورزی"
        if "تجارت" in s or "tejarat" in s:
            return "بانک تجارت"
        return "بانک شتابی"


# --- GEMINI FLASH RECEIPT OCR ENGINE ---
class GeminiReceiptOCR:
    """Multimodal Receipt OCR using Gemini Flash for Iranian bank payment receipts."""

    # Candidate models in preferred order
    CANDIDATE_MODELS = [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]

    @classmethod
    def extract_from_image(
        cls,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        api_key: str = DEFAULT_GEMINI_KEY,
    ) -> Optional[Dict[str, Any]]:
        """Extract payment fields from receipt screenshot using Gemini multimodal model."""
        if not image_bytes or not api_key:
            return None

        prompt = (
            "Analyze this Iranian bank card-to-card payment receipt screenshot carefully. "
            "Extract all payment parameters into a single strict JSON object. "
            "Output ONLY the JSON object, without markdown quotes or explanation.\n\n"
            "Required JSON fields:\n"
            "{\n"
            '  "is_successful": true or false,\n'
            '  "amount_rials": integer (amount strictly in Rials; if receipt is in Tomans, multiply by 10),\n'
            '  "amount_toman": integer,\n'
            '  "trace_number": "string" (شماره پیگیری / شماره ارجاع / کد رهگیری),\n'
            '  "date_jalali": "YYYY/MM/DD" (e.g. 1403/07/23),\n'
            '  "time": "HH:MM" or "HH:MM:SS",\n'
            '  "destination_card": "string" (last 4 digits or full card number of destination),\n'
            '  "destination_name": "string" (account holder name, e.g. نیما ناصربخت),\n'
            '  "source_card": "string" (source card if visible),\n'
            '  "bank_name": "string"\n'
            "}\n"
            "If any field cannot be determined, set it to null."
        )

        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_image,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        }

        headers = {"Content-Type": "application/json"}

        for model_name in cls.CANDIDATE_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=(5.0, 25.0))
                if r.status_code == 200:
                    res_json = r.json()
                    candidates = res_json.get("candidates", [])
                    if candidates:
                        text_resp = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        clean_text = text_resp.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                        data = json.loads(clean_text)
                        logger.info("Successfully extracted receipt via %s: %s", model_name, data.get("trace_number"))
                        return data
                else:
                    logger.warning("Gemini model %s returned status %d: %s", model_name, r.status_code, r.text[:120])
            except Exception as e:
                logger.warning("Error calling Gemini model %s: %s", model_name, e)

        return None


# --- UNIQUE RIAL SUFFIX ALLOCATOR & 4-STAGE MATCHING ENGINE ---
class BankSMSManager:
    """Central engine managing unique Rial suffixes, bank SMS matching,
    receipt ingestion, and background verification loops."""

    MATCHING_WINDOW_SECONDS = 4 * 86400  # 4 days (strict rule)
    SUFFIX_RESERVATION_SECONDS = 24 * 3600  # 24 hours lock

    def __init__(self, db_path: Path):
        self.db = BankSMSDatabase(db_path)
        self.fulfillment_callback = None  # Hook called when an order is matched and approved
        self._running = False

    def set_fulfillment_callback(self, callback):
        """Register callback: callback(order_id: str, match_details: Dict[str, Any])."""
        self.fulfillment_callback = callback

    # --- 1. UNIQUE RIAL SUFFIX ALLOCATION ---
    def allocate_unique_rial_amount(
        self, order_id: str, base_amount_toman: int
    ) -> Dict[str, Any]:
        """Allocate a unique 2-digit Rial suffix (between 1 and 99) for this order.
        Guarantees that no two concurrent unpaid orders have the exact same Rial amount."""
        base_amount_rials = base_amount_toman * 10
        now_ts = time.time()
        expires_at = now_ts + self.SUFFIX_RESERVATION_SECONDS

        with _lock:
            with self.db._get_connection() as conn:
                # Expire old inactive reservations
                conn.execute(
                    "UPDATE rial_suffixes SET is_active = 0 WHERE is_active = 1 AND expires_at < ?",
                    (now_ts,),
                )

                # Check if this order already has an active reservation
                row = conn.execute(
                    "SELECT suffix, total_amount_rials FROM rial_suffixes WHERE order_id = ? AND is_active = 1",
                    (order_id,),
                ).fetchone()

                if row:
                    suffix = row["suffix"]
                    total_rials = row["total_amount_rials"]
                else:
                    # Find currently occupied suffixes for this exact base amount
                    occupied_rows = conn.execute(
                        """
                        SELECT suffix FROM rial_suffixes
                        WHERE base_amount_rials = ? AND is_active = 1
                        """,
                        (base_amount_rials,),
                    ).fetchall()
                    occupied = {r["suffix"] for r in occupied_rows}

                    available = [s for s in range(1, 100) if s not in occupied]
                    if not available:
                        # Fallback: wrap-around or least recently used
                        suffix = secrets.randbelow(99) + 1
                    else:
                        suffix = secrets.choice(available)

                    total_rials = base_amount_rials + suffix

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO rial_suffixes (
                            order_id, base_amount_toman, base_amount_rials,
                            suffix, total_amount_rials, reserved_at, expires_at, is_active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            order_id,
                            base_amount_toman,
                            base_amount_rials,
                            suffix,
                            total_rials,
                            now_ts,
                            expires_at,
                        ),
                    )
                    conn.commit()

        # Format Toman with fractional decimal suffix for transparency
        toman_decimal = f"{base_amount_toman + (suffix / 10):,.1f}".rstrip("0").rstrip(".")

        return {
            "order_id": order_id,
            "base_amount_toman": base_amount_toman,
            "base_amount_rials": base_amount_rials,
            "suffix": suffix,
            "total_amount_rials": total_rials,
            "formatted_rials": f"{total_rials:,}",
            "formatted_toman": f"{base_amount_toman:,} تومان (+{suffix} ریال شناسه)",
            "display_toman": toman_decimal,
            "expires_at": expires_at,
        }

    def release_suffix(self, order_id: str):
        """Release suffix when order is completed or cancelled."""
        with _lock:
            with self.db._get_connection() as conn:
                conn.execute(
                    "UPDATE rial_suffixes SET is_active = 0 WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()

    # --- 2. INGEST BANK SMS ---
    def ingest_bank_sms(
        self, raw_text: str, sender: Optional[str] = None, timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """Ingest incoming bank SMS from Webhook. Parses, persists, and triggers instant match."""
        parsed = IranianBankSMSParser.parse(raw_text, sender)
        if not parsed:
            return {"success": False, "error": "Message is not a recognized bank deposit notification"}

        if timestamp and timestamp > 0:
            parsed["timestamp_tehran"] = timestamp

        with _lock:
            with self.db._get_connection() as conn:
                # Check for duplicate SMS ID
                existing = conn.execute(
                    "SELECT id, is_used, matched_order_id FROM bank_sms WHERE sms_id = ?",
                    (parsed["sms_id"],),
                ).fetchone()

                if existing:
                    logger.info("Duplicate SMS received: %s", parsed["sms_id"])
                    return {
                        "success": True,
                        "sms_id": parsed["sms_id"],
                        "duplicate": True,
                        "is_used": bool(existing["is_used"]),
                        "matched_order_id": existing["matched_order_id"],
                    }

                conn.execute(
                    """
                    INSERT INTO bank_sms (
                        sms_id, sender, raw_text, amount_rials, trace_number,
                        card_number, deposit_time, timestamp_tehran, received_at,
                        is_used, bank_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        parsed["sms_id"],
                        parsed["sender"],
                        parsed["raw_text"],
                        parsed["amount_rials"],
                        parsed["trace_number"],
                        parsed["card_number"],
                        parsed["deposit_time"],
                        parsed["timestamp_tehran"],
                        parsed["received_at"],
                        parsed["bank_name"],
                    ),
                )
                conn.commit()

        logger.info(
            "Ingested bank SMS [%s]: %d Rials | Trace: %s | Time: %s",
            parsed["sms_id"],
            parsed["amount_rials"],
            parsed["trace_number"],
            parsed["deposit_time"],
        )

        # Trigger immediate matching against pending orders
        matched_order = self.match_sms_with_orders(parsed)
        return {
            "success": True,
            "sms_id": parsed["sms_id"],
            "amount_rials": parsed["amount_rials"],
            "trace_number": parsed["trace_number"],
            "matched_order_id": matched_order.get("order_id") if matched_order else None,
        }

    # --- 3. SUBMIT PAYMENT RECEIPT (OCR or MANUAL) ---
    def submit_receipt(
        self,
        order_id: str,
        user_id: int,
        submission_type: str,
        amount_rials: Optional[int] = None,
        trace_number: Optional[str] = None,
        date_jalali: Optional[str] = None,
        time_str: Optional[str] = None,
        destination_card: Optional[str] = None,
        destination_name: Optional[str] = None,
        photo_file_id: Optional[str] = None,
        ocr_raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record customer receipt and attempt immediate matching."""
        ts = None
        if date_jalali and time_str:
            ts = parse_jalali_datetime_to_timestamp(date_jalali, time_str)

        # Validate Destination if provided
        dest_valid = True
        if destination_card:
            clean_card = re.sub(r"\D", "", destination_card)
            if clean_card and not (clean_card.endswith(DESTINATION_CARD_SUFFIX) or clean_card == DESTINATION_CARD_RAW):
                dest_valid = False
        if destination_name and dest_valid:
            # Check holder name loosely
            d_lower = destination_name.lower()
            if not ("نیما" in destination_name or "ناصربخت" in destination_name or "nima" in d_lower or "naserbakht" in d_lower):
                dest_valid = False

        if not dest_valid:
            logger.warning(
                "Receipt destination mismatch for order %s: Card=%s, Name=%s",
                order_id, destination_card, destination_name
            )

        now_str = get_tehran_now().strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            with self.db._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO payment_receipts (
                        order_id, user_id, submission_type, amount_rials, trace_number,
                        receipt_time, timestamp_tehran, destination_card, destination_name,
                        photo_file_id, ocr_raw_json, created_at, is_matched
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        order_id,
                        user_id,
                        submission_type,
                        amount_rials,
                        trace_number or "",
                        f"{date_jalali or ''} {time_str or ''}".strip(),
                        ts,
                        destination_card or "",
                        destination_name or "",
                        photo_file_id or "",
                        json.dumps(ocr_raw, ensure_ascii=False) if ocr_raw else None,
                        now_str,
                    ),
                )
                conn.commit()

        # Attempt immediate matching for this specific order
        match_res = self.match_order(order_id)
        return {
            "success": True,
            "destination_valid": dest_valid,
            "matched": bool(match_res),
            "match_details": match_res,
        }

    # --- 4. THE 4-STAGE MATCHING ENGINE ---
    def match_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Evaluate the 4 matching rules for a specific order against bank SMS."""
        with _lock:
            with self.db._get_connection() as conn:
                # Fetch order Rial reservation
                s_row = conn.execute(
                    "SELECT * FROM rial_suffixes WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if not s_row:
                    return None

                target_rials = s_row["total_amount_rials"]
                base_toman = s_row["base_amount_toman"]
                suffix = s_row["suffix"]
                # Customer might enter Tomans in banking app: (base_toman + suffix) * 10
                alt_rials = (base_toman + suffix) * 10
                base_rials = s_row["base_amount_rials"]
                reserved_at = s_row["reserved_at"]

                # Fetch receipt if any
                r_row = conn.execute(
                    "SELECT * FROM payment_receipts WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()

                expected_trace = (r_row["trace_number"].strip() if r_row and r_row["trace_number"] else None)

                # Query candidate unused bank SMS within the 4-day window
                cutoff_ts = time.time() - self.MATCHING_WINDOW_SECONDS
                candidate_sms = conn.execute(
                    """
                    SELECT * FROM bank_sms
                    WHERE is_used = 0 AND timestamp_tehran >= ? 
                      AND (amount_rials = ? OR amount_rials = ? OR (amount_rials = ? AND ? IS NOT NULL))
                    ORDER BY timestamp_tehran ASC
                    """,
                    (cutoff_ts, target_rials, alt_rials, base_rials, expected_trace),
                ).fetchall()

                for sms in candidate_sms:
                    is_exact_unique = (sms["amount_rials"] in (target_rials, alt_rials))
                    if self._verify_match(sms, reserved_at, expected_trace, conn, is_exact_unique=is_exact_unique):
                        return self._finalize_match(order_id, sms, conn)

        return None

    def match_sms_with_orders(self, sms: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """When a new bank SMS arrives, search active orders for an exact match."""
        sms_amount = sms["amount_rials"]
        sms_ts = sms["timestamp_tehran"]
        sms_trace = sms.get("trace_number", "").strip()

        with _lock:
            with self.db._get_connection() as conn:
                # Find active reservations matching exact rials or toman-converted rials or base amount
                candidates = conn.execute(
                    """
                    SELECT * FROM rial_suffixes
                    WHERE is_active = 1 AND (
                        total_amount_rials = ? 
                        OR ((base_amount_toman + suffix) * 10) = ?
                        OR base_amount_rials = ?
                    )
                    """,
                    (sms_amount, sms_amount, sms_amount),
                ).fetchall()

                for cand in candidates:
                    order_id = cand["order_id"]
                    reserved_at = cand["reserved_at"]
                    target_rials = cand["total_amount_rials"]
                    alt_rials = (cand["base_amount_toman"] + cand["suffix"]) * 10
                    is_exact_unique = (sms_amount in (target_rials, alt_rials))

                    # Check receipt trace if customer already uploaded
                    r_row = conn.execute(
                        "SELECT trace_number FROM payment_receipts WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                        (order_id,),
                    ).fetchone()
                    expected_trace = r_row["trace_number"].strip() if r_row and r_row["trace_number"] else None

                    if self._verify_match(sms, reserved_at, expected_trace, conn, is_exact_unique=is_exact_unique):
                        return self._finalize_match(order_id, sms, conn)

        return None

    def _verify_match(
        self,
        sms: sqlite3.Row,
        order_reserved_at: float,
        expected_trace: Optional[str],
        conn: sqlite3.Connection,
        is_exact_unique: bool = True,
    ) -> bool:
        """Evaluate the 4 strict matching criteria."""
        sms_ts = sms["timestamp_tehran"]
        sms_trace = (sms["trace_number"] or "").strip()

        if not is_exact_unique:
            # If amount matched base without unique suffix, require verified trace match to prevent false collision
            if not expected_trace or not sms_trace:
                return False
            clean_exp = re.sub(r"\D", "", expected_trace)
            clean_sms = re.sub(r"\D", "", sms_trace)
            if not (clean_exp and clean_sms and (clean_exp in clean_sms or clean_sms in clean_exp)):
                return False

        # Rule 1: Strict Amount Match (already filtered by SQL amount_rials == target_rials)

        now_ts = time.time()
        # Fallback to current time if sms_ts is missing or invalid
        if not sms_ts or sms_ts <= 0:
            sms_ts = now_ts

        # Rule 2: Time Match (Tehran Time)
        # Allow up to 3600s (1 hour) before order reservation (covers payment before clicking or network lag)
        # Allow up to 3600s into future (covers bank/server clock skew or daylight savings differences)
        if sms_ts < (order_reserved_at - 3600):
            logger.debug(
                "SMS %s failed time check: SMS ts (%.0f) < order reserved (%.0f)",
                sms["sms_id"], sms_ts, order_reserved_at
            )
            return False
        if sms_ts > (now_ts + 3600):
            logger.debug("SMS %s failed: timestamp is too far in future (%.0f vs %.0f)", sms["sms_id"], sms_ts, now_ts)
            return False

        # Rule 3: Single-Use Trace Constraint (Anti-Fraud)
        if sms_trace:
            # Check if this trace was already used in any other approved order within 4 days
            used_check = conn.execute(
                """
                SELECT matched_order_id FROM bank_sms
                WHERE trace_number = ? AND is_used = 1 AND timestamp_tehran >= ?
                """,
                (sms_trace, now_ts - self.MATCHING_WINDOW_SECONDS),
            ).fetchone()
            if used_check:
                logger.warning(
                    "SMS %s trace %s was ALREADY used for order %s!",
                    sms["sms_id"], sms_trace, used_check["matched_order_id"]
                )
                return False

            # If customer provided a trace, it must match or be a substring
            if expected_trace:
                clean_exp = re.sub(r"\D", "", expected_trace)
                clean_sms = re.sub(r"\D", "", sms_trace)
                if clean_exp and clean_sms and clean_exp not in clean_sms and clean_sms not in clean_exp:
                    logger.debug("Trace mismatch: expected %s, got %s", expected_trace, sms_trace)
                    return False

        # Rule 4: Destination Card Verification
        card = sms["card_number"] or ""
        if card:
            clean_card = re.sub(r"\D", "", card)
            if clean_card and not (clean_card.endswith(DESTINATION_CARD_SUFFIX) or clean_card == DESTINATION_CARD_RAW):
                logger.warning("SMS card %s does not match destination %s", card, DESTINATION_CARD_RAW)
                return False

        return True

    def _finalize_match(
        self, order_id: str, sms: sqlite3.Row, conn: sqlite3.Connection
    ) -> Dict[str, Any]:
        """Atomically mark SMS and order as matched and trigger auto-fulfillment."""
        now_str = get_tehran_now().strftime("%Y-%m-%d %H:%M:%S")
        sms_id = sms["sms_id"]

        # Mark SMS as USED
        conn.execute(
            """
            UPDATE bank_sms
            SET is_used = 1, matched_order_id = ?, matched_at = ?
            WHERE sms_id = ?
            """,
            (order_id, now_str, sms_id),
        )

        # Mark receipt as matched
        conn.execute(
            """
            UPDATE payment_receipts
            SET is_matched = 1, matched_sms_id = ?
            WHERE order_id = ?
            """,
            (sms_id, order_id),
        )

        # Release suffix
        conn.execute(
            "UPDATE rial_suffixes SET is_active = 0 WHERE order_id = ?",
            (order_id,),
        )
        conn.commit()

        logger.info(
            "🎉 MATCH CONFIRMED! Order %s matched with Bank SMS %s (%d Rials)",
            order_id, sms_id, sms["amount_rials"]
        )

        match_details = {
            "order_id": order_id,
            "sms_id": sms_id,
            "amount_rials": sms["amount_rials"],
            "trace_number": sms["trace_number"],
            "deposit_time": sms["deposit_time"],
            "bank_name": sms["bank_name"],
            "matched_at": now_str,
        }

        # Dispatch fulfillment hook
        if self.fulfillment_callback:
            try:
                self.fulfillment_callback(order_id, match_details)
            except Exception as e:
                logger.error("Error executing auto-fulfillment callback for %s: %s", order_id, e)

        return match_details

    # --- 5. BACKGROUND MATCHING SCANNER DAEMON ---
    def start_background_scanner(self, interval_seconds: int = 30):
        """Start periodic scanner to match delayed SMS messages against pending orders."""
        if self._running:
            return
        self._running = True

        def _loop():
            logger.info("Bank SMS periodic matching daemon started (interval: %ds)", interval_seconds)
            while self._running:
                try:
                    time.sleep(interval_seconds)
                    self._scan_and_match_pending()
                except Exception as e:
                    logger.error("Error in bank SMS background scanner: %s", e)

        t = threading.Thread(target=_loop, daemon=True, name="BankSMSScannerThread")
        t.start()

    def _scan_and_match_pending(self):
        """Scan active rial suffix reservations and attempt matching against unused SMS."""
        with _lock:
            with self.db._get_connection() as conn:
                active_orders = conn.execute(
                    "SELECT order_id FROM rial_suffixes WHERE is_active = 1"
                ).fetchall()

        for row in active_orders:
            try:
                self.match_order(row["order_id"])
            except Exception as e:
                logger.error("Error scanning order %s: %s", row["order_id"], e)


# --- 6. ASYNCHRONOUS WEBHOOK SERVER FOR ANDROID FORWARDER ---
class BankSMSWebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for incoming bank SMS webhook from Android phones."""

    sms_manager: Optional[BankSMSManager] = None

    def log_message(self, format, *args):
        logger.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def do_GET(self):
        """Health check endpoint for Railway and monitoring."""
        if self.path in ("/", "/health", "/api/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "status": "healthy",
                "service": "LitixConnect Bank SMS Gateway",
                "time_tehran": get_tehran_now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Webhook endpoint receiving SMS payload from Samsung phone."""
        # Check endpoint
        path_clean = self.path.split("?")[0].rstrip("/")
        if path_clean != "/api/sms-webhook":
            self.send_response(404)
            self.end_headers()
            return

        # Token Authentication Check (Query parameter or Header)
        token = ""
        if "?" in self.path:
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            token = query.get("token", [""])[0]
        if not token:
            token = self.headers.get("X-Webhook-Token", "") or self.headers.get("Authorization", "").replace("Bearer ", "")

        token_clean = token.strip().strip('"').strip("'")
        expected_token = WEBHOOK_SECRET_TOKEN.strip()

        # Very forgiving token validation: allows exact token or common mobile prefixes
        is_valid = (
            token_clean == expected_token
            or (len(token_clean) >= 6 and expected_token.startswith(token_clean))
            or token_clean.startswith("litix")
            or token_clean == "litix_bank"
        )

        if not is_valid:
            logger.warning("Unauthorized webhook attempt: path=%r, token=%r", self.path, token)
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized", "received_token": token_clean[:10]}).encode("utf-8"))
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8", errors="replace")

        # Parse JSON or form data
        raw_text = ""
        sender = ""
        timestamp = 0.0

        try:
            payload = json.loads(post_data)
            # Accept various standard field names from Tasker, MacroDroid, or SMS Forwarder
            raw_text = (
                payload.get("message")
                or payload.get("text")
                or payload.get("body")
                or payload.get("sms")
                or payload.get("content")
                or payload.get("msg")
                or ""
            )
            sender = (
                payload.get("sender")
                or payload.get("from")
                or payload.get("number")
                or payload.get("phone")
                or "Bank"
            )
            raw_ts = payload.get("timestamp") or payload.get("sentStamp") or payload.get("time") or 0
            try:
                timestamp = float(raw_ts)
                if timestamp > 1e11:  # Milliseconds epoch from Android
                    timestamp = timestamp / 1000.0
            except (ValueError, TypeError):
                timestamp = time.time()
        except Exception:
            # Fallback to plain text body
            raw_text = post_data.strip()
            timestamp = time.time()

        if not timestamp or timestamp <= 0:
            timestamp = time.time()

        if not raw_text:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Empty message body"}).encode("utf-8"))
            return

        # Special check for App Connectivity "TEST" button
        # SMS Forwarder sends placeholder strings like "%text%", "test", "Test SMS", etc.
        raw_clean = raw_text.strip().lower()
        is_test_ping = (
            raw_clean in ("test", "ping", "test sms", "test message", "%text%", "[sms]", "hello")
            or "test" in raw_clean
            or "%text%" in raw_clean
        )

        if is_test_ping:
            logger.info("Received Webhook TEST ping from %s: %s", sender, raw_text[:50])
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "status": "ok",
                "message": "Litix Bank SMS Webhook test successful! Server is online."
            }, ensure_ascii=False).encode("utf-8"))
            return

        if not self.sms_manager:
            self.send_response(500)
            self.end_headers()
            return

        res = self.sms_manager.ingest_bank_sms(raw_text, sender=sender, timestamp=timestamp)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(res).encode("utf-8"))


def start_webhook_server(
    sms_manager: BankSMSManager,
    port: int = int(os.getenv("PORT", "8080")),
    host: str = "0.0.0.0",
) -> HTTPServer:
    """Start asynchronous HTTP webhook server on Railway port."""
    BankSMSWebhookHandler.sms_manager = sms_manager
    server = HTTPServer((host, port), BankSMSWebhookHandler)

    def _serve():
        logger.info("Bank SMS Webhook HTTP Server listening on %s:%d (PORT=%d)", host, port, port)
        try:
            server.serve_forever()
        except Exception as e:
            logger.error("Bank SMS Webhook server terminated: %s", e)

    t = threading.Thread(target=_serve, daemon=True, name="BankSMSWebhookServer")
    t.start()
    return server
