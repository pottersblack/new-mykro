import urllib.request, json, sys

def get(url):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            print('GET', url, '->', resp.getcode())
            print(resp.read().decode())
    except Exception as e:
        print('GET ERROR', url, e)

def post(url,data):
    try:
        b = json.dumps(data).encode()
        req = urllib.request.Request(url, data=b, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            print('POST', url, '->', resp.getcode())
            print(resp.read().decode())
    except Exception as e:
        print('POST ERROR', url, e)

get('http://127.0.0.1:8090/v1/models')
post('http://127.0.0.1:8090/v1/chat/completions', {'model':'mykro-large','messages':[{'role':'user','content':'Hello from test'}]})
