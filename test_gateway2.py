import json, sys
url = 'http://127.0.0.1:8090/v1/chat/completions'
data = {'model':'mykro-small','messages':[{'role':'user','content':'Ping from test (short).'}]}

try:
    import httpx
    print('using httpx')
    r = httpx.post(url, json=data, timeout=60.0)
    print('STATUS', r.status_code)
    print(r.text)
except Exception as e:
    print('httpx failed:', e)
    try:
        import urllib.request
        b = json.dumps(data).encode()
        req = urllib.request.Request(url, data=b, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            print('STATUS', resp.getcode())
            print(resp.read().decode())
    except Exception as e2:
        print('urllib failed:', e2)
