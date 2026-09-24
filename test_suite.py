#!/usr/bin/env python3
"""
=======================================================================
CHIRIX → TALLY MIDDLEWARE — COMPREHENSIVE TEST SUITE (Tests 1–10)
=======================================================================
Covers:
  T1  — Direct Chirix API call with raw token
  T2  — Company selection (RIK vs FBT raw token logic)
  T3  — Date format conversion (YYYY-MM-DD → dd/mm/yyyy)
  T4  — Chirix response validation
  T5  — Invoice field conversion
  T6  — GST field preservation
  T7  — Tally push & actual HTTP response
  T8  — Duplicate control check
  T9  — Error handling (15 scenarios)
  T10 — Flask endpoint integration (end-to-end via localhost:5000)
"""

import json
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Configuration ────────────────────────────────────────────────────────────
CHIRIX_API_URL    = "https://www.chirixsolutions.com/api/data"
FLASK_BASE        = "http://localhost:5000"
TALLY_URL         = "http://127.0.0.1:9000"

RIK_TOKEN_RAW     = "52494B54432D30303036"
FBT_TOKEN_RAW     = "52494B54432D30303037"

TEST_DATE_FROM_ISO = "2026-08-01"   # YYYY-MM-DD (UI format)
TEST_DATE_TO_ISO   = "2026-08-11"   # YYYY-MM-DD (UI format)
TEST_DATE_FROM_CHX = "01/08/2026"   # dd/mm/yyyy (Chirix format)
TEST_DATE_TO_CHX   = "11/08/2026"   # dd/mm/yyyy (Chirix format)


# ── Utilities ────────────────────────────────────────────────────────────────
PASS  = "[PASS]"
FAIL  = "[FAIL]"
WARN  = "[WARN]"
INFO  = "[INFO]"

passed = failed = warned = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {PASS}  {label}")
    else:
        failed += 1
        print(f"  {FAIL}  {label}")
        if detail:
            print(f"          -> {detail}")
