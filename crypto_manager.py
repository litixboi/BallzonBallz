import io
import logging
import os
import threading
import time
from typing import Dict, Any, Optional

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
    "TRX": 0.34,
    "ETH": 2700.0,
    "USDT": 1.0,
    "USDT_TOMAN": 252000.0,
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


def fetch_usdt_toman_rate() -> float:
    """Fetch live USDT price in Iranian Toman (IRT) with multi-tier fallback:
    1. Tetherland API (primary, lightweight JSON)
    2. Wallex API
    3. Bitpin API
    4. USDT_TOMAN_RATE environment variable
    5. DEFAULT_RATES['USDT_TOMAN'] fallback."""
    # 1. Tetherland API
    try:
        r = _sync_session.get("https://api.tetherland.com/currencies", timeout=(3.0, 5.0))
        if r.status_code == 200:
            price = float(r.json().get("data", {}).get("currencies", {}).get("USDT", {}).get("price", 0))
            if price > 1000:
                logger.debug("Fetched USDT/Toman rate from Tetherland: %s", price)
                return price
    except Exception as e:
        logger.debug("Tetherland rate fetch failed: %s - attempting Wallex", e)

    # 2. Wallex API
    try:
        r = _sync_session.get("https://api.wallex.ir/v1/markets", timeout=(3.0, 5.0))
        if r.status_code == 200:
            price = float(r.json().get("result", {}).get("symbols", {}).get("USDTTMN", {}).get("stats", {}).get("lastPrice", 0))
            if price > 1000:
                logger.debug("Fetched USDT/Toman rate from Wallex: %s", price)
                return price
    except Exception as e:
        logger.debug("Wallex rate fetch failed: %s - attempting Bitpin", e)

    # 3. Bitpin API
    try:
        r = _sync_session.get("https://api.bitpin.ir/v1/mkt/markets/", timeout=(3.0, 5.0))
        if r.status_code == 200:
            for m in r.json().get("results", []):
                if m.get("code") == "USDT_IRT":
                    price = float(m.get("price", 0))
                    if price > 1000:
                        logger.debug("Fetched USDT/Toman rate from Bitpin: %s", price)
                        return price
    except Exception as e:
        logger.debug("Bitpin rate fetch failed: %s - checking env", e)

    # 4. Environment variable override
    env_rate = os.getenv("USDT_TOMAN_RATE")
    if env_rate:
        try:
            val = float(env_rate)
            if val > 1000:
                return val
        except ValueError:
            pass

    return DEFAULT_RATES["USDT_TOMAN"]


async def fetch_usdt_toman_rate_async(client: Optional[httpx.AsyncClient] = None) -> float:
    """Async version of live USDT/Toman rate fetcher."""
    # 1. Tetherland API
    try:
        if client:
            r = await client.get("https://api.tetherland.com/currencies")
        else:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                r = await c.get("https://api.tetherland.com/currencies")
        if r.status_code == 200:
            price = float(r.json().get("data", {}).get("currencies", {}).get("USDT", {}).get("price", 0))
            if price > 1000:
                return price
    except Exception as e:
        logger.debug("Async Tetherland rate fetch failed: %s", e)

    # 2. Wallex API
    try:
        if client:
            r = await client.get("https://api.wallex.ir/v1/markets")
        else:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                r = await c.get("https://api.wallex.ir/v1/markets")
        if r.status_code == 200:
            price = float(r.json().get("result", {}).get("symbols", {}).get("USDTTMN", {}).get("stats", {}).get("lastPrice", 0))
            if price > 1000:
                return price
    except Exception as e:
        logger.debug("Async Wallex rate fetch failed: %s", e)

    # 3. Bitpin API
    try:
        if client:
            r = await client.get("https://api.bitpin.ir/v1/mkt/markets/")
        else:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                r = await c.get("https://api.bitpin.ir/v1/mkt/markets/")
        if r.status_code == 200:
            for m in r.json().get("results", []):
                if m.get("code") == "USDT_IRT":
                    price = float(m.get("price", 0))
                    if price > 1000:
                        return price
    except Exception as e:
        logger.debug("Async Bitpin rate fetch failed: %s", e)

    # 4. Env override
    env_rate = os.getenv("USDT_TOMAN_RATE")
    if env_rate:
        try:
            val = float(env_rate)
            if val > 1000:
                return val
        except ValueError:
            pass

    return DEFAULT_RATES["USDT_TOMAN"]


