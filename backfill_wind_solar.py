"""
Balanced backfill of wind + solar forecasts into aalborg_energy_prices.

Why: price spans the full 4 years (2022→now), but wind was only added by the
live pipeline ~6 months ago and solar was never collected at all. This pulls the
Forecasts_Hour dataset (which carries Onshore Wind, Offshore Wind AND Solar back
to 2019) and fills BOTH series across the whole price history so all three line
up.

Source (energidataservice.dk, no key):
  Forecasts_Hour (hourly, DK1):
    wind_forecast_mw  = sum(Onshore Wind + Offshore Wind) ForecastDayAhead per hour
    solar_forecast_mw = Solar ForecastDayAhead per hour

Idempotent / fill-only: COALESCE keeps any existing value, so the 6 months of
wind already stitched by the live pipeline are left untouched — only NULL holes
(wind's missing 3.5 yrs, all of solar) get filled.

Run (inside the energy-dash image, on the DB's docker network):
  docker run --rm --network n8n_internal_energy --env-file /opt/energy-dash/.env \
    -v /opt/energy-dash:/work -w /work energy-dash python backfill_wind_solar.py
"""
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

import pandas as pd
from sqlalchemy import create_engine, text

API = "https://api.energidataservice.dk/dataset"
YEARS = [2022, 2023, 2024, 2025, 2026]  # price history starts 2022-01-01
DB_URL = os.environ["ENERGY_DB_URL"]


def fetch_year(year: int) -> pd.DataFrame:
    """Fetch one year of DK1 wind+solar forecasts (chunked to keep responses sane)."""
    filt = {"PriceArea": ["DK1"],
            "ForecastType": ["Onshore Wind", "Offshore Wind", "Solar"]}
    url = (f"{API}/Forecasts_Hour?start={year}-01-01&end={year + 1}-01-01"
           f"&filter={quote(json.dumps(filt))}&limit=0")
    for attempt in range(5):
        try:
            with urlopen(url, timeout=300) as r:
                return pd.DataFrame(json.load(r)["records"])
        except HTTPError as e:
            if e.code == 429 and attempt < 4:
                wait = 15 * (attempt + 1)  # back off: 15s, 30s, 45s, 60s
                print(f"  429 rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def main():
    frames = []
    for y in YEARS:
        df = fetch_year(y)
        print(f"{y}: {len(df):,} forecast records")
        if not df.empty:
            frames.append(df)
        time.sleep(8)  # be gentle: stay under the API rate limit between years
    fc = pd.concat(frames, ignore_index=True)

    fc["hour"] = pd.to_datetime(fc["HourUTC"], utc=True)
    fc["val"] = pd.to_numeric(fc["ForecastDayAhead"], errors="coerce")
    fc["kind"] = fc["ForecastType"].map(lambda t: "solar" if "Solar" in t else "wind")

    # sum per hour; min_count=1 so an hour with no records stays NULL (not 0)
    wind = fc[fc.kind == "wind"].groupby("hour")["val"].sum(min_count=1)
    solar = fc[fc.kind == "solar"].groupby("hour")["val"].sum(min_count=1)
    hourly = pd.DataFrame({"wind": wind, "solar": solar}).reset_index()
    # naive-UTC hour key to join against (timestamp AT TIME ZONE 'UTC')
    hourly["hour_utc"] = hourly["hour"].dt.tz_convert("UTC").dt.tz_localize(None)
    hourly = hourly[["hour_utc", "wind", "solar"]]
    print(f"hourly rows: {len(hourly):,} | "
          f"wind hrs: {hourly['wind'].notna().sum():,} | "
          f"solar hrs: {hourly['solar'].notna().sum():,}")
    print(f"range: {hourly['hour_utc'].min()} -> {hourly['hour_utc'].max()}")

    eng = create_engine(DB_URL)
    with eng.begin() as c:
        hourly.to_sql("_fc_backfill", c, if_exists="replace", index=False)
        c.execute(text("CREATE INDEX ON _fc_backfill (hour_utc)"))
        res = c.execute(text("""
            UPDATE aalborg_energy_prices p
            SET wind_forecast_mw  = COALESCE(p.wind_forecast_mw,  t.wind),
                solar_forecast_mw = COALESCE(p.solar_forecast_mw, t.solar)
            FROM _fc_backfill t
            WHERE date_trunc('hour', p.timestamp AT TIME ZONE 'UTC') = t.hour_utc
        """))
        print(f"rows updated: {res.rowcount:,}")
        c.execute(text("DROP TABLE _fc_backfill"))


if __name__ == "__main__":
    main()
