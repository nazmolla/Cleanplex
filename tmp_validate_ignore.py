import json
import urllib.parse
import urllib.request
import urllib.error

guid = 'local://67166'
url = 'http://localhost:7979/api/scan/title/' + urllib.parse.quote(guid, safe='') + '/ignore'
data = json.dumps({'ignore': True}).encode('utf-8')
req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')

try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print('STATUS', r.status)
        print(r.read().decode('utf-8', 'replace'))
except urllib.error.HTTPError as e:
    print('STATUS', e.code)
    print(e.read().decode('utf-8', 'replace'))
except Exception as e:
    print('EXC', repr(e))