def fetch_live_crypto_rates() -> Dict[str, float]:
    """Fetch live crypto exchange rates with connection pooling, retries, 90s TTL cache,
    Binance primary, CoinGecko fallback, and multi-source USDT/Toman rate."""
    now = time.monotonic()
    with _cache_lock:
        if now - _rate_cache["last_fetch"] < _rate_cache["ttl"] and _rate_cache["rates"]:
            return dict(_rate_cache["rates"])
        rates = dict(_rate_cache["rates"])

    # 1. Fetch live USDT in Iranian Toman
    try:
        rates["USDT_TOMAN"] = fetch_usdt_toman_rate()
    except Exception as e:
        logger.warning("USDT/Toman rate fetch error: %s", e)
        rates["USDT_TOMAN"] = rates.get("USDT_TOMAN", DEFAULT_RATES["USDT_TOMAN"])

    # 2. Try Binance public API (timeouts: connect=5s, read=10s)
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
        logger.info(
            "Updated live rates from Binance: TRX=$%.4f, ETH=$%.2f, USDT=%s IRT",
            rates["TRX"], rates["ETH"], f"{rates['USDT_TOMAN']:,.0f}"
        )
        return rates
    except Exception as e:
        logger.warning("Binance ticker fetch failed: %s - attempting CoinGecko fallback", e)

    # 3. Fallback to CoinGecko
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
            logger.info(
                "Updated live rates from CoinGecko: TRX=$%.4f, ETH=$%.2f, USDT=%s IRT",
                rates["TRX"], rates["ETH"], f"{rates['USDT_TOMAN']:,.0f}"
            )
            return rates
    except Exception as e:
        logger.warning("CoinGecko rate fetch failed: %s - using cached rates", e)

    with _cache_lock:
        _rate_cache["rates"] = rates
        _rate_cache["last_fetch"] = now
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
        # 1. Live USDT/Toman
        try:
            rates["USDT_TOMAN"] = await fetch_usdt_toman_rate_async(client)
        except Exception as e:
            logger.warning("Async USDT/Toman rate fetch error: %s", e)
            rates["USDT_TOMAN"] = rates.get("USDT_TOMAN", DEFAULT_RATES["USDT_TOMAN"])

        # 2. Try Binance
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
            logger.info(
                "Async updated live rates from Binance: TRX=$%.4f, ETH=$%.2f, USDT=%s IRT",
                rates["TRX"], rates["ETH"], f"{rates['USDT_TOMAN']:,.0f}"
            )
            return rates
        except Exception as e:
            logger.warning("Async Binance ticker fetch failed: %s - trying CoinGecko fallback", e)

        # 3. Try CoinGecko
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
                logger.info(
                    "Async updated live rates from CoinGecko: TRX=$%.4f, ETH=$%.2f, USDT=%s IRT",
                    rates["TRX"], rates["ETH"], f"{rates['USDT_TOMAN']:,.0f}"
                )
                return rates
        except Exception as e:
            logger.warning("Async CoinGecko rate fetch failed: %s - using cached rates", e)

    with _cache_lock:
        _rate_cache["rates"] = rates
        _rate_cache["last_fetch"] = now
    return rates


def calculate_adaptive_prices(price_usd: float = 0.0, price_toman: Optional[int] = None) -> Dict[str, Any]:
    """Calculates adaptive payment amounts across supported cryptos based on live market rates.
    If price_toman is provided, converts dynamically into USDT based on the latest live
    USDT/Toman exchange rate, then into TRX and ETH.
    Also calculates blockchain network and exchange withdrawal fees for full transparency."""
    rates = fetch_live_crypto_rates()
    trx_rate = max(0.001, float(rates.get("TRX", DEFAULT_RATES["TRX"])))
    eth_rate = max(1.0, float(rates.get("ETH", DEFAULT_RATES["ETH"])))
    usdt_toman_rate = max(1000.0, float(rates.get("USDT_TOMAN", DEFAULT_RATES["USDT_TOMAN"])))

    if price_toman is not None and price_toman > 0:
        effective_toman = int(price_toman)
        usdt_amount = max(0.1, round(effective_toman / usdt_toman_rate, 2))
        effective_usd = usdt_amount
    else:
        effective_usd = round(price_usd, 2)
        usdt_amount = effective_usd
        effective_toman = int(round(effective_usd * usdt_toman_rate))

    trx_amount = round(usdt_amount / trx_rate, 2)
    eth_amount = round(usdt_amount / eth_rate, 6)

    # Calculate estimated network / exchange withdrawal fees
    # 1. TRX: typical exchange fee is 1 TRX (~$0.34)
    # 2. USDT-TRC20: typical exchange fee is 1.0 USDT
    # 3. ETH: typical exchange fee / gas is ~0.001 ETH
    # 4. USDT-ERC20: typical exchange fee is 2.0 USDT
    trx_fee = 1.0
    usdt_fee = 1.0
    eth_fee = 0.001
    usdt_erc20_fee = 2.0

    trx_recommended_gross = round(trx_amount + trx_fee, 2)
    usdt_recommended_gross = round(usdt_amount + usdt_fee, 2)
    eth_recommended_gross = round(eth_amount + eth_fee, 6)
    usdt_erc20_recommended_gross = round(usdt_amount + usdt_erc20_fee, 2)

    return {
        "price_usd": effective_usd,
        "price_toman": effective_toman,
        "usdt": usdt_amount,
        "trx": trx_amount,
        "eth": eth_amount,
        "rates": rates,
        "usdt_toman_rate": int(usdt_toman_rate),
        "trx_usd_rate": trx_rate,
        "eth_usd_rate": eth_rate,
        "fees": {
            "trx_fee": trx_fee,
            "trx_fee_usd": round(trx_fee * trx_rate, 2),
            "trx_gross": trx_recommended_gross,
            "usdt_fee": usdt_fee,
            "usdt_gross": usdt_recommended_gross,
            "eth_fee": eth_fee,
            "eth_gross": eth_recommended_gross,
            "usdt_erc20_fee": usdt_erc20_fee,
            "usdt_erc20_gross": usdt_erc20_recommended_gross,
        },
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
