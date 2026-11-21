"""Turn state/state.json + state/rules.json into database operations for the page
"Виртуальный счёт Олега" (artifact 96edafd2-3aa6-43db-84af-77ba50311b25).

Claude runs this during the twice-daily check, then sends each printed batch with
mcp__claude-artifacts__db_write.

Usage:
  python tools/to_page.py versions.json out_dir
versions.json maps every page document path that already exists to its version,
e.g. {"state/account": 14, "trades/2026-11-13-hype-AKE-buy-0844": 1}. Build it from
db_get / db_list results. Paths missing from the map are created.
Writes out_dir/batch_1.json, batch_2.json, ... each a list of ops under ~50 KB.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "state")


def main():
    versions = json.load(open(sys.argv[1], encoding="utf-8"))
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    st = json.load(open(os.path.join(ROOT, "state.json"), encoding="utf-8"))
    rules = json.load(open(os.path.join(ROOT, "rules.json"), encoding="utf-8"))
    b = st["books"]
    m = st.get("market", {})
    sm = st.get("stockMarket", {})
    h = st.get("health", {})
    docs = {}
    docs["state/account"] = {
        "startDate": b["trend"]["startDate"], "startEquity": b["trend"]["startEquity"], "cash": b["trend"]["cash"],
        "positions": b["trend"]["positions"], "tradeCount": b["trend"].get("tradeCount", 0),
        "benchmark": st["benchmark"], "rulesVersion": rules.get("version"),
        "lastRun": (st.get("lastRun") or "")[:10], "lastRunText": st.get("lastRunText", ""), "feeRate": 0.004}
    docs["state/hype"] = {k: b["hype"].get(k) for k in ("startDate", "startEquity", "cash", "positions", "cooldown", "tradeCount")}
    docs["state/stocks"] = {k: b["stocks"].get(k) for k in ("startDate", "startEquity", "cash", "positions", "tradeCount")}
    docs["state/market"] = {"source": "Kraken (через сторожа)", "updatedAt": m.get("updatedAt"), "prices": m.get("prices", {}),
                            "closes": {k: v[-60:] for k, v in m.get("closes", {}).items()}}
    docs["state/stockmarket"] = {"updatedAt": sm.get("updatedAt"), "marketOpen": sm.get("marketOpen", False),
                                 "prices": sm.get("prices", {}), "benchmark": st.get("stocksBenchmark", {})}
    docs["state/scan"] = st.get("scan") or {}
    docs["state/rules"] = {k: v for k, v in rules.items() if k != "paused"}
    docs["state/watcher"] = {"lastRun": h.get("lastRun"), "lastRunText": st.get("lastRunText", ""), "runs": h.get("runs", 0),
                             "errors": h.get("errors", []), "kraken": h.get("kraken"), "coinlore": h.get("coinlore"), "yahoo": h.get("yahoo")}
    docs["state/events"] = {"items": st.get("events", [])[-60:]}
    for d, snap in sorted(st.get("equityDaily", {}).items())[-14:]:
        docs[f"equity/{d}"] = {k: v for k, v in snap.items() if k != "t"}
    for t in st.get("trades", []):
        path = f"trades/{t['id']}"
        if path not in versions:  # trades never change once written
            docs[path] = {k: v for k, v in t.items() if k != "id"}

    ops, batches, size = [], [], 0
    for path, data in docs.items():
        op = {"op": "set", "path": path, "data": data}
        if path in versions:
            op["ifVersion"] = versions[path]
        n = len(json.dumps(op, ensure_ascii=False).encode())
        if ops and (size + n > 50000 or len(ops) >= 40):
            batches.append(ops)
            ops, size = [], 0
        ops.append(op)
        size += n
    if ops:
        batches.append(ops)
    for i, bt in enumerate(batches, 1):
        json.dump(bt, open(os.path.join(out, f"batch_{i}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{len(docs)} documents in {len(batches)} batches -> {out}")


if __name__ == "__main__":
    main()
