"""Refresh the repository's last-known-good mutual-fund cache from official sources."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mutual_funds import CACHE_PATH, FUND_SPECS, get_mutual_fund_data


def main():
    existing = {"generated_at": None, "funds": {}}
    if CACHE_PATH.exists():
        existing = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    funds = dict(existing.get("funds", {}))
    successes = 0
    now = datetime.now(ZoneInfo("Asia/Tokyo"))

    for code in FUND_SPECS:
        record = get_mutual_fund_data(code, now=now, allow_cache=False)
        if not record["success"]:
            print(f"[WARN] {code}: {record['error']} — preserving existing cache")
            continue
        funds[code] = {
            "product_name": record["product_name"],
            "price": record["price"],
            "base_date": record["base_date"],
            "previous_change": record["previous_change"],
            "previous_change_pct": record["previous_change_pct"],
            "ttm_distribution": record["ttm_distribution"],
            "forecast_distribution": record["forecast_distribution"],
            "forecast_source": record["forecast_source"],
        }
        successes += 1
        print(f"[OK] {code}: {record['base_date']} NAV={record['price']}")

    if not funds:
        raise SystemExit("No cache data available")

    payload = {
        "generated_at": now.isoformat(),
        "funds": funds,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Cache refreshed: {successes}/{len(FUND_SPECS)} live sources succeeded")


if __name__ == "__main__":
    main()
