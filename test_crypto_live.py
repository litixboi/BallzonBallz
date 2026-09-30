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
            # Test 400,000 Tomans plan
            pricing = crypto_manager.calculate_adaptive_prices(price_usd=4.0, price_toman=400000)
            self.assertEqual(pricing["price_toman"], 400000)
            # 400,000 / 252,750 = ~1.58 USDT (NOT $4.0 USD!)
            self.assertAlmostEqual(pricing["usdt"], 1.58, places=2)
            # 1.58 / 0.3379 = ~4.68 TRX
            self.assertAlmostEqual(pricing["trx"], 4.68, places=2)
            # 4 payment option gross amounts with fees included:
            # 1. TRX: 4.68 + 1.0 = 5.68 TRX
            self.assertEqual(pricing["fees"]["trx_fee"], 1.0)
            self.assertAlmostEqual(pricing["fees"]["trx_gross"], 5.68, places=2)
            # 2. USDT-TRC20: 1.58 + 1.0 = 2.58 USDT
            self.assertEqual(pricing["fees"]["usdt_fee"], 1.0)
            self.assertAlmostEqual(pricing["fees"]["usdt_gross"], 2.58, places=2)
            # 3. ETH: (1.58 / 2700) + 0.001 = ~0.001585 ETH
            self.assertEqual(pricing["fees"]["eth_fee"], 0.001)
            self.assertGreater(pricing["fees"]["eth_gross"], 0.001)
            # 4. USDT-ERC20: 1.58 + 2.0 = 3.58 USDT
            self.assertEqual(pricing["fees"]["usdt_erc20_fee"], 2.0)
            self.assertAlmostEqual(pricing["fees"]["usdt_erc20_gross"], 3.58, places=2)

    def test_multi_tier_fallback(self):
        # When all external network calls fail, should fallback to env or default
        with patch.object(crypto_manager._sync_session, "get", side_effect=Exception("Network down")):
            rate = crypto_manager.fetch_usdt_toman_rate()
            self.assertGreaterEqual(rate, 250000.0)


if __name__ == "__main__":
    unittest.main()
