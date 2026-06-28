"""
Backfill 2026 gaps in aalborg_energy_prices from Energinet's open API.

Source (energidataservice.dk, no key):
  - DayAheadPrices  (15-min, DK1)        -> price_dkk_kwh = DayAheadPriceDKK/1000*1.25
  - Forecasts_Hour  (hourly, DK1 wind)   -> wind = sum(Onshore + Offshore) per hour

Idempotent: upsert on the `timestamp` PK with COALESCE, so it only FILLS holes
(missing rows, partial days, NULL wind/day_name) and never overwrites good values.

Run:  .venv/bin/python backfill_2026.py            # uses the same SSH tunnel + .env as the app
Tunnel must be up:  ssh -L 15432:localhost:5432 -N voxly
"""
import json
from urllib.parse import quote

import pandas as pd
import requests
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

START = "2026-01-01"
END = "2026-06-29"  # exclusive upper bound -> through 2026-06-28
API = "https://api.energidataservice.dk/dataset"


def fetch(dataset: str, filt: dict) -> pd.DataFrame:
    url = (
        f"{API}/{dataset}?start={START}&end={END}"
        f"&filter={quote(json.dumps(filt))}&limit=0"  # limit=0 = no limit
    )
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return pd.DataFrame(r.json()["records"])


def main():
    # ---- prices (15-min) ----
    prices = fetch("DayAheadPrices", {"PriceArea": ["DK1"]})
    prices["timestamp"] = pd.to_datetime(prices["TimeUTC"], utc=True)
    prices["price_dkk_kwh"] = prices["DayAheadPriceDKK"] / 1000 * 1.25
    prices = prices[["timestamp", "price_dkk_kwh"]].sort_values("timestamp")

    # ---- wind (hourly, sum onshore+offshore) ----
    wind = fetch("Forecasts_Hour", {"PriceArea": ["DK1"],
                                     "ForecastType": ["Onshore Wind", "Offshore Wind"]})
    wind["hour"] = pd.to_datetime(wind["HourUTC"], utc=True)
    wind = (wind.groupby("hour")["ForecastDayAhead"].sum()
                .rename("wind_forecast_mw").reset_index())

    # map each 15-min price row to its hour's wind
    prices["hour"] = prices["timestamp"].dt.floor("h")
    df = prices.merge(wind, on="hour", how="left").drop(columns="hour")

    # derived (UTC convention, matching the table)
    df["price_area"] = "DK1"
    df["day_name"] = df["timestamp"].dt.day_name()
    df["is_weekend"] = df["timestamp"].dt.dayofweek >= 5

    print(f"Fetched {len(df):,} price rows, {wind['wind_forecast_mw'].notna().sum():,} wind hours")
    print(f"Range: {df['timestamp'].min()} -> {df['timestamp'].max()}")

    # ---- upsert (fill-only) ----
    upsert = text("""
        INSERT INTO aalborg_energy_prices
            (timestamp, price_dkk_kwh, wind_forecast_mw, price_area, day_name, is_weekend)
        VALUES (:timestamp, :price_dkk_kwh, :wind_forecast_mw, :price_area, :day_name, :is_weekend)
        ON CONFLICT (timestamp) DO UPDATE SET
            price_dkk_kwh    = COALESCE(aalborg_energy_prices.price_dkk_kwh, EXCLUDED.price_dkk_kwh),
            wind_forecast_mw = COALESCE(aalborg_energy_prices.wind_forecast_mw, EXCLUDED.wind_forecast_mw),
            price_area       = COALESCE(aalborg_energy_prices.price_area, EXCLUDED.price_area),
            day_name         = COALESCE(aalborg_energy_prices.day_name, EXCLUDED.day_name),
            is_weekend       = COALESCE(aalborg_energy_prices.is_weekend, EXCLUDED.is_weekend)
    """)
    rows = df.where(pd.notna(df), None).to_dict("records")

    eng = create_engine(dotenv_values(".env")["ENERGY_DB_URL"])
    with eng.begin() as c:
        before = c.execute(text(
            "SELECT count(*) FROM aalborg_energy_prices "
            "WHERE timestamp >= '2026-01-01' AND timestamp < '2026-06-29'")).scalar()
        c.execute(upsert, rows)
        after = c.execute(text(
            "SELECT count(*) FROM aalborg_energy_prices "
            "WHERE timestamp >= '2026-01-01' AND timestamp < '2026-06-29'")).scalar()
    print(f"2026 rows: {before:,} -> {after:,}  (+{after - before:,} inserted, rest updated)")


if __name__ == "__main__":
    main()
