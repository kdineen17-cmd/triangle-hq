"""Convert raw Robinhood get_equity_historicals JSON dumps into one CSV.

Usage: python build_dataset.py <out.csv> <dump1.json> [dump2.json ...]
"""
import csv
import json
import sys


def main(out_path, paths):
    rows = 0
    seen = set()
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "open", "high", "low", "close", "volume"])
        for p in paths:
            with open(p) as fh:
                data = json.load(fh)
            for res in data["data"]["results"]:
                sym = res["symbol"]
                for b in res.get("bars", []):
                    if b.get("interpolated"):
                        continue
                    key = (sym, b["begins_at"][:10])
                    if key in seen:
                        continue
                    seen.add(key)
                    w.writerow([key[1], sym, b["open_price"], b["high_price"],
                                b["low_price"], b["close_price"], b["volume"]])
                    rows += 1
    print(f"wrote {rows} rows, {len({s for s, _ in seen})} symbols -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
