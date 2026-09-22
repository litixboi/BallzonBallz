import io
import logging
import os
import time
import requests
import qrcode
from PIL import Image

logger = logging.getLogger("CryptoManager")

# Default Wallets provided by user
DEFAULT_ETH_WALLET = "0x225f3f2113B5C81A907dFeFA1551e88239cBF2EA"
DEFAULT_TRON_WALLET = "TLydCCA4FCSPczXXmDDrmhJQsK9ePXL8sQ"

ETH_WALLET = (os.getenv("ETH_WALLET_ADDRESS") or DEFAULT_ETH_WALLET).strip()
TRON_WALLET = (os.getenv("TRON_WALLET_ADDRESS") or DEFAULT_TRON_WALLET).strip()

# Fallback prices if all external rate APIs fail
DEFAULT_RATES = {
    "TRX": 0.35,
    "ETH": 2700.0,
    "USDT": 1.0,
}

_rate_cache = {
    "rates": dict(DEFAULT_RATES),
    "last_fetch": 0.0,
    "ttl": 300.0,  # 5 minutes
}


def fetch_live_crypto_rates():
    """Fetch live crypto exchange rates with Binance primary and CoinGecko fallback.
    Returns a dict with 'TRX', 'ETH', 'USDT' rates in USD."""
    now = time.monotonic()
    if now - _rate_cache["last_fetch"] < _rate_cache["ttl"] and _rate_cache["rates"]:
        return _rate_cache["rates"]

    rates = dict(_rate_cache["rates"])

    # 1. Try Binance public API
    try:
        r_trx = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT", timeout=5)
        if r_trx.status_code == 200:
            rates["TRX"] = float(r_trx.json().get("price", rates["TRX"]))

        r_eth = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=5)
        if r_eth.status_code == 200:
            rates["ETH"] = float(r_eth.json().get("price", rates["ETH"]))

        rates["USDT"] = 1.0
        _rate_cache["rates"] = rates
        _rate_cache["last_fetch"] = now
        logger.info("Updated live rates from Binance: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
        return rates
    except Exception as e:
        logger.warning("Binance ticker fetch failed: %s - trying CoinGecko fallback", e)

    # 2. Fallback to CoinGecko
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,tron,tether&vs_currencies=usd"
        r_cg = requests.get(url, timeout=6)
        if r_cg.status_code == 200:
            data = r_cg.json()
            rates["TRX"] = float(data.get("tron", {}).get("usd", rates["TRX"]))
            rates["ETH"] = float(data.get("ethereum", {}).get("usd", rates["ETH"]))
            rates["USDT"] = float(data.get("tether", {}).get("usd", 1.0))
            _rate_cache["rates"] = rates
            _rate_cache["last_fetch"] = now
            logger.info("Updated live rates from CoinGecko: TRX=$%.4f, ETH=$%.2f", rates["TRX"], rates["ETH"])
            return rates
    except Exception as e:
        logger.warning("CoinGecko rate fetch failed: %s - using cached rates", e)

    return rates


def calculate_adaptive_prices(price_usd: float):
    """Calculates adaptive payment amounts across supported cryptos based on live rates."""
    rates = fetch_live_crypto_rates()
    trx_rate = max(0.001, rates.get("TRX", DEFAULT_RATES["TRX"]))
    eth_rate = max(1.0, rates.get("ETH", DEFAULT_RATES["ETH"]))

    # Rounded calculations
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


def get_wallet_info_text():
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
