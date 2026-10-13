import json
import os
import secrets
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bank_sms_manager import BankSMSManager, DESTINATION_CARD_RAW
from order_manager import OrderManager
import ConfigBot


class TestCardPaymentE2E(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(__file__).parent / f"test_e2e_tmp_{secrets.token_hex(4)}"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp_dir / "orders.db"
        self.order_mgr = OrderManager(self.db_path)
        self.bank_mgr = BankSMSManager(self.db_path)

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_client_username_generation(self):
        # 1. User with username
        order1 = {"user_id": 12345, "username": "@AwlinAvakhtam", "first_name": "Nima"}
        email1 = ConfigBot.generate_client_username(order1, index=0, total_qty=1)
        self.assertEqual(email1, "AwlinAvakhtam")

        # Multi-quantity
        email1_q2 = ConfigBot.generate_client_username(order1, index=1, total_qty=3)
        self.assertEqual(email1_q2, "AwlinAvakhtam_2")

        # 2. User without username, but with first name
        order2 = {"user_id": 67890, "username": "", "first_name": "Soroush"}
        email2 = ConfigBot.generate_client_username(order2, index=0, total_qty=1)
        self.assertEqual(email2, "Soroush")

        # 3. User with special characters
        order3 = {"user_id": 99999, "username": "ali_vpn_2026!", "first_name": "Ali"}
        email3 = ConfigBot.generate_client_username(order3, index=0, total_qty=1)
        self.assertEqual(email3, "ali_vpn_2026")

    def test_full_e2e_card_payment_and_bank_sms_matching(self):
        # 1. User orders a 30GB plan for 400,000 Tomans
        plan = {
            "id": "vip_30gb_1m_1u",
            "name_fa": "پلن ۱ ماهه ۳۰ گیگ",
            "volume_gb": 30,
            "duration_days": 30,
            "devices": 1,
            "price_usd": 4.0,
            "price_toman": 400000,
        }

        order_id = self.order_mgr.create_order(
            user_id=2064926384,
            username="awlinavakhtam",
            first_name="Nima",
            plan=plan,
            crypto_network="کارت به کارت (بانک ملی)",
            crypto_currency="TOMAN/RIAL",
            crypto_amount=400000,
            quantity=1,
            final_price_toman=400000,
            final_price_usd=4.0,
            payment_method="CARD_TO_CARD",
        )

        # 2. Suffix allocation
        alloc = self.bank_mgr.allocate_unique_rial_amount(order_id, 400000)
        target_rials = alloc["total_amount_rials"]
        self.assertEqual(target_rials, 4000000 + alloc["suffix"])

        # 3. Mock botpanel_mgr subscription creation
        mock_sub = {
            "success": True,
            "email": "awlinavakhtam",
            "client_uuid": "test-uuid-1234",
            "subId": "sub12345678",
            "sub_url": "https://conbot.85-10-197-124.nip.io/sub/sub12345678",
            "bridge_url": "https://freshbridge.darkube.ir/sub/sub12345678",
            "bridge2_url": "https://fckassthis.darkube.ir/sub/sub12345678",
            "total_gb": 30,
            "expiry_days": 30,
            "limit_hwid": 1,
        }

        fulfilled_records = []

        def mock_fulfill(oid, details):
            fulfilled_records.append((oid, details))
            self.order_mgr.approve_order(oid, mock_sub["bridge_url"], delivered_subs=[mock_sub])
            self.bank_mgr.release_suffix(oid)

        self.bank_mgr.set_fulfillment_callback(mock_fulfill)

        # 4. Bank SMS arrives via webhook from Samsung phone
        sms_text = (
            f"بانک ملی ایران\n"
            f"انتقال از 603799******9876 به {DESTINATION_CARD_RAW}\n"
            f"مبلغ: {target_rials:,}+ ریال\n"
            f"1403/07/23-18:30:00\n"
            f"مانده: 25,000,000 ریال\n"
            f"پیگیری: 76543210"
        )

        res = self.bank_mgr.ingest_bank_sms(sms_text, sender="BankMelli", timestamp=time.time())
        self.assertTrue(res["success"])
        self.assertEqual(res["matched_order_id"], order_id)

        # 5. Order is APPROVED
        updated_order = self.order_mgr.get_order(order_id)
        self.assertEqual(updated_order["status"], "APPROVED")
        self.assertEqual(len(fulfilled_records), 1)
        self.assertEqual(fulfilled_records[0][0], order_id)
        self.assertEqual(fulfilled_records[0][1]["trace_number"], "76543210")

        # 6. Suffix reservation is released
        with self.bank_mgr.db._get_connection() as conn:
            row = conn.execute("SELECT is_active FROM rial_suffixes WHERE order_id = ?", (order_id,)).fetchone()
            self.assertEqual(row["is_active"], 0)


if __name__ == "__main__":
    unittest.main()
