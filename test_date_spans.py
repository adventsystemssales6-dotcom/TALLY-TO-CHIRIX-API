import urllib.request
import json
import ssl

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

token = "52494B54432D30303036" # RIK

test_spans = [
    ("01/07/2026", "11/08/2026"), # July 1 to Aug 11 (41 days)
    ("15/07/2026", "11/08/2026"), # July 15 to Aug 11 (27 days)
    ("01/07/2026", "31/07/2026"), # July 1 to July 31 (31 days)
    ("11/07/2026", "11/08/2026"), # July 11 to Aug 11 (31 days)
    ("10/07/2026", "11/08/2026"), # July 10 to Aug 11 (32 days)
    ("29/04/2026", "29/05/2026"), # April 29 to May 29 (30 days)
]

for df, dt in test_spans:
    payload = json.dumps({'dateFrom': df, 'dateTo': dt}).encode('utf-8')
    req = urllib.request.Request(
        'https://www.chirixsolutions.com/api/data',
        data=payload,
        headers={
            'Chirix-Auth-Token': token,
            'Transaction-Type': 'SIV',
            'Content-Type': 'application/json'
        },
        method='GET'
    )
    try:
        with urllib.request.urlopen(req, context=ssl_ctx) as r:
            res = json.loads(r.read().decode('utf-8'))
            print(f"Range {df} -> {dt}: records={res.get('records')}")
    except Exception as e:
        print(f"Range {df} -> {dt}: ERROR {e}")
