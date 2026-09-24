import urllib.request
import json
import ssl

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

tokens = [
    ('RIK', '52494B54432D30303036'),
    ('FBT', '52494B54432D30303037')
]

for name, token in tokens:
    payload = json.dumps({'dateFrom': '01/08/2026', 'dateTo': '11/08/2026'}).encode('utf-8')
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
            print(f"{name} ({token}): STATUS {r.status}, success={res.get('success')}, records={res.get('records')}")
            if res.get('data'):
                print(f"   First InvNo: {res['data'][0].get('InvNo')}")
                print(f"   Buyer: {res['data'][0].get('BuyerLglNm')}")
                print(f"   Grand Total: {res['data'][0].get('TotInvVal')}")
    except Exception as e:
        print(f"{name} ({token}): ERROR {e}")
