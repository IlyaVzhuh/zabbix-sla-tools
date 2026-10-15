#!/usr/bin/env python3
"""vpn external: stop one-off telegram.org 404s from counting as VPN outages.

Adds a host trigger that fires only after 3 non-200 checks in a row (or no
answer at all for 5 min), with the same tags/priority as the template trigger,
then disables the inherited one-shot template trigger on this host only.
The template (also used by 1C, mail, cloud, NAS) is not touched.
"""
import json
import os
import ssl
import urllib.request

TOKEN = os.environ["ZABBIX_TOKEN"]
URL = os.environ.get("ZABBIX_URL", "https://zabbix.example.com/api_jsonrpc.php")
# self-signed certificate on the internal Zabbix
CTX = ssl._create_unverified_context()
OLD = "34039"
KEY = '/vpn external/http.response.code["{$WEB.URL}"]'   # key is stored with the macro


def api(method, params):
    req = urllib.request.Request(URL, data=json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json-rpc", "Authorization": "Bearer " + TOKEN})
    r = json.load(urllib.request.urlopen(req, context=CTX, timeout=120))
    if "error" in r:
        raise SystemExit(f"{method}: {r['error']}")
    return r["result"]


old = api("trigger.get", {"triggerids": OLD, "output": ["priority"], "selectTags": ["tag", "value"]})[0]
new = api("trigger.create", {
    "description": "VPN for staff outside: telegram.org not answering 200 for 3 checks in a row",
    "expression": f'count({KEY},#3,"ne","200")=3 or nodata({KEY},5m)=1',
    "priority": old["priority"],
    "tags": old["tags"],
    "comments": "Replaces the template trigger 'HTTP service is returning unexpected status code' "
                "on this host (disabled here): telegram.org itself returns a one-off HTTP 404 about "
                "once a day while the VPN works. nodata covers a request that fails completely.",
})["triggerids"][0]
api("trigger.update", {"triggerid": OLD, "status": 1})
for t in api("trigger.get", {"hostids": "10830", "output": ["triggerid", "description", "status", "state", "error"]}):
    print(t["triggerid"], "disabled" if t["status"] == "1" else "enabled", t["description"], t["error"])
print(f"new trigger {new}; template trigger {OLD} disabled on vpn external")
