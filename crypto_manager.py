import io
import logging
import os
import threading
import time
from typing import Dict, Any

import httpx
import qrcode
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("CryptoManager")

# Default Wallets provided by user
DEFAULT_ETH_WALLET = "0x225f3f2113B5C81A907dFeFA1551e88239cBF2EA"
DEFAULT_TRON_WALLET = "TLydCCA4FCSPczXXmDDrmhJQsK9ePXL8sQ"

ETH_WALLET = (os.getenv("ETH_WALLET_ADDRESS") or DEFAULT_ETH_WALLET).strip()
TRON_WALLET = (os.getenv("TRON_WALLET_ADDRESS") or DEFAULT_TRON_WALLET).strip()

# Fallback prices if external rate APIs fail completely
DEFAULT_RATES: Dict[str, float] = {
    "TRX": 0.35,
    "ETH": 2700.0,
    "USDT": 1.0,
}

_cache_lock = threading.Lock()
_rate_cache: Dict[str, Any] = {
    "rates": dict(DEFAULT_RATES),
    "last_fetch": 0.0,
    "ttl": 90.0,  # 90-second in-memory TTL cache (satisfies 60-120s specification)
}

# Resilient connection pool for synchronous callers
_sync_session = requests.Session()
_sync_session.trust_env = False
_retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry_strategy, pool_connections=5, pool_maxsize=10)
_sync_session.mount("https://", _adapter)
_sync_session.mount("http://", _adapter)


def get_cached_rates() -> Dict[str, float]:
    """Return a thread-safe copy of the cached rates."""
    with _cache_lock:
        return dict(_rate_cache["rates"])


def fetch_live_crypto_rates() -> Dict[str, float]:
    """Fetch live crypto exchange rates with connection pooling, retries, 90s TTL cache,
    Binance primary and CoinGecko fallback."""
    now = time.monotonic()
    with _cache_lock:
        if now - _rate_cache["last_fetch"] < _rate_cache["ttl"] and _rate_cache["rates"]:
            return dict(_rate_cache["rates"])
        rates = dict(_rate_cache["rates"])

    # 1. Try Binance public API (timeouts: connect=5s, read=10s)
    try:
        r_trx = _sync_session.get("https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT", timeout=(5.0, 10.0))
        if r_trx.status_code == 200:
            rates["TRX"] = float(r_trx.json().get("price", rates["TRX"]))

        r_eth = _sync_session.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=(5.0, 10.0))
        if r_eth.status_code == 200:
            rates["ETH"] = float(r_eth.json().get("price", rates["ETH"]))

        rates["USDT"] = 1.0
        with _cache_lock:
            _rate_cache["rates"] = rates
            _rate_cache["last_fetch"] = now
        logger.info("Updated live rates from Binance: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
        return rates
    except Exception as e:
        logger.warning("Binance ticker fetch failed: %s - attempting CoinGecko fallback", e)

    # 2. Fallback to CoinGecko
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,tron,tether&vs_currencies=usd"
        r_cg = _sync_session.get(url, timeout=(5.0, 10.0))
        if r_cg.status_code == 200:
            data = r_cg.json()
            rates["TRX"] = float(data.get("tron", {}).get("usd", rates["TRX"]))
            rates["ETH"] = float(data.get("ethereum", {}).get("usd", rates["ETH"]))
            rates["USDT"] = float(data.get("tether", {}).get("usd", 1.0))
            with _cache_lock:
                _rate_cache["rates"] = rates
                _rate_cache["last_fetch"] = now
            logger.info("Updated live rates from CoinGecko: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
            return rates
    except Exception as e:
        logger.warning("CoinGecko rate fetch failed: %s - using cached rates", e)

    return rates


async def fetch_live_crypto_rates_async() -> Dict[str, float]:
    """Asynchronous version of live crypto rate fetcher using httpx.AsyncClient with timeouts and retries."""
    now = time.monotonic()
    with _cache_lock:
        if now - _rate_cache["last_fetch"] < _rate_cache["ttl"] and _rate_cache["rates"]:
            return dict(_rate_cache["rates"])
        rates = dict(_rate_cache["rates"])

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        # 1. Try Binance
        try:
            r_trx = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT")
            if r_trx.status_code == 200:
                rates["TRX"] = float(r_trx.json().get("price", rates["TRX"]))

            r_eth = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT")
            if r_eth.status_code == 200:
                rates["ETH"] = float(r_eth.json().get("price", rates["ETH"]))

            rates["USDT"] = 1.0
            with _cache_lock:
                _rate_cache["rates"] = rates
                _rate_cache["last_fetch"] = now
            logger.info("Async updated live rates from Binance: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
            return rates
        except Exception as e:
            logger.warning("Async Binance ticker fetch failed: %s - trying CoinGecko fallback", e)

        # 2. Try CoinGecko
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,tron,tether&vs_currencies=usd"
            r_cg = await client.get(url)
            if r_cg.status_code == 200:
                data = r_cg.json()
                rates["TRX"] = float(data.get("tron", {}).get("usd", rates["TRX"]))
                rates["ETH"] = float(data.get("ethereum", {}).get("usd", rates["ETH"]))
                rates["USDT"] = float(data.get("tether", {}).get("usd", 1.0))
                with _cache_lock:
                    _rate_cache["rates"] = rates
                    _rate_cache["last_fetch"] = now
                logger.info("Async updated live rates from CoinGecko: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
                return rates
        except Exception as e:
            logger.warning("Async CoinGecko rate fetch failed: %s - using cached rates", e)

    return rates


def calculate_adaptive_prices(price_usd: float) -> Dict[str, Any]:
    """Calculates adaptive payment amounts across supported cryptos based on live rates."""
    rates = fetch_live_crypto_rates()
    trx_rate = max(0.001, rates.get("TRX", DEFAULT_RATES["TRX"]))
    eth_rate = max(1.0, rates.get("ETH", DEFAULT_RATES["ETH"]))

    trx_amount = round(price_usd / trx_rate, 2)
    eth_amount = round(price_usd / eth_rate, 6)
    usdt_amount = round(price_usd, 2)

    return {
        "price_usd": price_usd,
        "usdt": usdt_amount,
        "trx": trx_amount,
        "eth": eth_amount,
        "rates": rates,
    }


def generate_qr_bytes(text: str) -> bytes:
    """Generate a high-contrast QR code image as PNG bytes."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def get_wallet_info_text() -> str:
    """Returns formatted Persian text displaying both wallet addresses."""
    return (
        "💳 <b>آدرس‌های رسمی کیف پول جهت خرید و حمایت مالی (Donation):</b>\n\n"
        "🔺 <b>شبکه ترون (Tron Network - TRX / USDT-TRC20):</b>\n"
        f"<code>{TRON_WALLET}</code>\n"
        "<i>(برای کپی کردن آدرس، روی آن ضربه بزنید)</i>\n\n"
        "🔹 <b>شبکه اتریوم (Ethereum Network - ETH / USDT-ERC20):</b>\n"
        f"<code>{ETH_WALLET}</code>\n"
        "<i>(برای کپی کردن آدرس، روی آن ضربه بزنید)</i>\n\n"
        "⚠️ <b>نکته مهم:</b> لطفاً در انتخاب شبکه انتقال دقت فرمایید. واریز در شبکه اشتباه ممکن است باعث از دست رفتن وجه شود."
    )
