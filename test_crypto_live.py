import unittest
from unittest.mock import patch
import crypto_manager


class TestLiveCryptoConversionAndFees(unittest.TestCase):
    def test_live_rates_structure(self):
        rates = crypto_manager.fetch_live_crypto_rates()
        self.assertIn("TRX", rates)
        self.assertIn("ETH", rates)
        self.assertIn("USDT", rates)
        self.assertIn("USDT_TOMAN", rates)
        self.assertGreater(rates["TRX"], 0.0)
        self.assertGreater(rates["ETH"], 0.0)
        self.assertGreater(rates["USDT_TOMAN"], 1000.0)

    def test_legacy_pricing_backwards_compatibility(self):
        pricing = crypto_manager.calculate_adaptive_prices(10.0)
        self.assertEqual(pricing["usdt"], 10.0)
        self.assertGreater(pricing["trx"], 0.0)
        self.assertGreater(pricing["eth"], 0.0)
        self.assertIn("fees", pricing)
        self.assertEqual(pricing["fees"]["trx_fee"], 1.0)
        self.assertEqual(pricing["fees"]["usdt_fee"], 1.0)
        self.assertEqual(pricing["fees"]["usdt_gross"], 11.0)

    def test_dynamic_toman_conversion(self):
        mock_rates = {
            "TRX": 0.3379,
            "ETH": 2700.0,
            "USDT": 1.0,
            "USDT_TOMAN": 252750.0,
        }
        with patch.object(crypto_manager, "fetch_live_crypto_rates", return_value=mock_rates):
            # 260,000 Tomans
            pricing = crypto_manager.calculate_adaptive_prices(price_usd=2.6, price_toman=260000)
            self.assertEqual(pricing["price_toman"], 260000)
            # 260,000 / 252,750 = ~1.03 USDT
            self.assertAlmostEqual(pricing["usdt"], 1.03, places=2)
            # 1.03 / 0.3379 = ~3.05 TRX
            self.assertAlmostEqual(pricing["trx"], 3.05, places=2)
            # Fees
            self.assertEqual(pricing["fees"]["trx_fee"], 1.0)
            self.assertAlmostEqual(pricing["fees"]["trx_gross"], 4.05, places=2)
            self.assertEqual(pricing["fees"]["usdt_fee"], 1.0)
            self.assertAlmostEqual(pricing["fees"]["usdt_gross"], 2.03, places=2)

    def test_multi_tier_fallback(self):
        # When all external network calls fail, should fallback to env or default
        with patch.object(crypto_manager._sync_session, "get", side_effect=Exception("Network down")):
            rate = crypto_manager.fetch_usdt_toman_rate()
            self.assertGreaterEqual(rate, 250000.0)


if __name__ == "__main__":
    unittest.main()
