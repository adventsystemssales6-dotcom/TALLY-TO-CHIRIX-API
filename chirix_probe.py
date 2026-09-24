#!/usr/bin/env python3
"""
Chirix API Probe — Tests every possible method/format combination
to find exactly what the API accepts.
"""
import sys
import json
import ssl
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHIRIX_URL    = "https://www.chirixsolutions.com/api/data"
RIK_TOKEN_RAW = "52494B54432D30303036"
FBT_TOKEN_RAW = "52494B54432D30303037"

DATE_FROM = "01/08/2026"
DATE_TO   = "11/08/2026"

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode    = ssl.CERT_NONE

def probe(label, method, url, headers, data=None):
    print(f"\n--- {label} ---")
    print(f"  URL    : {url}")
    print(f"  Method : {method}")
    for k, v in headers.items():
        if "token" in k.lower() or "auth" in k.lower():
            print(f"  {k}: {v[:15]}***")
        else:
            print(f"  {k}: {v}")
    if data:
        print(f"  Body   : {data.decode()}")
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as r:
            body = r.read().decode("utf-8", errors="replace")
            print(f"  STATUS : {r.status} OK")
            try:
                j = json.loads(body)
                print(f"  success: {j.get('success')}")
                print(f"  records: {j.get('records')}")
                if j.get('data'):
                    print(f"  data[0]: {json.dumps(j['data'][0])[:200]}")
            except Exception:
                print(f"  BODY   : {body[:300]}")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"  STATUS : {e.code} {e.reason}")
        try:
            j = json.loads(body)
            print(f"  error_type: {j.get('error_type')}")
            print(f"  message   : {j.get('message')}")
        except Exception:
            print(f"  BODY: {body[:200]}")
        return e.code, body
    except Exception as ex:
        print(f"  ERROR  : {ex}")
        return 0, str(ex)

payload = json.dumps({"dateFrom": DATE_FROM, "dateTo": DATE_TO}).encode("utf-8")
payload_alt = json.dumps({"date_from": DATE_FROM, "date_to": DATE_TO}).encode("utf-8")

base_headers_rik = {
    "Chirix-Auth-Token": f"RIK - {RIK_TOKEN_RAW}",
    "Transaction-Type":  "SIV",
    "Content-Type":      "application/json",
}
base_headers_fbt = {
    "Chirix-Auth-Token": f"FBT - {FBT_TOKEN_RAW}",
    "Transaction-Type":  "SIV",
    "Content-Type":      "application/json",
}

print("=" * 60)
print("  CHIRIX API PROBE")
print("=" * 60)

# Try 1: GET + JSON body + RIK
probe("T1: GET + body + RIK", "GET", CHIRIX_URL, base_headers_rik, payload)

# Try 2: POST + JSON body + RIK
probe("T2: POST + body + RIK", "POST", CHIRIX_URL, base_headers_rik, payload)

# Try 3: GET + JSON body + FBT
probe("T3: GET + body + FBT", "GET", CHIRIX_URL, base_headers_fbt, payload)

# Try 4: POST + JSON body + FBT
probe("T4: POST + body + FBT", "POST", CHIRIX_URL, base_headers_fbt, payload)

# Try 5: GET with query params (no body)
qs_url = f"{CHIRIX_URL}?dateFrom={DATE_FROM.replace('/', '%2F')}&dateTo={DATE_TO.replace('/', '%2F')}"
probe("T5: GET + query params + RIK (no body)", "GET", qs_url, {
    "Chirix-Auth-Token": f"RIK - {RIK_TOKEN_RAW}",
    "Transaction-Type":  "SIV",
}, None)

# Try 6: POST + alt field names
probe("T6: POST + alt field names (date_from/date_to) + RIK", "POST",
      CHIRIX_URL, base_headers_rik, payload_alt)

# Try 7: GET, no Transaction-Type header
h7 = {
    "Chirix-Auth-Token": f"RIK - {RIK_TOKEN_RAW}",
    "Content-Type":      "application/json",
}
probe("T7: GET + body + RIK, no Transaction-Type", "GET", CHIRIX_URL, h7, payload)

# Try 8: Raw token (no prefix)
h8 = {
    "Chirix-Auth-Token": RIK_TOKEN_RAW,
    "Transaction-Type":  "SIV",
    "Content-Type":      "application/json",
}
probe("T8: GET + body + raw token only (no RIK - prefix)", "GET", CHIRIX_URL, h8, payload)

# Try 9: Authorization: Bearer header
h9 = {
    "Authorization":    f"Bearer {RIK_TOKEN_RAW}",
    "Transaction-Type": "SIV",
    "Content-Type":     "application/json",
}
probe("T9: GET + Authorization Bearer RIK", "GET", CHIRIX_URL, h9, payload)

# Try 10: Check what the base URL returns (no body)
probe("T10: GET root / no headers", "GET", CHIRIX_URL, {}, None)

print("\n" + "=" * 60)
print("  PROBE COMPLETE")
print("=" * 60)