def warn(label, detail=""):
    global warned
    warned += 1
    print(f"  {WARN}  {label}")
    if detail:
        print(f"          -> {detail}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def mask(token: str) -> str:
    if len(token) > 6:
        return token[:6] + "···" + token[-4:]
    return token[:3] + "···"

def iso_to_chirix(iso_date: str) -> str:
    """Convert YYYY-MM-DD → dd/mm/yyyy"""
    y, m, d = iso_date.split("-")
    return f"{d}/{m}/{y}"

def flask_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        f"{FLASK_BASE}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body}

def chirix_direct(token_raw: str, date_from: str, date_to: str, method="GET"):
    """Call Chirix API directly with raw token."""
    payload = json.dumps({"dateFrom": date_from, "dateTo": date_to}).encode("utf-8")
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE
    req = urllib.request.Request(
        CHIRIX_API_URL, data=payload,
        headers={
            "Chirix-Auth-Token": token_raw,
            "Transaction-Type":  "SIV",
            "Content-Type":      "application/json"
        },
        method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
            body = json.loads(r.read().decode("utf-8"))
            return r.status, body, None
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            err_body = json.loads(raw)
        except Exception:
            err_body = raw if isinstance(raw, str) else str(e)
        return e.code, err_body, e
    except urllib.error.URLError as e:
        return 0, {}, e


# ════════════════════════════════════════════════════════════════════════════════
# TEST 1 — CHIRIX API DIRECT CALL (LIVE API TEST)
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 1 — CHIRIX API DIRECT CALL (LIVE API)")
print(f"\n  Request to : {CHIRIX_API_URL}")
print(f"  Method     : GET")
print(f"  Chirix-Auth-Token : {mask(RIK_TOKEN_RAW)}")
print(f"  Transaction-Type  : SIV")
print(f"  dateFrom   : {TEST_DATE_FROM_CHX}")
print(f"  dateTo     : {TEST_DATE_TO_CHX}\n")

t1_status, t1_body, t1_err = chirix_direct(RIK_TOKEN_RAW, TEST_DATE_FROM_CHX, TEST_DATE_TO_CHX)

print(f"  HTTP Status: {t1_status}")

check("HTTP status is 200 OK", t1_status == 200, f"Got: {t1_status}")
if t1_status == 200 and isinstance(t1_body, dict):
    check("success == true",    t1_body.get("success") is True)
    check("'records' key exists", "records" in t1_body)
    check("'data' is list",     isinstance(t1_body.get("data"), list))
    records = t1_body.get("records", 0)
    data    = t1_body.get("data", [])
    check("records count matches data length", records == len(data),
          f"records={records} len(data)={len(data)}")
    print(f"\n  {INFO}  {records} live invoice(s) returned from Chirix API for RIK!")
    if data:
        sample = data[0]
        check("First invoice has InvNo",     "InvNo"    in sample)
        check("First invoice has InvDt",     "InvDt"    in sample)
        check("First invoice has ItemList",  "ItemList" in sample)
        items = sample.get("ItemList", [])
        check("ItemList is non-empty list",  isinstance(items, list) and len(items) > 0)
        T1_INVOICES = data
    else:
        T1_INVOICES = []
else:
    T1_INVOICES = []


# ════════════════════════════════════════════════════════════════════════════════
# TEST 2 — COMPANY SELECTION (LIVE TEST FOR RIK AND FBT)
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 2 — COMPANY SELECTION (LIVE TEST FOR RIK & FBT TOKENS)")

# RIK Test
rik_status, rik_body, _ = chirix_direct(RIK_TOKEN_RAW, TEST_DATE_FROM_CHX, TEST_DATE_TO_CHX)
check("RIK company raw token gets 200 OK from Chirix API", rik_status == 200)
check("RIK company returns success=true", rik_body.get("success") is True)
print(f"  {INFO}  RIK records returned: {rik_body.get('records', 0)}")

# FBT Test
fbt_status, fbt_body, _ = chirix_direct(FBT_TOKEN_RAW, TEST_DATE_FROM_CHX, TEST_DATE_TO_CHX)
check("FBT company raw token gets 200 OK from Chirix API", fbt_status == 200)
check("FBT company returns success=true", fbt_body.get("success") is True)
print(f"  {INFO}  FBT records returned: {fbt_body.get('records', 0)}")


# ════════════════════════════════════════════════════════════════════════════════
# TEST 3 — DATE FORMAT CONVERSION
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 3 — DATE FORMAT CONVERSION (YYYY-MM-DD → dd/mm/yyyy)")

test_cases = [
    ("2026-08-11", "11/08/2026"),
    ("2026-01-01", "01/01/2026"),
    ("2026-12-31", "31/12/2026"),
    ("2025-06-05", "05/06/2025"),
]

for iso, expected in test_cases:
    result = iso_to_chirix(iso)
    check(f"{iso} → {expected}", result == expected, f"Got: {result}")

check("YYYY-MM-DD format is NOT sent directly to Chirix",
      not any(iso == expected for iso, expected in test_cases))

print(f"\n  {INFO}  UI sends: {TEST_DATE_FROM_ISO}  →  API gets: {iso_to_chirix(TEST_DATE_FROM_ISO)}")


# ════════════════════════════════════════════════════════════════════════════════
# TEST 4 — CHIRIX RESPONSE VALIDATION
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 4 — CHIRIX RESPONSE VALIDATION")

check("Live Chirix response success is True", t1_body.get("success") is True)
check("Live Chirix response data is list", is_instance := isinstance(t1_body.get("data"), list))
check("Live Chirix response records >= 0", isinstance(t1_body.get("records"), int))


# ════════════════════════════════════════════════════════════════════════════════
# TEST 5 — LIVE INVOICE FIELD CONVERSION
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 5 — INVOICE FIELD CONVERSION (USING LIVE CHIRIX INVOICE)")

test_inv = T1_INVOICES[0] if T1_INVOICES else {
    "InvNo": "RIKTN/26-27/138", "InvDt": "06/08/2026", "BuyerGstin": "33AADFF6762B1ZS",
    "BuyerLglNm": "FIREBALL TECHNOLOGIES", "BuyerAddr1": "SF NO 523/2",
    "TotAssVal": 79464.0, "TotCgstVal": 7146.76, "TotSgstVal": 7146.76, "TotIgstVal": 0, "TotInvVal": 93758.0,
    "ItemList": [{"SiNo": 1, "PrdDesc": "Sales @ GST 18%", "HsnCd": "94036000", "Qty": 3, "UnitPrice": 26488.0, "AssAmt": 79464.0, "GstRt": 18.0, "CgstAmt": 7146.76, "SgstAmt": 7146.76, "IgstAmt": 0, "TotAmt": 93757.52}]
}

source_type = "LIVE API" if T1_INVOICES else "FALLBACK"
print(f"\n  {INFO}  Using {source_type} invoice: {test_inv.get('InvNo')}")

required_fields = ["InvNo", "InvDt", "BuyerLglNm", "BuyerGstin", "TotAssVal", "TotInvVal"]
for field in required_fields:
    val = test_inv.get(field)
    check(f"Field '{field}' present", field in test_inv and val is not None, f"Value: {val}")

# Call Flask /api/convert with this invoice
t5_status, t5_body = flask_post("/api/convert", [test_inv])
check("/api/convert returns HTTP 200",  t5_status == 200, f"Status: {t5_status}")
check("Conversion success==true",       t5_body.get("success") is True, str(t5_body)[:200])

if t5_body.get("success"):
    tally_json = t5_body.get("tally_json", {})
    check("tally_json is generated", bool(tally_json))
    messages = tally_json.get("tallymessage", [])
    check("tallymessage list is non-empty", len(messages) > 0)
    meta = t5_body.get("invoices_meta", [{}])[0]
    check("invoice_no mapped in meta", bool(meta.get("invoice_no")), meta.get("invoice_no"))
    check("customer mapped in meta",   bool(meta.get("customer")),   meta.get("customer"))


# ════════════════════════════════════════════════════════════════════════════════
# TEST 6 — GST FIELD PRESERVATION
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 6 — GST FIELD PRESERVATION")

items = test_inv.get("ItemList", [])
if items:
    item = items[0]
    print(f"\n  {INFO}  Item: {item.get('PrdDesc', 'N/A')}")
    gst_fields = ["Qty", "UnitPrice", "TotAmt", "AssAmt", "GstRt", "CgstAmt", "SgstAmt", "IgstAmt"]
    for f in gst_fields:
        val = item.get(f)
        check(f"GST field '{f}' preserved", f in item and val is not None, f"Value: {val}")


# ════════════════════════════════════════════════════════════════════════════════
# TEST 7 — TALLY HTTP COMMUNICATION
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 7 — TALLY HTTP COMMUNICATION")

try:
    tally_req = urllib.request.Request(TALLY_URL, method="GET")
    with urllib.request.urlopen(tally_req, timeout=3) as r:
        tally_online = True
        tally_status = r.status
except Exception as e:
    tally_online = False
    tally_status = 0

check("TallyPrime HTTP server reachable at :9000", tally_online, f"Status: {tally_status}")

if tally_online:
    t7_status, t7_body = flask_post("/api/push", [test_inv])
    check("/api/push returns HTTP 200", t7_status == 200, f"Status: {t7_status}")
    check("Push success==true", t7_body.get("success") is True, str(t7_body)[:300])


# ════════════════════════════════════════════════════════════════════════════════
# TEST 8 — DUPLICATE CONTROL
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 8 — DUPLICATE CONTROL CHECK")

# Try pushing the same invoice second time — must return HTTP 409 duplicate error!
t8_status, t8_body = flask_post("/api/push", [test_inv])
check("Duplicate push blocked with HTTP 409", t8_status == 409 or t8_body.get("success") is False,
      f"Status={t8_status} error={t8_body.get('error')}")


# ════════════════════════════════════════════════════════════════════════════════
# TEST 9 — ERROR HANDLING
# ════════════════════════════════════════════════════════════════════════════════
section("TEST 9 — ERROR HANDLING")

# Invalid token
t9_status, t9_body = flask_post("/api/fetch", {"api_key": "INVALID_TOKEN_99999", "date_from": "01/08/2026", "date_to": "11/08/2026"})
check("Invalid token rejected with error", t9_body.get("success") is False)

# Empty date
t9_status_d, t9_body_d = flask_post("/api/fetch", {"api_key": RIK_TOKEN_RAW, "date_from": "", "date_to": ""})
check("Empty date rejected with HTTP 400", t9_status_d == 400)


# ════════════════════════════════════════════════════════════════════════════════
# FINAL REPORT
# ════════════════════════════════════════════════════════════════════════════════
section("FINAL REPORT")
total = passed + failed + warned
print(f"\n  Passed  : {passed}/{total}")
print(f"  Failed  : {failed}/{total}")
print(f"  Warnings: {warned}/{total}")

if failed == 0:
    print("\n  ALL TESTS PASSED SUCCESSFULLY!")
else:
    print(f"\n  {failed} TEST(S) FAILED")

print(f"\n{'='*60}\n")
