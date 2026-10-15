#!/usr/bin/env python3
"""Backfill the proportional internet SLA (sla-metrics: sla.internet.pct.hist).

Per scheduled hour (07:00-20:59 MSK = 04-17 UTC) since 2026-09-01:
    100 x internet up x NetBird up x VPN external up x Wi-Fi APs online / 100
- before 2026-09-22: the hourly *.hist trapper items (rebuilt from trends earlier)
- after: hourly trends of the live items; VPN external from raw history with the
  current rule (down only after 3 non-200 checks in a row)
Pushes 60 values per hour (same weight as the live 1-minute item) up to the first
value of sla.internet.pct. Reads only; existing items and data are not changed.
"""
import collections
import datetime as dt
import json
import os
import ssl
import sys
import urllib.request

TOKEN = os.environ["ZABBIX_TOKEN"]
URL = os.environ.get("ZABBIX_URL", "https://zabbix.example.com/api_jsonrpc.php")
# self-signed certificate on the internal Zabbix
CTX = ssl._create_unverified_context()

LIVE, HIST = "83424", "83425"
# component: (backfill trapper item, live item)
SRC = {"internet": ("78467", "78452"), "vpn_int": ("78469", "78464"),
       "vpn_ext": ("78468", "78453"), "wifi": ("78470", "78465")}
START = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
HOURS_UTC = range(4, 18)   # 07:00-20:59 MSK


def api(method, params):
    req = urllib.request.Request(URL, data=json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json-rpc", "Authorization": "Bearer " + TOKEN})
    r = json.load(urllib.request.urlopen(req, context=CTX, timeout=300))
    if "error" in r:
        sys.exit(f"{method}: {r['error']}")
    return r["result"]


def history(itemid, t0, t1, step=2 * 86400):
    out = []
    for vt in (0, 3):
        for a in range(t0, t1, step):
            out += api("history.get", {"itemids": itemid, "history": vt, "time_from": a,
                                       "time_till": min(a + step, t1) - 1,
                                       "output": ["clock", "value"], "limit": 1000000})
    return sorted((int(x["clock"]), float(x["value"])) for x in out)


def main():
    first = api("history.get", {"itemids": LIVE, "history": 0, "sortfield": "clock",
                                "sortorder": "ASC", "limit": 1, "output": ["clock"]})
    t_live = int(first[0]["clock"])
    if api("history.get", {"itemids": HIST, "history": 0, "limit": 1, "output": ["clock"]}):
        sys.exit("backfill item already has data; nothing done")
    h_live = t_live - t_live % 3600

    hourly = collections.defaultdict(dict)       # hour -> component -> 0..1 (wifi 0..100)
    for comp, (hist_id, live_id) in SRC.items():
        for c, v in history(hist_id, START, h_live):
            hourly[c - c % 3600][comp] = v
        if comp == "vpn_ext":
            continue
        for r in api("trend.get", {"itemids": [live_id], "time_from": START, "time_till": h_live - 1,
                                   "output": ["clock", "value_avg"], "limit": 1000000}):
            hourly[int(r["clock"])][comp] = float(r["value_avg"])

    # VPN external from raw checks: a check is "down" only as the 3rd non-200 in a row
    raw = history(SRC["vpn_ext"][1], START, h_live)
    per_hour = collections.defaultdict(lambda: [0, 0])
    for i, (c, v) in enumerate(raw):
        down = i >= 2 and v == 0 and raw[i - 1][1] == 0 and raw[i - 2][1] == 0
        per_hour[c - c % 3600][0] += 0 if down else 1
        per_hour[c - c % 3600][1] += 1
    for h, (up, n) in per_hour.items():
        hourly[h]["vpn_ext"] = up / n

    vals, skipped, by_month = [], 0, collections.defaultdict(list)
    for h in sorted(hourly):
        if h < START or h >= h_live or dt.datetime.fromtimestamp(h, dt.UTC).hour not in HOURS_UTC:
            continue
        c = hourly[h]
        if len(c) < 4:
            skipped += 1
            continue
        pct = 100 * c["internet"] * c["vpn_int"] * c["vpn_ext"] * c["wifi"] / 100
        vals += [{"itemid": HIST, "clock": h + 60 * m, "ns": 0, "value": round(pct, 6)} for m in range(60)]
        by_month[dt.datetime.fromtimestamp(h, dt.UTC).strftime("%Y-%m")].append(pct)
    if vals:   # minutes of the first live hour before the live item started
        vals += [{"itemid": HIST, "clock": t, "ns": 0, "value": vals[-1]["value"]}
                 for t in range(h_live, t_live - 30, 60)
                 if dt.datetime.fromtimestamp(t, dt.UTC).hour in HOURS_UTC]

    errors = 0
    for i in range(0, len(vals), 2000):
        r = api("history.push", vals[i:i + 2000])
        errors += sum(1 for x in r.get("data", []) if "error" in x)
    print(f"pushed {len(vals)} values, errors {errors}, hours skipped (missing a component) {skipped}")
    for m, v in sorted(by_month.items()):
        print(f"  {m}: {len(v)} scheduled hours, average {sum(v)/len(v):.3f} %")


if __name__ == "__main__":
    main()
