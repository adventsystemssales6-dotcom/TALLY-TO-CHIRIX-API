import urllib.request
import json
import ssl

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

tokens = [('RIK', '52494B54432D30303036'), ('FBT', '52494B54432D30303037')]

months = [
    # 2025
    ("01/01/2025", "31/01/2025"), ("01/02/2025", "28/02/2025"), ("01/03/2025", "31/03/2025"),
    ("01/04/2025", "30/04/2025"), ("01/05/2025", "31/05/2025"), ("01/06/2025", "30/06/2025"),
    ("01/07/2025", "31/07/2025"), ("01/08/2025", "31/08/2025"), ("01/09/2025", "30/09/2025"),
    ("01/10/2025", "31/10/2025"), ("01/11/2025", "30/11/2025"), ("01/12/2025", "31/12/2025"),
    # 2026
    ("01/01/2026", "31/01/2026"), ("01/02/2026", "28/02/2026"), ("01/03/2026", "31/03/2026"),
    ("01/04/2026", "30/04/2026"), ("01/05/2026", "31/05/2026"), ("01/06/2026", "30/06/2026"),
    ("01/07/2026", "31/07/2026"), ("01/08/2026", "31/08/2026"), ("01/09/2026", "30/09/2026"),
    ("01/10/2026", "31/10/2026"), ("01/11/2026", "30/11/2026"), ("01/12/2026", "31/12/2026"),
]

for name, token in tokens:
    print(f"\n--- Checking all months for {name} ({token}) ---")
    for df, dt in months:
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
                recs = res.get('records', 0)
                if recs > 0:
                    print(f"  [DATA FOUND] {df} -> {dt}: {recs} invoice(s)")
        except Exception as e:
            print(f"  {df} -> {dt}: ERROR {e}")
