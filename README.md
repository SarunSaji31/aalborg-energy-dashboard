# Aalborg DK1 Energy Dashboard (Plotly Dash)

Interactive dashboard over the `aalborg_energy_prices` data (DK1 day-ahead
electricity price + wind forecast). Centrica portfolio piece.

Features:
- **Live data** from the energy Postgres over an SSH tunnel, with a CSV snapshot
  fallback if the DB is unreachable (the footer shows which source is active).
- **Briefing panel** mirroring the Telegram night-briefing: colour-graded price
  bars (green = cheap → red = pricey) with the wind forecast overlaid as a line.
- **One unified date control** (preset chips + a date-range picker) drives the
  whole page — pick a single day or a range; the briefing and the detail
  price/wind charts both follow it.

## Run it

```bash
# 1) open the SSH tunnel to the energy Postgres (keep this terminal open)
ssh -L 15432:localhost:5432 -N voxly

# 2) in another terminal
cd energy_dash
python3 -m venv .venv          # first time only
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:8050

If the tunnel isn't up, the app still runs on the CSV snapshot.

## Data

- **Live:** `load_data()` reads from Postgres using `ENERGY_DB_URL` in `.env`
  (git-ignored). The URL points at `localhost:15432`, forwarded to the
  `postgres_energy` container on voxly by the SSH tunnel above.
- **Fallback:** `data/energy_prices.csv`, a snapshot exported with:

  ```bash
  ssh voxly "docker exec postgres_energy psql -U sarun_admin -d energy_project_db -c \
    \"\copy (SELECT timestamp, price_dkk_kwh, wind_forecast_mw FROM aalborg_energy_prices ORDER BY timestamp) TO STDOUT WITH CSV HEADER\"" \
    > data/energy_prices.csv
  ```

`.env` and the CSV are git-ignored.

## Architecture note

All data access is isolated in `load_data()` in `app.py` (live DB → CSV
fallback). The date selection is a two-callback chain: `apply_preset`
(preset → sets the range) and `render` (range + resolution → the whole page),
so there are no callback loops.
