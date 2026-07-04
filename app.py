"""
Aalborg DK1 Energy Dashboard — Plotly Dash.

Data layer is deliberately isolated in load_data() so the rest of the app
doesn't care WHERE the data comes from. Step 1 (now): a local CSV snapshot.
Step 2 (later): swap load_data() to read live from the voxly Postgres over an
SSH tunnel, or deploy this app on voxly next to the DB. Nothing else changes.

Styling lives in assets/style.css (Dash auto-loads everything in assets/).
"""

import os
import threading
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DATA_FILE = Path(__file__).parent / "data" / "energy_prices.csv"
DB_URL = os.getenv("ENERGY_DB_URL")

# DK1 consumer price already includes 25% VAT: SpotPriceDKK / 1000 * 1.25.
PRICE_COL = "price_dkk_kwh"
WIND_COL = "wind_forecast_mw"
SOLAR_COL = "solar_forecast_mw"

# Palette mirrors assets/style.css so charts match the page.
INK = "#1a2230"
MUTED = "#6b7785"
PRIMARY = "#1e6f5c"
ACCENT = "#2f80ed"
SOLAR = "#f2a900"  # amber — solar forecast line (distinct from the blue wind line)
NEGATIVE = "#d64545"
GRID = "#eef1f5"
FONT = "Inter, -apple-system, Segoe UI, sans-serif"


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Shared shaping so live and snapshot paths return identical frames."""
    # The frozen CSV snapshot predates the solar column; keep the frame shape
    # identical so every downstream chart can assume SOLAR_COL exists.
    if SOLAR_COL not in df.columns:
        df[SOLAR_COL] = pd.NA
    df = df.set_index("timestamp").sort_index()
    # Timestamps are stored in UTC, but the market day — and the Telegram
    # briefing this dashboard mirrors — run on Europe/Copenhagen wall-clock.
    # Convert before any day-slicing or hour labels, else every hour reads ~2h
    # early (CEST) and the day window is misaligned. Kept tz-aware (not dropped
    # to naive) so the index stays monotonic across the autumn DST fall-back;
    # partial-string day slicing and %H:%M formatting honour the index tz.
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert("Europe/Copenhagen")
    # day_name / is_weekend in the DB are unreliable (NULL on live rows),
    # so derive weekday here instead of trusting those columns.
    df["weekday"] = df.index.day_name()
    df["is_weekend"] = df.index.dayofweek >= 5
    return df


def _load_from_db() -> pd.DataFrame:
    """Live read from the energy Postgres (postgres_energy on voxly) via the
    SSH tunnel. Imported lazily so the app still starts without DB libs."""
    import sqlalchemy as sa

    engine = sa.create_engine(DB_URL, connect_args={"connect_timeout": 5})
    query = (
        f"SELECT timestamp, {PRICE_COL}, {WIND_COL}, {SOLAR_COL} "
        "FROM aalborg_energy_prices ORDER BY timestamp"
    )
    df = pd.read_sql(query, engine, parse_dates=["timestamp"])
    return _finalize(df)


def _load_from_csv() -> pd.DataFrame:
    """Frozen snapshot fallback — keeps the dashboard alive if the tunnel is down."""
    df = pd.read_csv(DATA_FILE, parse_dates=["timestamp"])
    return _finalize(df)


def load_data() -> tuple[pd.DataFrame, str]:
    """Single source of data for the whole app. Tries the live Postgres first,
    falls back to the CSV snapshot. Returns (frame, source_label)."""
    if DB_URL:
        try:
            df = _load_from_db()
            return df, "Live · postgres_energy (voxly)"
        except Exception as exc:  # tunnel down, DB unreachable, etc.
            print(f"[load_data] live DB unavailable ({exc}); using CSV snapshot")
    return _load_from_csv(), "Snapshot · local CSV"


# Loading once at boot froze the dashboard at that moment's data: the n8n
# pipeline writes the next day's rows to Postgres every night at 22:00, but a
# long-running gunicorn worker would never see them (so the date picker stayed
# stuck on the boot day). Cache with a short TTL instead — the first request
# after the TTL expires re-reads Postgres, and new rows (plus the date-picker
# bounds derived from them) appear automatically within CACHE_TTL of any write.
CACHE_TTL = 300  # seconds
_cache: dict = {"df": None, "source": None, "ts": 0.0}
_cache_lock = threading.Lock()


def get_data() -> tuple[pd.DataFrame, str]:
    """Cached accessor for the live frame. Refreshes from Postgres once the
    cached copy is older than CACHE_TTL; otherwise serves the in-memory copy."""
    now = time.monotonic()
    with _cache_lock:
        if _cache["df"] is None or now - _cache["ts"] > CACHE_TTL:
            _cache["df"], _cache["source"] = load_data()
            _cache["ts"] = now
        return _cache["df"], _cache["source"]

# The page always shows a single day, so hourly is the finest useful default
# (raw 15-min is available for a closer look).
DEFAULT_RESAMPLE = "h"

app = Dash(
    __name__,
    title="Aalborg DK1 Energy",
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
    ],
)
server = app.server  # exposed for gunicorn/Docker later

GRAPH_CONFIG = {"displaylogo": False,
                "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"]}


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
# A function (not a static value) so Dash re-evaluates it on every page load.
# That re-reads get_data(), so the date-picker bounds and footer always reflect
# the latest rows in Postgres without restarting the container.
def serve_layout():
    df, source = get_data()
    min_date = df.index.min().date()
    max_date = df.index.max().date()
    n_records = len(df)
    return html.Div([
    html.Header(className="app-header", children=html.Div(className="inner", children=[
        html.H1([html.Span(className="dot"), "Aalborg DK1 — Power Price & Wind"]),
        html.P("Day-ahead consumer electricity price (DKK/kWh, incl. 25% VAT) and "
               "day-ahead wind forecast for the DK1 bidding zone."),
    ])),

    html.Div(className="container", children=[
        # Daily briefing — same content the Telegram bot sends each night,
        # presented interactively. One single-day picker drives the whole page.
        html.Div(className="panel briefing", children=[
            html.Div(className="briefing-head", children=[
                html.Div("📍", className="briefing-pin"),
                html.Div(id="briefing-title", className="briefing-title"),
                html.Div(className="briefing-controls", children=[
                    # One single-day picker drives the whole page.
                    dcc.DatePickerSingle(
                        id="master-date",
                        min_date_allowed=min_date,
                        max_date_allowed=max_date,
                        initial_visible_month=max_date,
                        date=max_date,
                        display_format="DD MMM YYYY",
                    ),
                ]),
            ]),
            html.Div(id="briefing-stats", className="kpi-grid"),
            dcc.Graph(id="briefing-chart", config=GRAPH_CONFIG,
                      className="briefing-chart"),
            html.Div(className="briefing-caption", children=[
                "Bars are the electricity price — ",
                html.Span("cheaper", className="lg lg-green"), " to ",
                html.Span("pricier", className="lg lg-red"),
                " per kWh. The ",
                html.Span("blue line", className="lg lg-blue"),
                " is wind and the ",
                html.Span("amber line", className="lg lg-solar"),
                " is solar: price tends to fall when wind and solar are high "
                "(and demand is low) — usually midday.",
            ]),
        ]),

        # Detail resolution — the date selection above drives the whole page;
        # this only changes how finely the line/area charts below are sampled.
        html.Div(className="panel controls", children=[
            html.Div([
                html.Label("Detail resolution", className="control-label"),
                dcc.RadioItems(
                    id="resample",
                    className="seg",
                    options=[
                        {"label": "Raw (15-min)", "value": "raw"},
                        {"label": "Hourly", "value": "h"},
                    ],
                    value=DEFAULT_RESAMPLE,
                    inline=True,
                ),
            ]),
        ]),

        # KPI cards
        html.Div(id="kpis", className="kpi-grid"),

        # Charts
        html.Div(className="panel chart-card", children=[
            html.Div("Electricity price", className="chart-title"),
            dcc.Graph(id="price-chart", config=GRAPH_CONFIG),
        ]),
        html.Div(className="panel chart-card", children=[
            html.Div("Day-ahead wind forecast", className="chart-title"),
            dcc.Graph(id="wind-chart", config=GRAPH_CONFIG),
        ]),
    ]),

    html.Footer(className="app-footer", children=[
        f"Source: energidataservice.dk (DK1) · {source} · {n_records:,} records · "
        f"{min_date} – {max_date}. Recent rows come from a live n8n feed, so "
        f"coverage thins out after early 2026."
    ]),
    ])


app.layout = serve_layout


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    if rule == "raw":
        return frame
    return frame.resample(rule).mean(numeric_only=True)


def kpi_card(label, value, unit="", variant=""):
    cls = "kpi" + (f" {variant}" if variant else "")
    return html.Div(className=cls, children=[
        html.Div(label, className="kpi-label"),
        html.Div([value, html.Span(unit, className="kpi-unit")] if unit
                 else value, className="kpi-value"),
    ])


def _style(fig: go.Figure) -> go.Figure:
    """Apply the shared chart theme so figures match the page."""
    fig.update_layout(
        template="plotly_white",
        font=dict(family=FONT, color=INK, size=13),
        margin=dict(t=12, r=18, b=12, l=18),
        height=320,
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
    )
    fig.update_xaxes(showgrid=False, showline=True, linecolor=GRID, title_text="")
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _empty(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False,
                       font=dict(family=FONT, color=MUTED, size=14),
                       xref="paper", yref="paper", x=0.5, y=0.5)
    fig.update_layout(height=320, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="white",
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


# ----------------------------------------------------------------------------
# Unified date selection — one control drives the whole page.
# ----------------------------------------------------------------------------
def _as_date(value):
    return pd.Timestamp(value).date() if value else None


def _briefing_view(df, day):
    """Briefing window + title for a single selected day (hourly view)."""
    win = df.loc[str(day): str(day)]
    return win, f"Aalborg / DK1 — {day:%a %d %b %Y}"


@app.callback(
    Output("briefing-title", "children"),
    Output("briefing-stats", "children"),
    Output("briefing-chart", "figure"),
    Output("kpis", "children"),
    Output("price-chart", "figure"),
    Output("wind-chart", "figure"),
    Input("master-date", "date"),
    Input("resample", "value"),
)
def render(date, rule):
    df, _ = get_data()
    max_date = df.index.max().date()
    day = _as_date(date) or max_date         # default to latest day if cleared
    mode = "hourly"

    win, title = _briefing_view(df, day)

    if win.empty:
        empty = _empty("No data in this selection.")
        dash_kpis = [kpi_card(lbl, "—") for lbl in
                     ("Avg price", "Lowest", "Highest", "Negative intervals")]
        return (title, [kpi_card("Average", "—")], empty,
                dash_kpis, empty, empty)

    # --- Briefing summary (top) ---
    avg = win[PRICE_COL].mean()
    avg_wind = win[WIND_COL].mean()
    avg_solar = win[SOLAR_COL].mean()
    grain = "h" if mode == "hourly" else "D"
    by = win[PRICE_COL].resample(grain).mean().dropna()
    lo_t, lo_v, hi_t, hi_v = by.idxmin(), by.min(), by.idxmax(), by.max()
    stamp = "%H:%M" if mode == "hourly" else "%d %b"
    stats = [
        kpi_card("Average", f"{avg:.2f}", " DKK/kWh"),
        kpi_card(f"🟢 Cheapest · {lo_t:{stamp}}", f"{lo_v:.2f}", " DKK/kWh",
                 variant="kpi-neg" if lo_v < 0 else ""),
        kpi_card(f"🔴 Priciest · {hi_t:{stamp}}", f"{hi_v:.2f}", " DKK/kWh"),
        kpi_card("Avg wind", _fmt_wind(avg_wind), " MW", variant="kpi-wind"),
        kpi_card("Avg solar", _fmt_wind(avg_solar), " MW", variant="kpi-solar"),
    ]
    briefing_fig = _briefing_figure(win, mode)

    # --- Detail charts (bottom) at the chosen resolution ---
    series = _resample(win, rule)
    # Plotly.js has no timezone support: a tz-aware x-axis is converted back to
    # UTC for display, which would re-introduce the ~2h shift on these charts.
    # Drop the offset (keeping the local wall-clock) so the axis stays local.
    if series.index.tz is not None:
        series = series.tz_localize(None)
    min_price, max_price = win[PRICE_COL].min(), win[PRICE_COL].max()
    neg_count = int((win[PRICE_COL] < 0).sum())
    kpis = [
        kpi_card("Avg price", f"{avg:.2f}", " DKK/kWh"),
        kpi_card("Lowest", f"{min_price:.2f}", " DKK/kWh",
                 variant="kpi-neg" if min_price < 0 else ""),
        kpi_card("Highest", f"{max_price:.2f}", " DKK/kWh"),
        kpi_card("Negative intervals", f"{neg_count:,}", variant="kpi-neg"),
    ]

    price_fig = px.line(series, y=PRICE_COL, labels={PRICE_COL: "DKK/kWh"})
    price_fig.update_traces(line=dict(color=PRIMARY, width=2),
                            hovertemplate="%{y:.2f} DKK/kWh<extra></extra>")
    price_fig.add_hline(y=0, line_dash="dot", line_color=NEGATIVE, line_width=1)
    _style(price_fig)

    wind = series[series[WIND_COL].notna()]
    if wind.empty:
        wind_fig = _empty("No wind-forecast data in this range "
                          "(wind is only populated for recent rows).")
    else:
        wind_fig = px.area(wind, y=WIND_COL, labels={WIND_COL: "MW"})
        wind_fig.update_traces(line=dict(color=ACCENT, width=2),
                               fillcolor="rgba(47,128,237,.12)",
                               hovertemplate="%{y:.0f} MW<extra></extra>")
        _style(wind_fig)

    return title, stats, briefing_fig, kpis, price_fig, wind_fig


# ----------------------------------------------------------------------------
# Daily briefing — mirrors the Telegram night-briefing the n8n pipeline sends.
# ----------------------------------------------------------------------------
def _fmt_wind(value) -> str:
    return f"{value:.0f}" if pd.notna(value) else "—"


def _briefing_figure(win: pd.DataFrame, mode: str) -> go.Figure:
    """Color-graded price bars (green = cheap, red = pricey) with the wind and
    solar forecasts overlaid as lines — reads at a glance, no table needed."""
    rule = "h" if mode == "hourly" else "D"
    fmt = "%H:%M" if mode == "hourly" else "%a %d %b"
    agg = win.resample(rule).agg({PRICE_COL: "mean", WIND_COL: "mean",
                                  SOLAR_COL: "mean"})
    agg = agg.dropna(subset=[PRICE_COL])

    labels = [f"{ts:{fmt}}" for ts in agg.index]
    price = agg[PRICE_COL]
    wind = agg[WIND_COL]
    solar = agg[SOLAR_COL]
    # Equal min/max (single bar) would break the colour mapping.
    cmin, cmax = price.min(), price.max()
    if cmax == cmin:
        cmax = cmin + 0.01

    fig = go.Figure()
    fig.add_bar(
        x=labels, y=price, name="Price",
        marker=dict(
            color=price, cmin=cmin, cmax=cmax,
            colorscale=[[0, PRIMARY], [0.5, "#f2c94c"], [1, NEGATIVE]],
            line=dict(width=0),
        ),
        hovertemplate="%{x} · %{y:.2f} DKK/kWh<extra></extra>",
    )
    if wind.notna().any():
        fig.add_scatter(
            x=labels, y=wind, name="Wind", mode="lines",
            yaxis="y2", line=dict(color=ACCENT, width=2.5, shape="spline"),
            hovertemplate="%{x} · %{y:.0f} MW wind<extra></extra>",
        )
    if solar.notna().any():
        fig.add_scatter(
            x=labels, y=solar, name="Solar", mode="lines",
            yaxis="y2", line=dict(color=SOLAR, width=2.5, shape="spline"),
            hovertemplate="%{x} · %{y:.0f} MW solar<extra></extra>",
        )
    fig.update_layout(
        template="plotly_white",
        font=dict(family=FONT, color=INK, size=12),
        margin=dict(t=14, r=52, b=10, l=10),
        height=360, bargap=0.22, hovermode="x unified",
        plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
        xaxis=dict(showgrid=False, showline=True, linecolor=GRID),
        yaxis=dict(title="DKK/kWh", gridcolor=GRID, zeroline=True, zerolinecolor=GRID),
        yaxis2=dict(title="wind / solar MW", overlaying="y", side="right",
                    showgrid=False, rangemode="tozero"),
    )
    return fig


if __name__ == "__main__":
    app.run(debug=True)
