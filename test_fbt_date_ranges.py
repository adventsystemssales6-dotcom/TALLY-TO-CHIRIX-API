import urllib.request
import json
import ssl

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

token = "52494B54432D30303037" # FBT

test_spans = [
    ("01/04/2026", "11/08/2026"),
    ("01/05/2026", "31/05/2026"),
    ("01/06/2026", "30/06/2026"),
    ("01/07/2026", "31/07/2026"),
    ("01/08/2026", "11/08/2026"),
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
            print(f"FBT Range {df} -> {dt}: records={res.get('records')}")
    except Exception as e:
        print(f"FBT Range {df} -> {dt}: ERROR {e}")
