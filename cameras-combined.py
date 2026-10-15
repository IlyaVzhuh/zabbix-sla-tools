#!/usr/bin/env python3
"""Finish switching the cameras SLA to all NVR servers (run once).

1. Backfill trapper item cameras.online.pct.hist (sla-metrics) from NVR
   trends: per hour 100 * sum(online) / sum(total) over the servers that have
   data, one value per minute (same weight as the live 1m item), from
   2026-09-01 00:00 UTC up to the first value of cameras.online.pct.
2. Point cameras.avg.month / cameras.avg.prevmonth on NVR-1 at
   (hist + live) via trendsum/trendcount (both return 0 when there is no data).
3. Graph "Камеры онлайн, %" on dashboard 410: hist + live in one colour.
4. Delete the copied cameras.avg.* items on NVR-2 (they read NVR-1).
"""
import calendar
import collections
import json
import os
import ssl
import sys
import urllib.request

TOKEN = os.environ["ZABBIX_TOKEN"]
URL = os.environ.get("ZABBIX_URL", "https://zabbix.example.com/api_jsonrpc.php")
# self-signed certificate on the internal Zabbix
CTX = ssl._create_unverified_context()

LIVE, HIST = "83422", "83423"              # sla-metrics
MONTH, PREVMONTH = "83098", "83099"        # NVR-1, used by dashboard 410
T2_COPIES = ["83415", "83416"]             # NVR-2 cameras.avg.* copies
ONLINE = ["73536", "83404"]                # nvrchannelsonline T1, T2
TOTAL = ["73537", "83405"]                 # nvrchannelstotal  T1, T2
START = calendar.timegm((2026, 9, 1, 0, 0, 0))
DASHBOARD = "410"
GRAPH_NAME = "Камеры онлайн, %"


def api(method, params):
    req = urllib.request.Request(URL, data=json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json-rpc", "Authorization": "Bearer " + TOKEN})
    r = json.load(urllib.request.urlopen(req, context=CTX, timeout=60))
    if "error" in r:
        sys.exit(f"{method}: {r['error']}")
    return r["result"]


def backfill(gap=False):
    first = api("history.get", {"itemids": LIVE, "history": 0, "sortfield": "clock",
                                "sortorder": "ASC", "limit": 1, "output": ["clock"]})
    t0 = int(first[0]["clock"])
    h0 = t0 - t0 % 3600
    have = api("history.get", {"itemids": HIST, "history": 0, "sortfield": "clock",
                               "sortorder": "ASC", "limit": 1, "output": ["clock"]})
    if have and not gap:
        print("backfill: already has data, skipped")
        return
    # --gap: only values older than the earliest stored one (lost to the old 31d history limit)
    until = int(have[0]["clock"]) if have else t0
    tr = collections.defaultdict(dict)
    rows = api("trend.get", {"itemids": ONLINE + TOTAL, "time_from": START, "time_till": h0 - 1,
                             "output": ["itemid", "clock", "value_avg"], "limit": 1000000})
    for r in rows:
        tr[int(r["clock"])][r["itemid"]] = float(r["value_avg"])
    hours = {}
    for clock, v in sorted(tr.items()):
        pairs = [(v[o], v[t]) for o, t in zip(ONLINE, TOTAL) if o in v and t in v]
        total = sum(t for _, t in pairs)
        if total > 0:
            hours[clock] = 100 * sum(o for o, _ in pairs) / total
    last = hours[max(hours)]
    vals = [{"itemid": HIST, "clock": c + 60 * m, "ns": 0, "value": round(p, 6)}
            for c, p in hours.items() for m in range(60)]
    vals += [{"itemid": HIST, "clock": t, "ns": 0, "value": round(last, 6)}
             for t in range(h0, t0 - 30, 60)]
    vals = [v for v in vals if v["clock"] < until]
    if not vals:
        print("backfill: nothing missing")
        return
    errors = 0
    for i in range(0, len(vals), 2000):
        r = api("history.push", vals[i:i + 2000])
        errors += sum(1 for x in r.get("data", []) if "error" in x)
    oct1 = calendar.timegm((2026, 10, 1, 0, 0, 0))
    sep = [x["value"] for x in vals if x["clock"] < oct1]
    octo = [x["value"] for x in vals if x["clock"] >= oct1]
    avg = lambda a: f"{sum(a)/len(a):.3f}" if a else "-"
    print(f"backfill: {len(vals)} values, push errors {errors}; "
          f"Sep avg {avg(sep)}, Oct 1-13 avg {avg(octo)}")


def formulas():
    def f(period):
        return (f"(trendsum(/sla-metrics/cameras.online.pct.hist,{period})"
                f"+trendsum(/sla-metrics/cameras.online.pct,{period}))"
                f"/(trendcount(/sla-metrics/cameras.online.pct.hist,{period})"
                f"+trendcount(/sla-metrics/cameras.online.pct,{period}))")
    api("item.update", [{"itemid": MONTH, "params": f("1M:now/M+1M")},
                        {"itemid": PREVMONTH, "params": f("1M:now/M")}])
    print("formulas: cameras.avg.month / prevmonth now use all servers")


def graph():
    names = {i["itemid"]: i["name"] for i in api("item.get", {"itemids": [LIVE, HIST], "output": ["name"]})}
    d = api("dashboard.get", {"dashboardids": DASHBOARD, "selectPages": "extend"})[0]
    changed = 0
    for page in d["pages"]:
        for w in page["widgets"]:
            if w["type"] != "svggraph" or w["name"] != GRAPH_NAME:
                continue
            ds0 = [x for x in w["fields"] if x["name"].startswith("ds.0.")]
            rest = [x for x in w["fields"] if not x["name"].startswith("ds.")]
            new = []
            for n, item in ((0, names[HIST]), (1, names[LIVE])):
                for x in ds0:
                    y = dict(x, name=x["name"].replace("ds.0.", f"ds.{n}.", 1))
                    if y["name"] == f"ds.{n}.hosts.0":
                        y["value"] = "SLA metrics"
                    elif y["name"] == f"ds.{n}.items.0":
                        y["value"] = item
                    new.append(y)
            w["fields"] = rest + new
            changed += 1
    pages = [{"dashboard_pageid": p["dashboard_pageid"], "name": p["name"],
              "display_period": p["display_period"],
              "widgets": [{k: w[k] for k in ("widgetid", "type", "name", "x", "y", "width",
                                             "height", "view_mode", "fields")} for w in p["widgets"]]}
             for p in d["pages"]]
    api("dashboard.update", {"dashboardid": DASHBOARD, "pages": pages})
    print(f"graph: {changed} widget(s) now show SLA metrics backfill + live")


def cleanup():
    have = [i["itemid"] for i in api("item.get", {"itemids": T2_COPIES, "output": ["itemid"]})]
    if have:
        api("item.delete", have)
    print(f"cleanup: deleted {len(have)} copied item(s) on NVR-2")


if __name__ == "__main__":
    if "--gap" in sys.argv:
        backfill(gap=True)
        sys.exit()
    backfill()
    formulas()
    graph()
    cleanup()
