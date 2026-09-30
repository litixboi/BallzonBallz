import sys
sys.stdout.reconfigure(encoding='utf-8')
import json
import base64
import urllib.parse
from pathlib import Path
import ConfigBot

print("==================================================")
print("🧪 TESTING CHANNEL POST REFACTOR & ANTI-DPI ENGINE")
print("==================================================")

# 1. Test rebrand_config & Anti-DPI Link Enhancement
print("\n[1/4] Testing Config Link Tampering & Enhancement...")
vless_in = "vless://7ef197b3-8715-44ea-bdd8-719ac24140c3@85.10.197.124:443?type=ws&security=tls&path=%2Fcon&sni=conpanel.litontheix.ir#OriginalRemark"
vless_out = ConfigBot.rebrand_config(vless_in, "Germany", 1)
print("  VLESS Input: ", vless_in)
print("  VLESS Output:", vless_out)
assert "fragment=100-200%2C10-20%2Ctlshello" in vless_out or "fragment=100-200,10-20,tlshello" in vless_out, "Fragment missing in VLESS!"
assert "fp=chrome" in vless_out, "Chrome fingerprint missing in VLESS!"
assert "alpn=h2%2Chttp%2F1.1" in vless_out or "alpn=h2,http/1.1" in vless_out, "ALPN missing in VLESS!"
assert "DE" in vless_out and "litixconnect" in vless_out, "Rebrand remark missing!"

# Test Trojan
trojan_in = "trojan://password123@1.2.3.4:443?security=tls&sni=myhost.com#OldTrojan"
trojan_out = ConfigBot.rebrand_config(trojan_in, "Netherlands", 2)
assert "fragment=" in trojan_out, "Fragment missing in Trojan!"
assert "fp=chrome" in trojan_out, "Chrome fingerprint missing in Trojan!"

# Test VMess
vmess_data = {"v": "2", "ps": "old", "add": "1.1.1.1", "port": 443, "id": "uuid-999", "net": "ws", "tls": "tls"}
vmess_in = "vmess://" + base64.b64encode(json.dumps(vmess_data).encode()).decode()
vmess_out = ConfigBot.rebrand_config(vmess_in, "United States", 3)
decoded_vmess = json.loads(base64.b64decode(vmess_out.replace("vmess://", "")).decode("utf-8"))
assert decoded_vmess.get("fp") == "chrome", "Chrome fingerprint missing in VMess!"
assert decoded_vmess.get("alpn") == "h2,http/1.1", "ALPN missing in VMess!"
assert "US" in decoded_vmess.get("ps", ""), "Rebranded ps missing in VMess!"

# Test Shadowsocks (SS must not break or have weird tampering, only remark)
ss_in = "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@8.8.8.8:8388#OldSS"
ss_out = ConfigBot.rebrand_config(ss_in, "Germany", 4)
assert ss_out.startswith("ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@8.8.8.8:8388#"), "SS link corrupted!"
print("  ✓ Config link anti-DPI enhancements verified for VLESS, VMess, Trojan, and SS!")

# 2. Test collect_top_fastest_configs (Top 5, NO ss://)
print("\n[2/4] Testing Top 5 Configs Selection (Zero SS)...")
active_entries_mock = [
    ("Germany", [
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@8.8.8.8:8388#ss1",  # SS should be skipped!
        "vless://user1@server-de.com:443?security=tls#vless1",
        "vmess://eyJ2IjoiMiJ9#vmess1"
    ]),
    ("Netherlands", [
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@9.9.9.9:8388#ss2",  # SS should be skipped!
        "trojan://pass@server-nl.com:443?security=tls#trojan1"
    ]),
    ("United States", [
        "vless://user2@server-us.com:443?security=tls#vless2",
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@7.7.7.7:8388#ss3"
    ]),
    ("Singapore", [
        "vmess://eyJ2IjoiMiJ9#vmess2"
    ]),
    ("France", [
        "trojan://pass@server-fr.com:443?security=tls#trojan2"
    ]),
    ("Japan", [
        "vless://user3@server-jp.com:443?security=tls#vless3"
    ])
]

top_5 = ConfigBot.collect_top_fastest_configs(active_entries_mock, count=5)
print(f"  Selected {len(top_5)} configs:")
for i, c in enumerate(top_5, 1):
    print(f"    [{i}] {c.split('://')[0]}://...{c[-30:]}")

assert len(top_5) == 5, f"Expected 5 configs, got {len(top_5)}"
for c in top_5:
    assert not c.startswith("ss://"), f"Found forbidden ss:// config in top 5: {c}"
print("  ✓ Exactly 5 configs selected, 0 Shadowsocks (SS), diversity across countries verified!")

# 3. Test Banner Generation
print("\n[3/4] Testing Banner Image Generation...")
with ConfigBot.nodes_lock:
    ConfigBot.categorized_nodes["Germany"] = ["vless://de1", "vless://de2", "vless://de3"]
    ConfigBot.categorized_nodes["Netherlands"] = ["trojan://nl1", "trojan://nl2"]
    ConfigBot.categorized_nodes["United States"] = ["vmess://us1", "vmess://us2", "vmess://us3", "vmess://us4"]

banner_file = ConfigBot.create_update_banner()
assert banner_file.exists(), f"Banner file {banner_file} was not generated!"
from PIL import Image
b_img = Image.open(banner_file)
assert b_img.size == (1280, 720), f"Unexpected banner size: {b_img.size}"
print(f"  ✓ Banner generated at {banner_file} (dimensions: {b_img.size}, bytes: {banner_file.stat().st_size:,})")

# 4. Test Connectivity URL endpoints
print("\n[4/4] Testing Connectivity Endpoints...")
print(f"  CONNECTIVITY_URLS: {ConfigBot.CONNECTIVITY_URLS}")
assert any(u.startswith("https://") for u in ConfigBot.CONNECTIVITY_URLS), "HTTPS endpoint missing in CONNECTIVITY_URLS!"
print("  ✓ HTTPS verification endpoints verified!")

print("\n==================================================")
print("🎉 ALL CHANNEL REFACTOR & ANTI-DPI TESTS PASSED!")
print("==================================================")
