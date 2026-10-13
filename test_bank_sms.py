import json
import os
import secrets
import time
import unittest
from pathlib import Path

from bank_sms_manager import (
    IranianBankSMSParser,
    BankSMSManager,
    jalali_to_gregorian,
    gregorian_to_jalali,
    parse_jalali_datetime_to_timestamp,
    start_webhook_server,
    WEBHOOK_SECRET_TOKEN,
    DESTINATION_CARD_RAW,
)
import requests


class TestBankSMSManager(unittest.TestCase):
    def setUp(self):
        self.test_db = Path(__file__).parent / f"test_bank_sms_temp_{secrets.token_hex(4)}.db"
        self.manager = BankSMSManager(self.test_db)

    def tearDown(self):
        # Best effort cleanup
        try:
            for p in self.test_db.parent.glob(f"{self.test_db.stem}*"):
                p.unlink(missing_ok=True)
        except Exception:
            pass

    def test_jalali_conversion(self):
        gy, gm, gd = jalali_to_gregorian(1403, 7, 23)
        self.assertEqual((gy, gm, gd), (2024, 10, 14))

        jy, jm, jd = gregorian_to_jalali(2024, 10, 14)
        self.assertEqual((jy, jm, jd), (1403, 7, 23))

        ts = parse_jalali_datetime_to_timestamp("1403/07/23", "14:30:00")
        self.assertIsNotNone(ts)

        # Invalid date rejection
        self.assertIsNone(parse_jalali_datetime_to_timestamp("1403/18/45", "14:30:00"))

    def test_melli_sms_parsing(self):
        # Sample realistic Bank Melli deposit SMS
        melli_sms = (
            "بانک ملی ایران\n"
            "انتقال از 6037991234567890 به 6037701506640355\n"
            "مبلغ: 4,000,037+ ریال\n"
            "1403/07/23-15:45:10\n"
            "مانده: 18,500,000 ریال\n"
            "پیگیری: 87654321"
        )
        parsed = IranianBankSMSParser.parse(melli_sms)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["amount_rials"], 4000037)
        self.assertEqual(parsed["trace_number"], "87654321")
        self.assertEqual(parsed["bank_name"], "بانک ملی")
        self.assertEqual(parsed["card_number"], DESTINATION_CARD_RAW)

    def test_unique_rial_suffix_allocation(self):
        # Base amount: 400,000 Tomans
        alloc1 = self.manager.allocate_unique_rial_amount("ORD-AAA111", 400000)
        self.assertEqual(alloc1["base_amount_toman"], 400000)
        self.assertEqual(alloc1["base_amount_rials"], 4000000)
        self.assertTrue(1 <= alloc1["suffix"] <= 99)
        self.assertEqual(alloc1["total_amount_rials"], 4000000 + alloc1["suffix"])

        # Second order with same base amount must receive DIFFERENT suffix
        alloc2 = self.manager.allocate_unique_rial_amount("ORD-BBB222", 400000)
        self.assertNotEqual(alloc1["suffix"], alloc2["suffix"])
        self.assertNotEqual(alloc1["total_amount_rials"], alloc2["total_amount_rials"])

        # Idempotence: requesting again for same order_id returns identical suffix
        alloc1_again = self.manager.allocate_unique_rial_amount("ORD-AAA111", 400000)
        self.assertEqual(alloc1["suffix"], alloc1_again["suffix"])

    def test_matching_engine_auto_fulfillment(self):
        order_id = "ORD-TEST999"
        alloc = self.manager.allocate_unique_rial_amount(order_id, 400000)
        exact_rials = alloc["total_amount_rials"]

        fulfilled_orders = []

        def dummy_fulfill(oid, details):
            fulfilled_orders.append((oid, details))

        self.manager.set_fulfillment_callback(dummy_fulfill)

        # Incoming SMS matching exact rials and destination
        sms_text = (
            f"بانک ملی ایران\n"
            f"واریز به کارت 6037701506640355\n"
            f"مبلغ: {exact_rials:,} ریال\n"
            f"پیگیری: 99887766\n"
            f"1403/07/23 16:00"
        )
        ingest_res = self.manager.ingest_bank_sms(sms_text, sender="BankMelli", timestamp=time.time())
        self.assertTrue(ingest_res["success"])
        self.assertEqual(ingest_res["matched_order_id"], order_id)

        # Verify callback was fired
        self.assertEqual(len(fulfilled_orders), 1)
        self.assertEqual(fulfilled_orders[0][0], order_id)
        self.assertEqual(fulfilled_orders[0][1]["trace_number"], "99887766")

        # Anti-fraud: same trace number cannot be matched again!
        alloc_new = self.manager.allocate_unique_rial_amount("ORD-FRAUD01", 400000)
        ingest_fraud = self.manager.ingest_bank_sms(
            f"واریز {alloc_new['total_amount_rials']:,} ریال پیگیری: 99887766",
            sender="BankMelli",
            timestamp=time.time(),
        )
        self.assertIsNone(ingest_fraud.get("matched_order_id"))

    def test_webhook_server(self):
        fulfilled = []
        self.manager.set_fulfillment_callback(lambda oid, d: fulfilled.append(oid))

        port = 18991
        server = start_webhook_server(self.manager, port=port, host="127.0.0.1")
        time.sleep(0.5)

        # 1. Health check
        r_health = requests.get(f"http://127.0.0.1:{port}/")
        self.assertEqual(r_health.status_code, 200)
        self.assertEqual(r_health.json()["status"], "healthy")

        # 2. Unauthorized webhook attempt
        r_unauth = requests.post(f"http://127.0.0.1:{port}/api/sms-webhook", json={"message": "test"})
        self.assertEqual(r_unauth.status_code, 401)

        # 3. Authorized webhook attempt with matching order
        order_id = "ORD-WEBHOOK01"
        alloc = self.manager.allocate_unique_rial_amount(order_id, 300000)

        sms_body = (
            f"واریز به کارت 6037701506640355\n"
            f"مبلغ: {alloc['total_amount_rials']:,} ریال\n"
            f"پیگیری: 55443322\n"
            f"1403/07/23 17:00"
        )
        r_auth = requests.post(
            f"http://127.0.0.1:{port}/api/sms-webhook?token={WEBHOOK_SECRET_TOKEN}",
            json={"sender": "9820004000", "message": sms_body, "timestamp": time.time()},
        )
        self.assertEqual(r_auth.status_code, 200)
        self.assertEqual(r_auth.json()["matched_order_id"], order_id)
        self.assertIn(order_id, fulfilled)

        server.shutdown()


if __name__ == "__main__":
    unittest.main()
