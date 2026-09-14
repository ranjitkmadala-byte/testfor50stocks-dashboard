import os
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# ============================================================
# CONFIG
# ============================================================

# Local development fallback
load_dotenv(r"C:\upstox_dashboard\.env")

# Cloud: use Streamlit Secrets.
# Local: fall back to NEON_DATABASE_URL from .env.
try:
    DATABASE_URL = st.secrets["NEON_DATABASE_URL"]
except Exception:
    DATABASE_URL = os.getenv("NEON_DATABASE_URL")

IST = ZoneInfo("Asia/Kolkata")
MONEY_FLOW_TOP_N = int(os.getenv("MONEY_FLOW_TOP_N", "50"))
MONEY_FLOW_FREEZE_HOUR = int(os.getenv("MONEY_FLOW_FREEZE_HOUR", "9"))
MONEY_FLOW_FREEZE_MINUTE = int(os.getenv("MONEY_FLOW_FREEZE_MINUTE", "20"))
FREEZE_LABEL = f"{MONEY_FLOW_FREEZE_HOUR:02d}:{MONEY_FLOW_FREEZE_MINUTE:02d}"



@st.cache_data(ttl=300)
def load_same_strike_oi_backtest(days=5, target_pct=0.50, stop_pct=0.30, cost_bps=10.0):
    """Last-N trading-day event backtest for the new same-strike OI confirmation.

    Entry is the nearest futures snapshot at/after the first qualifying pair signal.
    Target/stop are evaluated chronologically on stored 3-minute futures snapshots.
    Cost-adjusted return subtracts round-trip cost_bps from the raw outcome.
    """
    if not option_snapshot_table_exists(): return pd.DataFrame()
    sql="""
    WITH dates AS (
      SELECT DISTINCT trading_date FROM public.money_flow_option_snapshots
      ORDER BY trading_date DESC LIMIT %s
    ), sig AS (
      SELECT DISTINCT ON (trading_date,symbol,same_strike_signal)
        trading_date,symbol,ts,same_strike_signal,strike,own_oi_change_pct_0920,
        paired_oi_change_pct_0920,same_strike_persistence,same_strike_contribution
      FROM public.money_flow_option_snapshots
      WHERE trading_date IN (SELECT trading_date FROM dates)
        AND same_strike_signal IN ('BULLISH','BEARISH')
      ORDER BY trading_date,symbol,same_strike_signal,ts,same_strike_contribution DESC
    ), ent AS (
      SELECT s.*, e.ts entry_ts,e.future entry_price
      FROM sig s
      LEFT JOIN LATERAL (
        SELECT ts,future FROM public.stock_engine_snapshots x
        WHERE x.symbol=s.symbol AND x.ts>=s.ts
          AND (x.ts AT TIME ZONE 'Asia/Kolkata')::date=s.trading_date
        ORDER BY x.ts LIMIT 1
      ) e ON TRUE
    ), outcomes AS (
      SELECT e.*, o.outcome_ts,o.outcome_price,o.raw_return_pct,o.outcome
      FROM ent e
      LEFT JOIN LATERAL (
        SELECT x.ts outcome_ts,x.future outcome_price,
          CASE WHEN e.same_strike_signal='BULLISH' THEN (x.future/e.entry_price-1)*100
               ELSE (e.entry_price/x.future-1)*100 END raw_return_pct,
          CASE
            WHEN e.same_strike_signal='BULLISH' AND (x.future/e.entry_price-1)*100 >= %s THEN 'TARGET'
            WHEN e.same_strike_signal='BULLISH' AND (x.future/e.entry_price-1)*100 <= -%s THEN 'STOP'
            WHEN e.same_strike_signal='BEARISH' AND (e.entry_price/x.future-1)*100 >= %s THEN 'TARGET'
            WHEN e.same_strike_signal='BEARISH' AND (e.entry_price/x.future-1)*100 <= -%s THEN 'STOP'
          END outcome
        FROM public.stock_engine_snapshots x
        WHERE x.symbol=e.symbol AND x.ts>e.entry_ts
          AND (x.ts AT TIME ZONE 'Asia/Kolkata')::date=e.trading_date
          AND (
            (e.same_strike_signal='BULLISH' AND ((x.future/e.entry_price-1)*100 >= %s OR (x.future/e.entry_price-1)*100 <= -%s)) OR
            (e.same_strike_signal='BEARISH' AND ((e.entry_price/x.future-1)*100 >= %s OR (e.entry_price/x.future-1)*100 <= -%s))
          )
        ORDER BY x.ts LIMIT 1
      ) o ON TRUE
    )
    SELECT *,
      CASE WHEN outcome='TARGET' THEN %s WHEN outcome='STOP' THEN -%s ELSE raw_return_pct END AS modeled_return_pct,
      CASE WHEN outcome='TARGET' THEN %s-%s/100.0 WHEN outcome='STOP' THEN -%s-%s/100.0
           ELSE raw_return_pct-%s/100.0 END AS cost_adjusted_return_pct,
      CASE WHEN outcome='TARGET' THEN TRUE WHEN outcome='STOP' THEN FALSE ELSE NULL END AS target_before_stop
    FROM outcomes ORDER BY trading_date DESC,entry_ts,symbol
    """
    params=(days,target_pct,stop_pct,target_pct,stop_pct,target_pct,stop_pct,target_pct,stop_pct,
            target_pct,stop_pct,target_pct,cost_bps,stop_pct,cost_bps,cost_bps)
    return query_df(sql,params)

st.set_page_config(
    page_title=f"Top {MONEY_FLOW_TOP_N} Money Flow",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Mobile-friendly CSS
st.markdown(
    """
    <style>
      .block-container {padding-top: 0.8rem; padding-bottom: 2rem; max-width: 1200px;}
      div[data-testid="stMetric"] {
          border: 1px solid rgba(128,128,128,.25);
          border-radius: 12px;
          padding: 10px 12px;
      }
      .small-note {font-size: 0.82rem; opacity: 0.72;}
      .stock-card {
          border: 1px solid rgba(128,128,128,.25);
          border-radius: 14px;
          padding: 12px 14px;
          margin-bottom: 10px;
      }
      @media (max-width: 700px) {
          .block-container {padding-left: .6rem; padding-right: .6rem;}
          h1 {font-size: 1.45rem !important;}
          h2 {font-size: 1.15rem !important;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)

if not DATABASE_URL:
    st.error("NEON_DATABASE_URL is missing. Add it to Streamlit Secrets in the cloud, or to your local .env file.")
    st.stop()


# ============================================================
# DATABASE
# ============================================================

def query_df(sql, params=None):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
    return pd.DataFrame(rows)


def ensure_early_detector_snapshots_table():
    """Create the persistent v2.2 score-history table when it is absent."""
    sql = """
    CREATE TABLE IF NOT EXISTS public.early_detector_snapshots (
        id BIGSERIAL PRIMARY KEY,
        trading_date DATE NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        money_flow_rank INTEGER,
        symbol TEXT NOT NULL,

        state TEXT NOT NULL,
        conviction TEXT NOT NULL,
        score NUMERIC NOT NULL,
        direction TEXT NOT NULL,
        bull_score NUMERIC NOT NULL,
        bear_score NUMERIC NOT NULL,

        option_bull_score INTEGER NOT NULL DEFAULT 0,
        option_bear_score INTEGER NOT NULL DEFAULT 0,
        option_persistence INTEGER NOT NULL DEFAULT 0,
        total_qty_imbalance NUMERIC,
        imbalance_persistence INTEGER NOT NULL DEFAULT 0,
        trade_delta_pct NUMERIC,
        classified_trade_count INTEGER NOT NULL DEFAULT 0,
        classified_qty BIGINT NOT NULL DEFAULT 0,
        delta_persistence INTEGER NOT NULL DEFAULT 0,
        aggression_quality TEXT NOT NULL,
        order_flow_agreement TEXT NOT NULL,
        price_change_3m_pct NUMERIC,
        oi_change_3m_pct NUMERIC,
        session_price_pct NUMERIC,
        cumulative_oi_pct NUMERIC,
        normalized_move_units NUMERIC,
        option_oi_iv_confirmation NUMERIC,
        same_strike_oi_signal TEXT,
        same_strike_oi_points NUMERIC NOT NULL DEFAULT 0,
        same_strike_oi_persistence INTEGER NOT NULL DEFAULT 0,
        same_strike_oi_strike NUMERIC,
        same_strike_own_change_pct NUMERIC,
        same_strike_opposite_change_pct NUMERIC,
        relative_strength_pct NUMERIC,
        volume_percentile NUMERIC,
        extension_status TEXT,
        absorption_flag TEXT,
        minutes_2_to_4 NUMERIC,
        option_source_ts TIMESTAMPTZ,
        aggression_source_ts TIMESTAMPTZ,
        avwap_bull_points NUMERIC,
        avwap_bear_points NUMERIC,
        avwap_state TEXT,
        avwap_high NUMERIC,
        avwap_low NUMERIC,
        hourly_avwap_high NUMERIC,
        hourly_avwap_low NUMERIC,
        avwap_source_ts TIMESTAMPTZ,

        price_persistence_points NUMERIC NOT NULL DEFAULT 0,
        oi_confirmation_points NUMERIC NOT NULL DEFAULT 0,
        futures_state TEXT,
        futures_state_persistence INTEGER NOT NULL DEFAULT 0,
        futures_state_points NUMERIC NOT NULL DEFAULT 0,
        total_flow_3m_cr NUMERIC,
        money_flow_acceleration_3m_cr NUMERIC,
        money_flow_acceleration_x NUMERIC,
        money_flow_points NUMERIC NOT NULL DEFAULT 0,
        pcr_trend_9m NUMERIC,
        pcr_trend_points NUMERIC NOT NULL DEFAULT 0,
        aggression_points NUMERIC NOT NULL DEFAULT 0,
        imbalance_points NUMERIC NOT NULL DEFAULT 0,

        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (trading_date, ts, symbol)
    );

    CREATE INDEX IF NOT EXISTS idx_early_detector_date_symbol_ts
        ON public.early_detector_snapshots (trading_date, symbol, ts);

    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS session_price_pct NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS cumulative_oi_pct NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS normalized_move_units NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS option_oi_iv_confirmation NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_oi_signal TEXT;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_oi_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_oi_persistence INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_oi_strike NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_own_change_pct NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS same_strike_opposite_change_pct NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS relative_strength_pct NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS volume_percentile NUMERIC;
    ALTER TABLE public.early_detector_snapshots
        ADD COLUMN IF NOT EXISTS extension_status TEXT;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_bull_points NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_bear_points NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_state TEXT;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_high NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_low NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS hourly_avwap_high NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS hourly_avwap_low NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS avwap_source_ts TIMESTAMPTZ;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS price_persistence_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS oi_confirmation_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS futures_state TEXT;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS futures_state_persistence INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS futures_state_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS total_flow_3m_cr NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS money_flow_acceleration_3m_cr NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS money_flow_acceleration_x NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS money_flow_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS pcr_trend_9m NUMERIC;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS pcr_trend_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS aggression_points NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE public.early_detector_snapshots ADD COLUMN IF NOT EXISTS imbalance_points NUMERIC NOT NULL DEFAULT 0;
    """
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def persist_early_detector_snapshots(history_df):
    """Idempotently persist reconstructed score history for the current universe."""
    if history_df is None or history_df.empty:
        return 0

    ensure_early_detector_snapshots_table()
    columns = [
        "trading_date", "ts", "money_flow_rank", "symbol",
        "state", "conviction", "score", "direction", "bull_score", "bear_score",
        "option_bull_score", "option_bear_score", "option_persistence",
        "total_qty_imbalance", "imbalance_persistence", "trade_delta_pct",
        "classified_trade_count", "classified_qty", "delta_persistence",
        "aggression_quality", "order_flow_agreement", "price_change_3m_pct",
        "oi_change_3m_pct", "session_price_pct", "cumulative_oi_pct",
        "normalized_move_units", "option_oi_iv_confirmation",
        "same_strike_oi_signal", "same_strike_oi_points", "same_strike_oi_persistence",
        "same_strike_oi_strike", "same_strike_own_change_pct", "same_strike_opposite_change_pct",
        "relative_strength_pct", "volume_percentile", "extension_status",
        "absorption_flag", "minutes_2_to_4",
        "option_source_ts", "aggression_source_ts",
        "avwap_bull_points", "avwap_bear_points", "avwap_state",
        "avwap_high", "avwap_low", "hourly_avwap_high", "hourly_avwap_low",
        "avwap_source_ts",
        "price_persistence_points", "oi_confirmation_points",
        "futures_state", "futures_state_persistence", "futures_state_points",
        "total_flow_3m_cr", "money_flow_acceleration_3m_cr", "money_flow_acceleration_x",
        "money_flow_points", "pcr_trend_9m", "pcr_trend_points",
        "aggression_points", "imbalance_points"
    ]

    def db_value(value):
        if value is None or pd.isna(value):
            return None
        if hasattr(value, "item"):
            return value.item()
        return value

    rows = [tuple(db_value(row.get(col)) for col in columns)
            for _, row in history_df.iterrows()]
    placeholders = ", ".join(["%s"] * len(columns))
    update_columns = [c for c in columns if c not in ("trading_date", "ts", "symbol")]
    assignments = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_columns)
    sql = f"""
        INSERT INTO public.early_detector_snapshots ({', '.join(columns)})
        VALUES ({placeholders})
        ON CONFLICT (trading_date, ts, symbol) DO UPDATE SET
            {assignments}, updated_at=NOW()
    """
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def load_universe():
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    )
    SELECT
        trading_date,
        freeze_ts,
        rank,
        symbol,
        futures_value_cr,
        options_value_cr,
        total_money_flow_cr,
        future_volume,
        future_oi,
        future_price,
        spot_price,
        selection_method
    FROM public.money_flow_universe
    WHERE trading_date = (SELECT trading_date FROM latest_date)
    ORDER BY rank;
    """
    return query_df(sql)


def load_avwap_history():
    """Load first-hour AVWAP structure for only the latest frozen universe."""
    sql = """
    WITH d AS (
        SELECT MAX(trading_date) AS trading_date FROM public.money_flow_universe
    ), u AS (
        SELECT trading_date, symbol FROM public.money_flow_universe
        WHERE trading_date=(SELECT trading_date FROM d)
    )
    SELECT a.symbol, a.candle_end AS ts, a.avwap_high, a.avwap_low,
           a.hourly_avwap_high, a.hourly_avwap_low,
           a.high_cross, a.low_cross
    FROM public.avwap_futures_3m a
    JOIN u ON u.trading_date=a.trading_date AND u.symbol=a.symbol
    ORDER BY a.symbol, a.candle_end
    """
    try:
        out = query_df(sql)
    except Exception:
        return pd.DataFrame()
    if not out.empty:
        out["ts"] = pd.to_datetime(out["ts"], errors="coerce", utc=True)
    return out


def load_latest_snapshots():
    sql = """
    WITH latest_date AS (
        SELECT MAX((ts AT TIME ZONE 'Asia/Kolkata')::date) AS trading_date
        FROM public.stock_engine_snapshots
    ),
    ranked AS (
        SELECT
            s.*,
            ROW_NUMBER() OVER (
                PARTITION BY s.symbol
                ORDER BY s.ts DESC
            ) AS rn
        FROM public.stock_engine_snapshots s
        WHERE (s.ts AT TIME ZONE 'Asia/Kolkata')::date =
              (SELECT trading_date FROM latest_date)
    )
    SELECT *
    FROM ranked
    WHERE rn = 1
    ORDER BY money_flow_rank NULLS LAST, symbol;
    """
    return query_df(sql)


def load_symbol_history(symbol, limit=40):
    sql = """
    SELECT *
    FROM public.stock_engine_snapshots
    WHERE symbol = %s
    ORDER BY ts DESC
    LIMIT %s;
    """
    df = query_df(sql, (symbol, limit))
    if not df.empty:
        df = df.sort_values("ts")
    return df



def load_oi_milestones():
    """First +2%, +4% and +8% futures-OI crossings for the latest trading day."""
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    base AS (
        SELECT
            (s.ts AT TIME ZONE 'Asia/Kolkata')::date AS trading_date,
            s.symbol, s.ts, s.future, s.future_oi, s.future_oi_change_pct_t0,
            u.rank AS money_flow_rank, u.future_price AS future_930,
            u.future_oi AS oi_930, u.futures_value_cr AS futures_value_930_cr
        FROM public.stock_engine_snapshots s
        JOIN public.money_flow_universe u
          ON u.symbol = s.symbol
         AND u.trading_date = (s.ts AT TIME ZONE 'Asia/Kolkata')::date
        WHERE u.trading_date = (SELECT trading_date FROM latest_date)
    ),
    c2 AS (
        SELECT DISTINCT ON (symbol) symbol, ts AS time_2pct,
               future_oi_change_pct_t0 AS oi_2pct, future AS future_2pct, future_oi AS future_oi_2pct
        FROM base WHERE future_oi_change_pct_t0 >= 2 ORDER BY symbol, ts
    ),
    c4 AS (
        SELECT DISTINCT ON (symbol) symbol, ts AS time_4pct,
               future_oi_change_pct_t0 AS oi_4pct, future AS future_4pct, future_oi AS future_oi_4pct
        FROM base WHERE future_oi_change_pct_t0 >= 4 ORDER BY symbol, ts
    ),
    c8 AS (
        SELECT DISTINCT ON (symbol) symbol, ts AS time_8pct,
               future_oi_change_pct_t0 AS oi_8pct, future AS future_8pct, future_oi AS future_oi_8pct
        FROM base WHERE future_oi_change_pct_t0 >= 8 ORDER BY symbol, ts
    ),
    symbols AS (
        SELECT DISTINCT symbol, money_flow_rank, future_930, oi_930, futures_value_930_cr FROM base
    )
    SELECT s.*,
           c2.time_2pct, c2.oi_2pct, c2.future_2pct, c2.future_oi_2pct,
           c4.time_4pct, c4.oi_4pct, c4.future_4pct, c4.future_oi_4pct,
           c8.time_8pct, c8.oi_8pct, c8.future_8pct, c8.future_oi_8pct,
           CASE WHEN c2.time_2pct IS NOT NULL AND c4.time_4pct IS NOT NULL
                THEN EXTRACT(EPOCH FROM (c4.time_4pct-c2.time_2pct))/60.0 END AS minutes_2_to_4,
           CASE WHEN c4.time_4pct IS NOT NULL AND c8.time_8pct IS NOT NULL
                THEN EXTRACT(EPOCH FROM (c8.time_8pct-c4.time_4pct))/60.0 END AS minutes_4_to_8
    FROM symbols s
    LEFT JOIN c2 USING(symbol)
    LEFT JOIN c4 USING(symbol)
    LEFT JOIN c8 USING(symbol)
    ORDER BY money_flow_rank;
    """
    return query_df(sql)



OI_SPURT_PCT = 0.50
OI_STRONG_SPURT_PCT = 1.00


def load_oi_spurt_stats():
    """Live + intraday OI-spurt statistics for the latest trading day."""
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    base AS (
        SELECT
            s.symbol,
            s.ts,
            s.future,
            s.future_oi,
            s.future_oi_change_3m,
            u.future_price AS future_930,
            CASE
                WHEN (s.future_oi - s.future_oi_change_3m) <> 0
                THEN (s.future_oi_change_3m::numeric /
                      NULLIF((s.future_oi - s.future_oi_change_3m),0)) * 100
            END AS oi_spurt_3m_pct,
            ((s.future / NULLIF(u.future_price,0)) - 1) * 100 AS future_vs_930_pct,
            ROW_NUMBER() OVER (PARTITION BY s.symbol ORDER BY s.ts DESC) AS rn_desc
        FROM public.stock_engine_snapshots s
        JOIN public.money_flow_universe u
          ON u.symbol = s.symbol
         AND u.trading_date = (s.ts AT TIME ZONE 'Asia/Kolkata')::date
        WHERE u.trading_date = (SELECT trading_date FROM latest_date)
    ),
    agg AS (
        SELECT
            symbol,
            COUNT(*) FILTER (WHERE oi_spurt_3m_pct >= %s) AS spurt_count,
            COUNT(*) FILTER (WHERE oi_spurt_3m_pct >= %s) AS strong_spurt_count,
            MAX(oi_spurt_3m_pct) AS max_spurt_pct,
            MIN(ts) FILTER (WHERE oi_spurt_3m_pct >= %s) AS first_spurt_time,
            MIN(ts) FILTER (
                WHERE oi_spurt_3m_pct >= %s AND future_vs_930_pct > 0
            ) AS first_long_spurt_time,
            MIN(ts) FILTER (
                WHERE oi_spurt_3m_pct >= %s AND future_vs_930_pct < 0
            ) AS first_short_spurt_time
        FROM base
        GROUP BY symbol
    ),
    latest AS (
        SELECT
            symbol,
            ts AS spurt_snapshot_time,
            oi_spurt_3m_pct AS current_spurt_pct,
            future_vs_930_pct AS spurt_future_vs_930_pct,
            CASE
              WHEN oi_spurt_3m_pct >= %s AND future_vs_930_pct > 0 THEN 'STRONG LONG SPURT'
              WHEN oi_spurt_3m_pct >= %s AND future_vs_930_pct < 0 THEN 'STRONG SHORT SPURT'
              WHEN oi_spurt_3m_pct >= %s AND future_vs_930_pct > 0 THEN 'LONG SPURT'
              WHEN oi_spurt_3m_pct >= %s AND future_vs_930_pct < 0 THEN 'SHORT SPURT'
              ELSE 'NO CURRENT SPURT'
            END AS current_spurt_state
        FROM base
        WHERE rn_desc = 1
    )
    SELECT
        a.symbol,
        COALESCE(a.spurt_count,0) AS spurt_count,
        COALESCE(a.strong_spurt_count,0) AS strong_spurt_count,
        a.max_spurt_pct,
        a.first_spurt_time,
        a.first_long_spurt_time,
        a.first_short_spurt_time,
        l.spurt_snapshot_time,
        l.current_spurt_pct,
        l.spurt_future_vs_930_pct,
        l.current_spurt_state
    FROM agg a
    JOIN latest l USING(symbol);
    """
    return query_df(sql, (
        OI_SPURT_PCT, OI_STRONG_SPURT_PCT,
        OI_SPURT_PCT, OI_SPURT_PCT, OI_SPURT_PCT,
        OI_STRONG_SPURT_PCT, OI_STRONG_SPURT_PCT,
        OI_SPURT_PCT, OI_SPURT_PCT,
    ))



OPTION_LEAD_ACCEL_X = 2.0
OPTION_LEAD_MIN_FRESH_CR = 0.50


def load_option_activity_stats():
    """Aggregate option fresh-value acceleration from stock-engine snapshots.

    Research-only lead flag: current 3m option fresh value >= ₹0.50Cr and >=2x
    the average of the previous five 3-minute snapshots. This does not alter
    the core Early Detector signal rules.
    """
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    base AS (
        SELECT
            s.symbol,
            s.ts,
            COALESCE(s.call_fresh_value_cr,0)::numeric AS call_fresh_cr,
            COALESCE(s.put_fresh_value_cr,0)::numeric AS put_fresh_cr,
            (COALESCE(s.call_fresh_value_cr,0) + COALESCE(s.put_fresh_value_cr,0))::numeric AS option_fresh_3m_cr,
            AVG((COALESCE(s.call_fresh_value_cr,0) + COALESCE(s.put_fresh_value_cr,0))::numeric)
              OVER (PARTITION BY s.symbol ORDER BY s.ts ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS prior_15m_avg_per_3m_cr,
            COUNT(*) OVER (PARTITION BY s.symbol ORDER BY s.ts ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS prior_rows,
            SUM(COALESCE(s.call_fresh_value_cr,0)::numeric)
              OVER (PARTITION BY s.symbol ORDER BY s.ts ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS call_fresh_15m_cr,
            SUM(COALESCE(s.put_fresh_value_cr,0)::numeric)
              OVER (PARTITION BY s.symbol ORDER BY s.ts ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS put_fresh_15m_cr,
            ROW_NUMBER() OVER (PARTITION BY s.symbol ORDER BY s.ts DESC) AS rn_desc
        FROM public.stock_engine_snapshots s
        WHERE (s.ts AT TIME ZONE 'Asia/Kolkata')::date = (SELECT trading_date FROM latest_date)
    ),
    calc AS (
        SELECT *,
            CASE WHEN prior_rows = 5 AND prior_15m_avg_per_3m_cr > 0
                 THEN option_fresh_3m_cr / prior_15m_avg_per_3m_cr END AS option_acceleration_x,
            CASE WHEN prior_rows = 5 AND prior_15m_avg_per_3m_cr > 0
                       AND option_fresh_3m_cr >= %s
                       AND (option_fresh_3m_cr / prior_15m_avg_per_3m_cr) >= %s
                 THEN TRUE ELSE FALSE END AS option_lead_flag
        FROM base
    ),
    agg AS (
        SELECT
            symbol,
            MAX(option_acceleration_x) AS max_option_acceleration_x,
            MIN(ts) FILTER (WHERE option_lead_flag) AS first_option_lead_time,
            COUNT(*) FILTER (WHERE option_lead_flag) AS option_lead_count
        FROM calc
        GROUP BY symbol
    ),
    latest AS (
        SELECT
            symbol,
            ts AS option_activity_ts,
            option_fresh_3m_cr AS current_option_fresh_3m_cr,
            prior_15m_avg_per_3m_cr,
            option_acceleration_x AS current_option_acceleration_x,
            call_fresh_15m_cr,
            put_fresh_15m_cr,
            option_lead_flag AS current_option_lead_flag
        FROM calc
        WHERE rn_desc = 1
    )
    SELECT l.*, a.max_option_acceleration_x, a.first_option_lead_time, a.option_lead_count
    FROM latest l
    LEFT JOIN agg a USING(symbol);
    """
    return query_df(sql, (OPTION_LEAD_MIN_FRESH_CR, OPTION_LEAD_ACCEL_X))


def option_snapshot_table_exists():
    sql = """
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name='money_flow_option_snapshots'
    ) AS exists;
    """
    df = query_df(sql)
    return bool(not df.empty and df.iloc[0].get("exists"))


def load_latest_option_contracts():
    """Latest frozen six-option snapshot per stock, with a dominant contract.

    Dominance is research-only and ranked by absolute premium-weighted OI change:
    |ΔOI_3m × LTP|. This is an approximate intensity score, not literal cash flow.
    """
    if not option_snapshot_table_exists():
        return pd.DataFrame()
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    latest_ts AS (
        SELECT symbol, MAX(ts) AS ts
        FROM public.money_flow_option_snapshots
        WHERE trading_date = (SELECT trading_date FROM latest_date)
        GROUP BY symbol
    ),
    six AS (
        SELECT
            o.*,
            ABS(COALESCE(o.oi_change_3m,0)::numeric * COALESCE(o.ltp,0)::numeric) AS premium_oi_intensity,
            ROW_NUMBER() OVER (
                PARTITION BY o.symbol
                ORDER BY ABS(COALESCE(o.oi_change_3m,0)::numeric * COALESCE(o.ltp,0)::numeric) DESC,
                         ABS(COALESCE(o.oi_change_3m,0)) DESC,
                         o.option_type, o.wing_no
            ) AS intensity_rank
        FROM public.money_flow_option_snapshots o
        JOIN latest_ts t ON t.symbol=o.symbol AND t.ts=o.ts
        WHERE o.trading_date = (SELECT trading_date FROM latest_date)
    )
    SELECT
        symbol,
        ts AS option_snapshot_ts,
        option_type AS dominant_option_type,
        wing_no AS dominant_wing_no,
        strike AS dominant_strike,
        ltp AS dominant_ltp,
        opening_ltp AS dominant_opening_ltp,
        price_multiple AS dominant_price_multiple,
        doubled AS dominant_doubled,
        oi AS dominant_oi,
        oi_change_3m AS dominant_oi_change_3m,
        iv AS dominant_iv,
        delta AS dominant_delta,
        premium_oi_intensity AS dominant_premium_oi_intensity
    FROM six
    WHERE intensity_rank=1;
    """
    return query_df(sql)



def load_dominant_option_at_first_spurt():
    """Exact frozen contract with the largest |ΔOI × LTP| at the first >=0.50% futures OI spurt."""
    if not option_snapshot_table_exists():
        return pd.DataFrame()
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date FROM public.money_flow_universe
    ),
    spurts AS (
        SELECT
            s.symbol, s.ts,
            CASE WHEN (s.future_oi-s.future_oi_change_3m) <> 0
                 THEN s.future_oi_change_3m::numeric / NULLIF((s.future_oi-s.future_oi_change_3m),0) * 100 END AS oi_spurt_pct
        FROM public.stock_engine_snapshots s
        WHERE (s.ts AT TIME ZONE 'Asia/Kolkata')::date=(SELECT trading_date FROM latest_date)
    ),
    first_spurt AS (
        SELECT symbol, MIN(ts) AS first_spurt_ts
        FROM spurts
        WHERE oi_spurt_pct >= %s
        GROUP BY symbol
    ),
    ranked AS (
        SELECT
            o.symbol, f.first_spurt_ts,
            o.option_type, o.wing_no, o.strike, o.ltp, o.opening_ltp, o.price_multiple,
            o.oi_change_3m, o.iv, o.doubled,
            ABS(COALESCE(o.oi_change_3m,0)::numeric * COALESCE(o.ltp,0)::numeric) AS intensity,
            ROW_NUMBER() OVER (
                PARTITION BY o.symbol
                ORDER BY ABS(COALESCE(o.oi_change_3m,0)::numeric * COALESCE(o.ltp,0)::numeric) DESC,
                         ABS(COALESCE(o.oi_change_3m,0)) DESC,
                         o.option_type, o.wing_no
            ) AS rn
        FROM public.money_flow_option_snapshots o
        JOIN first_spurt f ON f.symbol=o.symbol AND f.first_spurt_ts=o.ts
        WHERE o.trading_date=(SELECT trading_date FROM latest_date)
    )
    SELECT
        symbol,
        first_spurt_ts AS option_spurt_time,
        option_type AS spurt_dominant_option_type,
        wing_no AS spurt_dominant_wing_no,
        strike AS spurt_dominant_strike,
        ltp AS spurt_dominant_ltp,
        opening_ltp AS spurt_dominant_opening_ltp,
        price_multiple AS spurt_dominant_price_multiple,
        oi_change_3m AS spurt_dominant_oi_change_3m,
        iv AS spurt_dominant_iv,
        doubled AS spurt_dominant_doubled
    FROM ranked
    WHERE rn=1;
    """
    return query_df(sql, (OI_SPURT_PCT,))


def load_first_option_doubles():
    """First frozen option to reach 2x opening premium for each stock on latest day."""
    if not option_snapshot_table_exists():
        return pd.DataFrame()
    sql = """
    WITH latest_date AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    firsts AS (
        SELECT DISTINCT ON (symbol)
            symbol, ts, option_type, wing_no, strike, opening_ltp, ltp, price_multiple
        FROM public.money_flow_option_snapshots
        WHERE trading_date=(SELECT trading_date FROM latest_date)
          AND doubled=TRUE
        ORDER BY symbol, ts, price_multiple DESC
    )
    SELECT
        symbol,
        ts AS first_option_2x_time,
        option_type AS first_2x_option_type,
        wing_no AS first_2x_wing_no,
        strike AS first_2x_strike,
        opening_ltp AS first_2x_opening_ltp,
        ltp AS first_2x_ltp,
        price_multiple AS first_2x_multiple
    FROM firsts;
    """
    return query_df(sql)


def load_symbol_option_history(symbol, limit=120):
    if not option_snapshot_table_exists():
        return pd.DataFrame()
    sql = """
    SELECT *
    FROM public.money_flow_option_snapshots
    WHERE symbol=%s
    ORDER BY ts DESC, option_type, wing_no
    LIMIT %s;
    """
    return query_df(sql, (symbol, limit))

def load_early_detector_history(days=31):
    """Reconstruct v1.0 acceleration events from existing Neon snapshots."""
    sql = """
    WITH base AS (
        SELECT
            (s.ts AT TIME ZONE 'Asia/Kolkata')::date AS trading_date,
            s.symbol, s.ts, s.future, s.future_oi, s.future_oi_change_pct_t0, s.zone_state,
            u.rank AS money_flow_rank, u.future_price AS future_930,
            u.future_oi AS oi_930, u.futures_value_cr AS futures_value_930_cr
        FROM public.stock_engine_snapshots s
        JOIN public.money_flow_universe u
          ON u.symbol = s.symbol
         AND u.trading_date = (s.ts AT TIME ZONE 'Asia/Kolkata')::date
        WHERE (s.ts AT TIME ZONE 'Asia/Kolkata')::date >=
              ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - (%s::int))
    ),
    c2 AS (
        SELECT DISTINCT ON (trading_date, symbol) trading_date, symbol, ts AS time_2pct,
               future_oi_change_pct_t0 AS oi_2pct
        FROM base WHERE future_oi_change_pct_t0 >= 2
        ORDER BY trading_date, symbol, ts
    ),
    c4 AS (
        SELECT DISTINCT ON (trading_date, symbol) trading_date, symbol, ts AS time_4pct,
               future_oi_change_pct_t0 AS oi_4pct, future AS future_4pct, future_oi AS future_oi_4pct,
               zone_state AS zone_4pct, money_flow_rank, future_930, oi_930, futures_value_930_cr
        FROM base WHERE future_oi_change_pct_t0 >= 4
        ORDER BY trading_date, symbol, ts
    ),
    c8 AS (
        SELECT DISTINCT ON (trading_date, symbol) trading_date, symbol, ts AS time_8pct,
               future_oi_change_pct_t0 AS oi_8pct
        FROM base WHERE future_oi_change_pct_t0 >= 8
        ORDER BY trading_date, symbol, ts
    )
    SELECT c4.trading_date, c4.money_flow_rank, c4.symbol,
           c2.time_2pct, c4.time_4pct, c8.time_8pct,
           ROUND((EXTRACT(EPOCH FROM (c4.time_4pct-c2.time_2pct))/60.0)::numeric,0) AS minutes_2_to_4,
           ROUND((((c4.future_4pct/NULLIF(c4.future_930,0))-1)*100)::numeric,2) AS future_move_4pct,
           ROUND((c4.futures_value_930_cr * (((c4.future_4pct*c4.future_oi_4pct)/
                 NULLIF((c4.future_930*c4.oi_930),0))-1))::numeric,2) AS exposure_change_4pct_cr,
           c4.oi_4pct, c4.zone_4pct,
           CASE
             WHEN EXTRACT(EPOCH FROM (c4.time_4pct-c2.time_2pct))/60.0 <= 30 AND c4.future_4pct > c4.future_930 THEN 'ACCELERATING LONG'
             WHEN EXTRACT(EPOCH FROM (c4.time_4pct-c2.time_2pct))/60.0 <= 30 AND c4.future_4pct < c4.future_930 THEN 'ACCELERATING SHORT'
             ELSE 'NO ACCELERATION'
           END AS detector_state
    FROM c4
    JOIN c2 USING(trading_date, symbol)
    LEFT JOIN c8 USING(trading_date, symbol)
    ORDER BY trading_date DESC, money_flow_rank;
    """
    return query_df(sql, (days,))


# ============================================================
# MASTER DASHBOARD SCORING
# ============================================================

def pct_rank_abs(series):
    s = pd.to_numeric(series, errors="coerce").abs()
    if s.notna().sum() <= 1:
        return pd.Series([0.0] * len(s), index=s.index)
    return s.rank(pct=True, method="average").fillna(0.0) * 100.0


def build_master_score(df):
    """
    Cross-sectional frozen-universe attention score.
    This does NOT claim historical extremeness.
    It ranks current 3-minute activity against the other selected stocks.
    """
    if df.empty:
        return df

    out = df.copy()

    activity_cols = [
        "future_oi_change_3m",
        "call_oi_change_3m",
        "put_oi_change_3m",
        "pcr_change_3m",
        "pcr_acceleration",
        "call_iv_change_3m",
        "put_iv_change_3m",
        "call_iv_acceleration",
        "put_iv_acceleration",
        "call_fresh_value_cr",
        "put_fresh_value_cr",
        "atm_call_oi_change_3m",
        "atm_put_oi_change_3m",
    ]

    rank_parts = []
    for col in activity_cols:
        if col in out.columns:
            rank_parts.append(pct_rank_abs(out[col]))

    if rank_parts:
        score_matrix = pd.concat(rank_parts, axis=1)
        out["master_attention_score"] = score_matrix.mean(axis=1)
    else:
        out["master_attention_score"] = 0.0

    def status(score):
        if score >= 80:
            return "EXTREME"
        if score >= 65:
            return "STRONG"
        if score >= 50:
            return "NOTABLE"
        return "NORMAL"

    out["master_status"] = out["master_attention_score"].apply(status)

    # Direction is a simple confluence tag, not a trade signal.
    def direction(row):
        bullish = 0
        bearish = 0

        f_oi = float(row.get("future_oi_change_3m") or 0)
        call_oi = float(row.get("call_oi_change_3m") or 0)
        put_oi = float(row.get("put_oi_change_3m") or 0)
        pcr_d = float(row.get("pcr_change_3m") or 0)
        atm_call = float(row.get("atm_call_oi_change_3m") or 0)
        atm_put = float(row.get("atm_put_oi_change_3m") or 0)

        if put_oi > call_oi:
            bullish += 1
        elif call_oi > put_oi:
            bearish += 1

        if pcr_d > 0:
            bullish += 1
        elif pcr_d < 0:
            bearish += 1

        if atm_call < 0:
            bullish += 1
        if atm_put < 0:
            bearish += 1

        # Futures OI is confirmation only; direction requires spot change,
        # which is not yet stored directly as a 3m % field.
        if bullish >= bearish + 2:
            return "Bullish confluence"
        if bearish >= bullish + 2:
            return "Bearish confluence"
        return "Mixed"

    out["confluence"] = out.apply(direction, axis=1)
    return out




# ============================================================
# EARLY DETECTOR v1.3 — OI SPURT + OPTION LEAD LAYER
# ============================================================

ACCELERATION_MINUTES = 30


def build_early_detector(df):
    """v1.3 research rules: option-lead observation + OI Spurt warning + frozen 2→4 acceleration framework."""
    if df.empty:
        return df
    out = df.copy()
    numeric_cols = [
        "future_oi_change_pct_t0", "future_oi_change_3m", "future", "future_oi",
        "future_930", "oi_930", "futures_value_930_cr", "spot", "spot_930",
        "minutes_2_to_4", "minutes_4_to_8", "future_4pct", "future_oi_4pct",
        "current_spurt_pct", "max_spurt_pct", "spurt_count", "strong_spurt_count",
        "current_option_fresh_3m_cr", "prior_15m_avg_per_3m_cr", "current_option_acceleration_x",
        "max_option_acceleration_x", "option_lead_count", "dominant_price_multiple",
        "dominant_oi_change_3m", "dominant_iv"
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            # Early in the session milestone queries can return no 2%/4%/8%
            # rows, so the merge does not create those optional columns yet.
            # Keep the detector live and represent unreached milestones as NaN.
            out[col] = pd.NA

    out["price_change_from_930_pct"] = ((out["spot"] / out["spot_930"]) - 1.0) * 100.0
    out["future_price_change_from_930_pct"] = ((out["future"] / out["future_930"]) - 1.0) * 100.0
    out["futures_exposure_change_cr"] = out["futures_value_930_cr"] * (
        ((out["future"] * out["future_oi"]) / (out["future_930"] * out["oi_930"])) - 1.0
    )
    out["future_move_at_4pct"] = ((out["future_4pct"] / out["future_930"]) - 1.0) * 100.0
    out["exposure_at_4pct_cr"] = out["futures_value_930_cr"] * (
        ((out["future_4pct"] * out["future_oi_4pct"]) / (out["future_930"] * out["oi_930"])) - 1.0
    )

    def oi_stage(v):
        if pd.isna(v): return "QUIET"
        if v >= 8: return "STRONG"
        if v >= 4: return "BUILDING"
        if v >= 2: return "WATCH"
        return "QUIET"

    bearish_zones = {"BELOW STRONG DEMAND", "WEAK DEMAND BROKEN", "STRONG DEMAND"}
    bullish_zones = {"ABOVE DPOC", "WEAK SUPPLY", "WEAK SUPPLY BROKEN", "STRONG SUPPLY"}

    def acceleration_state(row):
        mins = row.get("minutes_2_to_4")
        f4 = row.get("future_move_at_4pct")
        if pd.notna(mins) and mins <= ACCELERATION_MINUTES and pd.notna(f4):
            if f4 > 0: return "ACCELERATING LONG"
            if f4 < 0: return "ACCELERATING SHORT"
        return "NO ACCELERATION"

    def state(row):
        oi = float(row.get("future_oi_change_pct_t0") or 0)
        fut_move = float(row.get("future_price_change_from_930_pct") or 0)
        zone = str(row.get("zone_state") or "").upper()
        accel = row.get("acceleration_state")
        spurt = str(row.get("current_spurt_state") or "NO CURRENT SPURT")

        # Final progression: WATCH -> OI SPURT -> ACCELERATING -> STRONG.
        if oi >= 8 and fut_move > 0: return "🟢 STRONG LONG BUILD"
        if oi >= 8 and fut_move < 0: return "🔴 STRONG SHORT BUILD"
        if accel == "ACCELERATING LONG": return "🟢 ACCELERATING LONG"
        if accel == "ACCELERATING SHORT": return "🔴 ACCELERATING SHORT"
        if spurt == "STRONG LONG SPURT": return "🔥 STRONG LONG OI SPURT"
        if spurt == "STRONG SHORT SPURT": return "🔥 STRONG SHORT OI SPURT"
        if spurt == "LONG SPURT": return "⚡ LONG OI SPURT"
        if spurt == "SHORT SPURT": return "⚡ SHORT OI SPURT"
        if oi >= 4 and fut_move > 0: return "🟢 BUILDING LONG"
        if oi >= 4 and fut_move < 0: return "🔴 BUILDING SHORT"
        if oi >= 2 and fut_move > 0: return "🟡 WATCH LONG"
        if oi >= 2 and fut_move < 0: return "🟡 WATCH SHORT"
        if zone in bearish_zones and fut_move <= 0: return "⚠️ BEARISH ZONE WATCH"
        if zone in bullish_zones and fut_move >= 0: return "👀 BULLISH ZONE WATCH"
        return "— NEUTRAL"

    # Research timeline deltas. Negative/blank values are left as NA.
    if "first_option_lead_time" in out.columns and "first_spurt_time" in out.columns:
        lead = pd.to_datetime(out["first_option_lead_time"], utc=True, errors="coerce")
        spurt = pd.to_datetime(out["first_spurt_time"], utc=True, errors="coerce")
        out["option_lead_to_spurt_min"] = (spurt - lead).dt.total_seconds() / 60.0
    if "first_spurt_time" in out.columns and "time_4pct" in out.columns:
        spurt = pd.to_datetime(out["first_spurt_time"], utc=True, errors="coerce")
        accel = pd.to_datetime(out["time_4pct"], utc=True, errors="coerce")
        out["spurt_to_accel_min"] = (accel - spurt).dt.total_seconds() / 60.0
    if "time_4pct" in out.columns and "first_option_2x_time" in out.columns:
        accel = pd.to_datetime(out["time_4pct"], utc=True, errors="coerce")
        double = pd.to_datetime(out["first_option_2x_time"], utc=True, errors="coerce")
        out["accel_to_option_2x_min"] = (double - accel).dt.total_seconds() / 60.0

    out["oi_stage"] = out["future_oi_change_pct_t0"].apply(oi_stage)
    out["acceleration_state"] = out.apply(acceleration_state, axis=1)
    out["build_state"] = out.apply(state, axis=1)
    priority = {
        "🔴 STRONG SHORT BUILD":0, "🟢 STRONG LONG BUILD":1,
        "🔴 ACCELERATING SHORT":2, "🟢 ACCELERATING LONG":3,
        "🔥 STRONG SHORT OI SPURT":4, "🔥 STRONG LONG OI SPURT":5,
        "⚡ SHORT OI SPURT":6, "⚡ LONG OI SPURT":7,
        "🔴 BUILDING SHORT":8, "🟢 BUILDING LONG":9,
        "🟡 WATCH SHORT":10, "🟡 WATCH LONG":11,
        "⚠️ BEARISH ZONE WATCH":12, "👀 BULLISH ZONE WATCH":13, "— NEUTRAL":14
    }
    out["build_priority"] = out["build_state"].map(priority).fillna(99)
    return out

# ============================================================
# HELPERS
# ============================================================

def num(v, digits=2, suffix=""):
    if v is None or pd.isna(v):
        return "-"
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except Exception:
        return str(v)


def integer(v):
    if v is None or pd.isna(v):
        return "-"
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return str(v)


def iv_pct(v):
    if v is None or pd.isna(v):
        return "-"
    try:
        return f"{float(v) * 100:.2f}%"
    except Exception:
        return "-"


def ts_ist(v):
    if v is None or pd.isna(v):
        return "-"
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("Asia/Kolkata").strftime("%d %b %H:%M:%S")


def time_ist(v):
    """Compact IST time for detector event timestamps."""
    if v is None or pd.isna(v):
        return "-"
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("Asia/Kolkata").strftime("%H:%M")


# ============================================================
# EARLY DETECTOR v2.0 — STATE + CONVICTION
# ============================================================
def aggression_table_exists():
    d=query_df("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='futures_aggression_snapshots') AS exists")
    return bool(not d.empty and d.iloc[0].get('exists'))

def load_v2_option_baskets():
    if not option_snapshot_table_exists(): return pd.DataFrame()
    return query_df("""
    WITH d AS (SELECT MAX(trading_date) trading_date FROM public.money_flow_universe),
    enriched AS (
      SELECT o.*,
             iv-LAG(iv) OVER(PARTITION BY trading_date,symbol,option_type,wing_no ORDER BY ts) AS iv_change_3m
      FROM public.money_flow_option_snapshots o
      WHERE trading_date=(SELECT trading_date FROM d)
    )
    SELECT symbol,ts,
      SUM((option_type='CE' AND price_multiple>1)::int)+SUM((option_type='PE' AND price_multiple<1)::int) bull_option_score,
      SUM((option_type='PE' AND price_multiple>1)::int)+SUM((option_type='CE' AND price_multiple<1)::int) bear_option_score,
      SUM(((option_type='CE' AND price_multiple>1 AND oi_change_3m>0) OR
           (option_type='PE' AND price_multiple<1 AND oi_change_3m>0))::int) bull_oi_confirm,
      SUM(((option_type='PE' AND price_multiple>1 AND oi_change_3m>0) OR
           (option_type='CE' AND price_multiple<1 AND oi_change_3m>0))::int) bear_oi_confirm,
      SUM(((option_type='CE' AND price_multiple>1 AND iv_change_3m>0) OR
           (option_type='PE' AND price_multiple<1 AND iv_change_3m<=0))::int) bull_iv_confirm,
      SUM(((option_type='PE' AND price_multiple>1 AND iv_change_3m>0) OR
           (option_type='CE' AND price_multiple<1 AND iv_change_3m<=0))::int) bear_iv_confirm,
      COALESCE(MAX(same_strike_contribution) FILTER (WHERE same_strike_signal='BULLISH'),0) same_strike_bull_points,
      COALESCE(MAX(same_strike_contribution) FILTER (WHERE same_strike_signal='BEARISH'),0) same_strike_bear_points,
      COALESCE(MAX(same_strike_persistence) FILTER (WHERE same_strike_signal='BULLISH'),0) same_strike_bull_persistence,
      COALESCE(MAX(same_strike_persistence) FILTER (WHERE same_strike_signal='BEARISH'),0) same_strike_bear_persistence,
      (ARRAY_AGG(strike ORDER BY same_strike_contribution DESC) FILTER (WHERE same_strike_signal IS NOT NULL))[1] same_strike_strike,
      (ARRAY_AGG(own_oi_change_pct_0920 ORDER BY same_strike_contribution DESC) FILTER (WHERE same_strike_signal IS NOT NULL))[1] same_strike_own_change_pct,
      (ARRAY_AGG(paired_oi_change_pct_0920 ORDER BY same_strike_contribution DESC) FILTER (WHERE same_strike_signal IS NOT NULL))[1] same_strike_opposite_change_pct
    FROM enriched
    GROUP BY symbol,ts ORDER BY symbol,ts""")

def load_v2_aggression():
    if not aggression_table_exists(): return pd.DataFrame()
    out=query_df("""
    WITH d AS (SELECT MAX(trading_date) trading_date FROM public.money_flow_universe)
    SELECT a.symbol,a.ts,a.delta_pct,a.total_qty_imbalance,
           a.price_change_3m_pct,a.oi_change_3m_pct,
           (COALESCE(s.future,a.ltp)/NULLIF(u.future_price,0)-1)*100 AS session_price_pct,
           COALESCE(
               s.future_oi_change_pct_t0,
               (s.future_oi/NULLIF(u.future_oi,0)-1)*100
           ) AS cumulative_oi_pct,
           COALESCE(s.future_price_change_3m_pct,a.price_change_3m_pct) AS engine_price_change_3m_pct,
           COALESCE(s.future_oi_change_3m_pct,a.oi_change_3m_pct) AS engine_oi_change_3m_pct,
           s.futures_flow_3m_cr,s.options_flow_3m_cr,s.total_flow_3m_cr,
           s.money_flow_acceleration_3m_cr,s.pcr,s.pcr_change_3m,
           a.volume_traded,u.future_volume AS volume_930,
           a.aggressive_buy_qty,a.aggressive_sell_qty,a.classified_trade_count
    FROM public.futures_aggression_snapshots a
    JOIN public.money_flow_universe u
      ON u.trading_date=a.trading_date AND u.symbol=a.symbol
    LEFT JOIN LATERAL (
        SELECT e.future,e.future_oi,e.future_oi_change_pct_t0,
              e.future_price_change_3m_pct,e.future_oi_change_3m_pct,
              e.futures_flow_3m_cr,e.options_flow_3m_cr,e.total_flow_3m_cr,
              e.money_flow_acceleration_3m_cr,e.pcr,e.pcr_change_3m
        FROM public.stock_engine_snapshots e
        WHERE e.symbol=a.symbol
          AND (e.ts AT TIME ZONE 'Asia/Kolkata')::date=a.trading_date
        ORDER BY ABS(EXTRACT(EPOCH FROM (e.ts-a.ts)))
        LIMIT 1
    ) s ON TRUE
    WHERE a.trading_date=(SELECT trading_date FROM d)
    ORDER BY symbol,ts""")
    if out.empty:
        return out
    for col in [
        "session_price_pct","cumulative_oi_pct","volume_traded","volume_930",
        "engine_price_change_3m_pct","engine_oi_change_3m_pct",
        "futures_flow_3m_cr","options_flow_3m_cr","total_flow_3m_cr",
        "money_flow_acceleration_3m_cr","pcr","pcr_change_3m"
    ]:
        out[col]=pd.to_numeric(out[col],errors="coerce").astype(float)
    out["volume_since_930"]=(pd.to_numeric(out["volume_traded"],errors="coerce")-
                              pd.to_numeric(out["volume_930"],errors="coerce")).clip(lower=0)
    out["market_session_pct"]=out.groupby("ts")["session_price_pct"].transform("median")
    out["relative_strength_pct"]=out["session_price_pct"]-out["market_session_pct"]
    out["volume_percentile"]=out.groupby("ts")["volume_since_930"].rank(pct=True,method="average")
    return out

@st.cache_data(ttl=300)
def load_v2_realized_volatility():
    """Per-stock 3-minute volatility from the latest five stored sessions."""
    return query_df("""
    WITH dates AS (
      SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date d
      FROM public.stock_engine_snapshots ORDER BY d DESC LIMIT 5
    ), r AS (
      SELECT symbol,ts,
        (future/NULLIF(LAG(future) OVER(
          PARTITION BY symbol,(ts AT TIME ZONE 'Asia/Kolkata')::date ORDER BY ts),0)-1)*100 ret_3m
      FROM public.stock_engine_snapshots
      WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date IN (SELECT d FROM dates)
    )
    SELECT symbol,STDDEV_SAMP(ret_3m) AS realized_3m_vol_pct,COUNT(ret_3m) AS vol_observations
    FROM r WHERE ret_3m IS NOT NULL GROUP BY symbol""")


def load_zone_aggression_signals():
    """Qualifying futures aggression aligned with the nearest liquidity-zone snapshot."""
    if not aggression_table_exists():
        return pd.DataFrame()

    return query_df("""
    WITH d AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    universe AS (
        SELECT trading_date, rank AS money_flow_rank, symbol
        FROM public.money_flow_universe
        WHERE trading_date = (SELECT trading_date FROM d)
    ),
    aggression AS (
        SELECT
            a.trading_date, a.symbol, a.ts, a.delta_pct,
            a.price_change_3m_pct, a.oi_change_3m_pct,
            a.aggressive_buy_qty, a.aggressive_sell_qty,
            a.classified_trade_count,
            CASE
                WHEN a.delta_pct >= 30 AND a.price_change_3m_pct > 0
                 AND a.oi_change_3m_pct > 0 THEN 'BUY AGGRESSION'
                WHEN a.delta_pct <= -30 AND a.price_change_3m_pct < 0
                 AND a.oi_change_3m_pct > 0 THEN 'SELL AGGRESSION'
            END AS aggression_type
        FROM public.futures_aggression_snapshots a
        WHERE a.trading_date = (SELECT trading_date FROM d)
          AND a.classified_trade_count >= 5
          AND COALESCE(a.aggressive_buy_qty, 0)
              + COALESCE(a.aggressive_sell_qty, 0) > 0
          AND (
                (a.delta_pct >= 30 AND a.price_change_3m_pct > 0 AND a.oi_change_3m_pct > 0)
                OR
                (a.delta_pct <= -30 AND a.price_change_3m_pct < 0 AND a.oi_change_3m_pct > 0)
          )
    ),
    aligned AS (
        SELECT
            u.trading_date, u.money_flow_rank, u.symbol,
            a.ts, a.aggression_type, a.delta_pct,
            a.price_change_3m_pct, a.oi_change_3m_pct,
            a.aggressive_buy_qty, a.aggressive_sell_qty,
            a.classified_trade_count,
            z.ts AS zone_source_ts, z.spot, z.zone_state, z.next_zone
        FROM universe u
        JOIN aggression a
          ON a.trading_date = u.trading_date AND a.symbol = u.symbol
        LEFT JOIN LATERAL (
            SELECT s.ts, s.spot, s.zone_state, s.next_zone
            FROM public.stock_engine_snapshots s
            WHERE s.symbol = a.symbol
              AND s.ts BETWEEN a.ts - INTERVAL '2 minutes'
                           AND a.ts + INTERVAL '4 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (s.ts - a.ts)))
            LIMIT 1
        ) z ON TRUE
    )
    SELECT *,
        CASE
            WHEN aggression_type = 'BUY AGGRESSION'
             AND UPPER(COALESCE(zone_state, '')) LIKE '%%ABOVE STRONG SUPPLY%%'
                THEN 'ABOVE STRONG SUPPLY + BUY AGGRESSION'
            WHEN aggression_type = 'SELL AGGRESSION'
             AND (
                    UPPER(COALESCE(zone_state, '')) LIKE '%%DEMAND%%BROKEN%%'
                    OR UPPER(COALESCE(zone_state, '')) LIKE '%%BELOW STRONG DEMAND%%'
                 )
                THEN 'BELOW WEAK DEMAND + SELL AGGRESSION'
        END AS setup
    FROM aligned
    WHERE (
            aggression_type = 'BUY AGGRESSION'
            AND UPPER(COALESCE(zone_state, '')) LIKE '%%ABOVE STRONG SUPPLY%%'
          )
       OR (
            aggression_type = 'SELL AGGRESSION'
            AND (
                   UPPER(COALESCE(zone_state, '')) LIKE '%%DEMAND%%BROKEN%%'
                   OR UPPER(COALESCE(zone_state, '')) LIKE '%%BELOW STRONG DEMAND%%'
                )
          )
    ORDER BY money_flow_rank, ts
    """)


def load_aggression_followthrough_sequences():
    """First fresh aggression followed later by covering/unwinding, without zone filters."""
    if not aggression_table_exists():
        return pd.DataFrame()

    return query_df("""
    WITH d AS (
        SELECT MAX(trading_date) AS trading_date
        FROM public.money_flow_universe
    ),
    universe AS (
        SELECT trading_date, rank AS money_flow_rank, symbol
        FROM public.money_flow_universe
        WHERE trading_date = (SELECT trading_date FROM d)
    ),
    events AS (
        SELECT
            a.*,
            (
                a.classified_trade_count >= 5
                AND COALESCE(a.aggressive_buy_qty, 0)
                    + COALESCE(a.aggressive_sell_qty, 0) > 0
            ) AS eligible
        FROM public.futures_aggression_snapshots a
        WHERE a.trading_date = (SELECT trading_date FROM d)
    ),
    firsts AS (
        SELECT
            symbol,
            MIN(ts) FILTER (
                WHERE eligible
                  AND delta_pct >= 30
                  AND price_change_3m_pct > 0
                  AND oi_change_3m_pct > 0
            ) AS first_buy_ts,
            MIN(ts) FILTER (
                WHERE eligible
                  AND delta_pct <= -30
                  AND price_change_3m_pct < 0
                  AND oi_change_3m_pct > 0
            ) AS first_sell_ts
        FROM events
        GROUP BY symbol
    ),
    starts AS (
        SELECT u.trading_date, u.money_flow_rank, u.symbol,
               'BUY AGGRESSION TO SHORT COVERING'::text AS sequence_type,
               f.first_buy_ts AS start_ts
        FROM universe u
        LEFT JOIN firsts f USING (symbol)
        WHERE f.first_buy_ts IS NOT NULL

        UNION ALL

        SELECT u.trading_date, u.money_flow_rank, u.symbol,
               'SELL AGGRESSION TO LONG UNWINDING'::text AS sequence_type,
               f.first_sell_ts AS start_ts
        FROM universe u
        LEFT JOIN firsts f USING (symbol)
        WHERE f.first_sell_ts IS NOT NULL
    )
    SELECT
        s.trading_date,
        s.money_flow_rank,
        s.symbol,
        s.sequence_type,
        s.start_ts,
        start_event.delta_pct AS start_delta_pct,
        start_event.price_change_3m_pct AS start_price_change_3m_pct,
        start_event.oi_change_3m_pct AS start_oi_change_3m_pct,
        start_event.classified_trade_count AS start_classified_trades,
        start_price.future AS start_future,
        follow_event.ts AS follow_ts,
        follow_event.delta_pct AS follow_delta_pct,
        follow_event.price_change_3m_pct AS follow_price_change_3m_pct,
        follow_event.oi_change_3m_pct AS follow_oi_change_3m_pct,
        follow_event.classified_trade_count AS follow_classified_trades,
        follow_price.future AS follow_future,
        CASE WHEN follow_event.ts IS NOT NULL
             THEN EXTRACT(EPOCH FROM (follow_event.ts - s.start_ts)) / 60.0
        END AS minutes_to_follow,
        CASE WHEN start_price.future IS NOT NULL AND follow_price.future IS NOT NULL
             THEN ((follow_price.future / NULLIF(start_price.future, 0)) - 1) * 100
        END AS future_move_to_follow_pct,
        CASE WHEN follow_event.ts IS NULL THEN 'WAITING' ELSE 'SEQUENCE COMPLETE' END AS sequence_status,
        CASE
            WHEN follow_event.ts IS NULL THEN 'WAITING'
            WHEN EXTRACT(EPOCH FROM (follow_event.ts - s.start_ts)) / 60.0 <= 15 THEN 'FAST 0-15M'
            WHEN EXTRACT(EPOCH FROM (follow_event.ts - s.start_ts)) / 60.0 <= 30 THEN 'STRONG 16-30M'
            WHEN EXTRACT(EPOCH FROM (follow_event.ts - s.start_ts)) / 60.0 <= 60 THEN 'DEVELOPING 31-60M'
            ELSE 'LATE 60M+'
        END AS timing_bucket
    FROM starts s
    JOIN events start_event
      ON start_event.symbol = s.symbol AND start_event.ts = s.start_ts
    LEFT JOIN LATERAL (
        SELECT e.*
        FROM events e
        WHERE e.symbol = s.symbol
          AND e.ts > s.start_ts
          AND (
                (s.sequence_type = 'BUY AGGRESSION TO SHORT COVERING'
                 AND e.price_change_3m_pct > 0 AND e.oi_change_3m_pct < 0)
                OR
                (s.sequence_type = 'SELL AGGRESSION TO LONG UNWINDING'
                 AND e.price_change_3m_pct < 0 AND e.oi_change_3m_pct < 0)
              )
        ORDER BY e.ts
        LIMIT 1
    ) follow_event ON TRUE
    LEFT JOIN LATERAL (
        SELECT x.future
        FROM public.stock_engine_snapshots x
        WHERE x.symbol = s.symbol
          AND x.ts BETWEEN s.start_ts - INTERVAL '2 minutes'
                       AND s.start_ts + INTERVAL '4 minutes'
        ORDER BY ABS(EXTRACT(EPOCH FROM (x.ts - s.start_ts)))
        LIMIT 1
    ) start_price ON TRUE
    LEFT JOIN LATERAL (
        SELECT x.future
        FROM public.stock_engine_snapshots x
        WHERE x.symbol = s.symbol
          AND follow_event.ts IS NOT NULL
          AND x.ts BETWEEN follow_event.ts - INTERVAL '2 minutes'
                       AND follow_event.ts + INTERVAL '4 minutes'
        ORDER BY ABS(EXTRACT(EPOCH FROM (x.ts - follow_event.ts)))
        LIMIT 1
    ) follow_price ON TRUE
    ORDER BY s.sequence_type, s.money_flow_rank
    """)

def tail_count(vals,pred):
    n=0
    for v in reversed(list(vals)):
        try: ok=pred(float(v))
        except: ok=False
        if not ok: break
        n+=1
    return n

def first_persistent(g,col,threshold=5,n=3):
    if g.empty:return pd.NaT
    h=pd.to_numeric(g[col],errors='coerce').fillna(0).ge(threshold).rolling(n).sum()
    x=h[h>=n]
    return pd.NaT if x.empty else g.loc[x.index[0],'ts']


def build_v2_state(universe_df, option_df, aggression_df, milestone_df, volatility_df,
                   avwap_df=None):
    """v2.9 structure-first live board with intraday memory.

    Adds:
      - current_state / current_conviction / current_score
      - peak_state_today / peak_conviction_today / peak_score_today / peak_state_time
      - first_current_direction_clue
      - first_bull_clue / first_bear_clue
      - reversal flag + reversal_time
      - absorption/conflict flags

    The score is observational/research logic, not a trade recommendation.
    """
    if universe_df.empty:
        return pd.DataFrame()

    mm = {}
    if not milestone_df.empty:
        mm = {r["symbol"]: r for _, r in milestone_df.iterrows()}
    volmap = {}
    if volatility_df is not None and not volatility_df.empty:
        volmap = {r["symbol"]: pd.to_numeric(r.get("realized_3m_vol_pct"),errors="coerce")
                  for _,r in volatility_df.iterrows()}

    def opt_pts(score, persist):
        pts = 3 if score >= 6 else 2.5 if score >= 5 else 2 if score >= 4 else 1 if score >= 3 else 0
        # Persistence is an independent point; the former min(3) cap erased it.
        # Reserve half a point for OI/IV quality confirmation so the complete
        # option group remains capped at four points.
        return min(3.5, pts + (0.5 if persist >= 3 else 0))

    def session_pts(value, normalized_units):
        if pd.isna(value):
            return 0
        if pd.notna(normalized_units):
            return 2 if normalized_units >= 1.50 else 1 if normalized_units >= 0.75 else 0
        move = abs(float(value))
        return 2 if move >= 0.50 else 1 if move >= 0.25 else 0

    def classify_state(bull, bear, imb, px, td, delta_eligible, m24,
                       bo, so, session_px):
        bull = min(10, float(bull))
        bear = min(10, float(bear))
        direction = "BULL" if bull > bear else "BEAR" if bear > bull else "MIXED"
        score = max(bull, bear)
        conflict = bull >= 4 and bear >= 4
        structural_bull = pd.notna(session_px) and session_px >= 0.50 and bo >= 5 and so <= 1
        structural_bear = pd.notna(session_px) and session_px <= -0.50 and so >= 5 and bo <= 1

        absorption = None
        if pd.notna(px):
            sell_pressure = (delta_eligible and td <= -30) or (pd.notna(imb) and imb <= -20)
            buy_pressure = (delta_eligible and td >= 30) or (pd.notna(imb) and imb >= 20)
            if sell_pressure and px >= 0:
                absorption = "SELL ABSORPTION"
            elif buy_pressure and px <= 0:
                absorption = "BUY ABSORPTION"

        if conflict:
            return "CONFLICT", "CONFLICT", score, direction, absorption
        if structural_bull and ((pd.notna(px) and px <= 0.05) or (pd.notna(imb) and imb < 0)):
            return "BULLISH CONSOLIDATION", "HIGH", score, direction, absorption
        if structural_bear and ((pd.notna(px) and px >= -0.05) or (pd.notna(imb) and imb > 0)):
            return "BEARISH CONSOLIDATION", "HIGH", score, direction, absorption
        if absorption and score < 6:
            return absorption, "WARNING", score, direction, absorption
        if score >= 8:
            if pd.notna(m24) and m24 <= 30:
                return ("ACCELERATING LONG" if direction == "BULL" else "ACCELERATING SHORT"), "VERY HIGH", score, direction, absorption
            return ("CONFIRMED LONG" if direction == "BULL" else "CONFIRMED SHORT"), "VERY HIGH", score, direction, absorption
        if score >= 6:
            return ("CONFIRMED LONG" if direction == "BULL" else "CONFIRMED SHORT"), "HIGH", score, direction, absorption
        if score >= 4:
            return ("BUILDING LONG" if direction == "BULL" else "BUILDING SHORT"), "MEDIUM", score, direction, absorption
        if score >= 2:
            return ("WATCH LONG" if direction == "BULL" else "WATCH SHORT"), "LOW", score, direction, absorption
        return "NEUTRAL", "LOW", score, direction, absorption

    def score_at(sym, ts, og_all, ag_all, av_all, m):
        og = og_all[og_all["ts"] <= ts].copy() if not og_all.empty else pd.DataFrame()
        ag = ag_all[ag_all["ts"] <= ts].copy() if not ag_all.empty else pd.DataFrame()
        av = av_all[av_all["ts"] <= ts].copy() if av_all is not None and not av_all.empty else pd.DataFrame()

        bo = so = bp = sp = 0
        bull_opt_quality = bear_opt_quality = 0
        same_strike_bull_points = same_strike_bear_points = 0.0
        same_strike_bull_persistence = same_strike_bear_persistence = 0
        same_strike_signal = None
        same_strike_strike = same_strike_own_change_pct = same_strike_opposite_change_pct = None
        option_source_ts = pd.NaT
        if not og.empty:
            option_source_ts = og.iloc[-1]["ts"]
            bo = int(pd.to_numeric(og.iloc[-1]["bull_option_score"], errors="coerce") or 0)
            so = int(pd.to_numeric(og.iloc[-1]["bear_option_score"], errors="coerce") or 0)
            bp = tail_count(og["bull_option_score"], lambda x: x >= 5)
            sp = tail_count(og["bear_option_score"], lambda x: x >= 5)
            b_oi=pd.to_numeric(og.iloc[-1].get("bull_oi_confirm"),errors="coerce")
            b_iv=pd.to_numeric(og.iloc[-1].get("bull_iv_confirm"),errors="coerce")
            s_oi=pd.to_numeric(og.iloc[-1].get("bear_oi_confirm"),errors="coerce")
            s_iv=pd.to_numeric(og.iloc[-1].get("bear_iv_confirm"),errors="coerce")
            bull_opt_quality=int(0 if pd.isna(b_oi) else b_oi)+int(0 if pd.isna(b_iv) else b_iv)
            bear_opt_quality=int(0 if pd.isna(s_oi) else s_oi)+int(0 if pd.isna(s_iv) else s_iv)
            same_strike_bull_points=float(pd.to_numeric(og.iloc[-1].get("same_strike_bull_points"),errors="coerce") or 0)
            same_strike_bear_points=float(pd.to_numeric(og.iloc[-1].get("same_strike_bear_points"),errors="coerce") or 0)
            same_strike_bull_persistence=int(pd.to_numeric(og.iloc[-1].get("same_strike_bull_persistence"),errors="coerce") or 0)
            same_strike_bear_persistence=int(pd.to_numeric(og.iloc[-1].get("same_strike_bear_persistence"),errors="coerce") or 0)
            if same_strike_bull_points > same_strike_bear_points: same_strike_signal = "BULLISH"
            elif same_strike_bear_points > same_strike_bull_points: same_strike_signal = "BEARISH"
            same_strike_strike=pd.to_numeric(og.iloc[-1].get("same_strike_strike"),errors="coerce")
            same_strike_own_change_pct=pd.to_numeric(og.iloc[-1].get("same_strike_own_change_pct"),errors="coerce")
            same_strike_opposite_change_pct=pd.to_numeric(og.iloc[-1].get("same_strike_opposite_change_pct"),errors="coerce")

        imb = px = oi = td = session_px = cumoi = rel_strength = vol_pctile = None
        classified_trades = classified_qty = 0
        buy_p = sell_p = long_p = short_p = delta_buy_p = delta_sell_p = 0
        delta_eligible = False
        aggression_quality = "NO DATA"
        flow_agreement = "NO DATA"
        aggression_source_ts = pd.NaT
        if not ag.empty:
            a = ag.iloc[-1]
            aggression_source_ts = a.get("ts")
            imb = pd.to_numeric(a.get("total_qty_imbalance"), errors="coerce")
            px = pd.to_numeric(a.get("engine_price_change_3m_pct"), errors="coerce")
            if pd.isna(px):
                px = pd.to_numeric(a.get("price_change_3m_pct"), errors="coerce")
            oi = pd.to_numeric(a.get("engine_oi_change_3m_pct"), errors="coerce")
            if pd.isna(oi):
                oi = pd.to_numeric(a.get("oi_change_3m_pct"), errors="coerce")
            session_px = pd.to_numeric(a.get("session_price_pct"), errors="coerce")
            cumoi = pd.to_numeric(a.get("cumulative_oi_pct"), errors="coerce")
            rel_strength = pd.to_numeric(a.get("relative_strength_pct"), errors="coerce")
            vol_pctile = pd.to_numeric(a.get("volume_percentile"), errors="coerce")
            td = pd.to_numeric(a.get("delta_pct"), errors="coerce")
            classified_trades_raw = pd.to_numeric(a.get("classified_trade_count"), errors="coerce")
            classified_trades = 0 if pd.isna(classified_trades_raw) else int(classified_trades_raw)
            aggressive_buy = pd.to_numeric(a.get("aggressive_buy_qty"), errors="coerce")
            aggressive_sell = pd.to_numeric(a.get("aggressive_sell_qty"), errors="coerce")
            classified_qty = int((0 if pd.isna(aggressive_buy) else aggressive_buy) +
                                 (0 if pd.isna(aggressive_sell) else aggressive_sell))
            delta_eligible = pd.notna(td) and classified_trades >= 5 and classified_qty > 0
            aggression_quality = "ELIGIBLE" if delta_eligible else "LOW SAMPLE"

            buy_p = tail_count(ag["total_qty_imbalance"], lambda x: x >= 20)
            sell_p = tail_count(ag["total_qty_imbalance"], lambda x: x <= -20)

            imbs = pd.to_numeric(ag["total_qty_imbalance"], errors="coerce")
            pxs = pd.to_numeric(ag["price_change_3m_pct"], errors="coerce")
            ois = pd.to_numeric(ag["oi_change_3m_pct"], errors="coerce")
            lf = (imbs >= 20) & (pxs > 0) & (ois > 0)
            sf = (imbs <= -20) & (pxs < 0) & (ois > 0)
            long_p = tail_count(lf.astype(int), lambda x: x == 1)
            short_p = tail_count(sf.astype(int), lambda x: x == 1)

            trades = pd.to_numeric(ag["classified_trade_count"], errors="coerce").fillna(0)
            buys = pd.to_numeric(ag["aggressive_buy_qty"], errors="coerce").fillna(0)
            sells = pd.to_numeric(ag["aggressive_sell_qty"], errors="coerce").fillna(0)
            deltas = pd.to_numeric(ag["delta_pct"], errors="coerce")
            eligible = (trades >= 5) & ((buys + sells) > 0)
            delta_buy_p = tail_count((eligible & (deltas >= 30)).astype(int), lambda x: x == 1)
            delta_sell_p = tail_count((eligible & (deltas <= -30)).astype(int), lambda x: x == 1)

            if delta_eligible and pd.notna(imb):
                if td >= 30 and imb >= 20:
                    flow_agreement = "BUY AGREEMENT"
                elif td <= -30 and imb <= -20:
                    flow_agreement = "SELL AGREEMENT"
                elif abs(td) >= 30 and abs(imb) >= 20:
                    flow_agreement = "ORDER-FLOW CONFLICT"
                else:
                    flow_agreement = "PARTIAL / NEUTRAL"

        # ============================================================
        # EARLY DETECTOR v2.9 — STRUCTURE-FIRST /10 SCORE
        #
        # BSE + COCHINSHIP research revision:
        #   Price persistence       2.0
        #   Futures OI confirmation 2.0
        #   Futures-state persistence 2.0
        #   3m money-flow expansion 1.5
        #   PCR rolling trend       1.0
        #   Executed aggression     1.0
        #   Qty imbalance           0.5
        #
        # Options, same-strike OI, AVWAP, RS and volume remain visible
        # confirmations but no longer inflate the primary /10 score.
        # ============================================================

        bull = 0.0
        bear = 0.0

        # ---------- 1) PRICE PERSISTENCE: max 2 points ----------
        price_persistence_points = 0.0
        bull_price_p = bear_price_p = 0
        if not ag.empty:
            recent_px = pd.to_numeric(
                ag.get("engine_price_change_3m_pct", ag.get("price_change_3m_pct")),
                errors="coerce"
            ).dropna().tail(3)
            bull_count = int((recent_px > 0.05).sum())
            bear_count = int((recent_px < -0.05).sum())
            bull_price_p = 2.0 if len(recent_px) >= 3 and bull_count == 3 else 1.0 if bull_count >= 2 else 0.0
            bear_price_p = 2.0 if len(recent_px) >= 3 and bear_count == 3 else 1.0 if bear_count >= 2 else 0.0
            bull += bull_price_p
            bear += bear_price_p
            price_persistence_points = max(bull_price_p, bear_price_p)

        # ---------- 2) FUTURES OI CONFIRMATION: max 2 points ----------
        oi_confirmation_points = 0.0
        bull_oi_pts = bear_oi_pts = 0.0
        directional_sign = 1 if pd.notna(session_px) and session_px > 0 else -1 if pd.notna(session_px) and session_px < 0 else (1 if pd.notna(px) and px > 0 else -1 if pd.notna(px) and px < 0 else 0)
        if pd.notna(oi) and oi >= 0.10:
            if directional_sign > 0:
                bull_oi_pts += 1.0
            elif directional_sign < 0:
                bear_oi_pts += 1.0
        if pd.notna(cumoi) and cumoi >= 1.0:
            if directional_sign > 0:
                bull_oi_pts += 1.0
            elif directional_sign < 0:
                bear_oi_pts += 1.0
        bull += min(2.0, bull_oi_pts)
        bear += min(2.0, bear_oi_pts)
        oi_confirmation_points = max(bull_oi_pts, bear_oi_pts)

        # ---------- 3) FUTURES STATE PERSISTENCE: max 2 points ----------
        futures_state = "UNKNOWN"
        futures_state_persistence = 0
        futures_state_points = 0.0
        if pd.notna(px) and pd.notna(oi):
            if px > 0 and oi > 0:
                futures_state = "LONG_BUILDUP"
            elif px < 0 and oi > 0:
                futures_state = "SHORT_BUILDUP"
            elif px > 0 and oi < 0:
                futures_state = "SHORT_COVERING"
            elif px < 0 and oi < 0:
                futures_state = "LONG_UNWINDING"
            else:
                futures_state = "MIXED"

        if not ag.empty:
            px_hist = pd.to_numeric(
                ag.get("engine_price_change_3m_pct", ag.get("price_change_3m_pct")),
                errors="coerce"
            )
            oi_hist = pd.to_numeric(
                ag.get("engine_oi_change_3m_pct", ag.get("oi_change_3m_pct")),
                errors="coerce"
            )
            long_flags = ((px_hist > 0) & (oi_hist > 0)).astype(int)
            short_flags = ((px_hist < 0) & (oi_hist > 0)).astype(int)
            long_state_p = tail_count(long_flags, lambda x: x == 1)
            short_state_p = tail_count(short_flags, lambda x: x == 1)

            if long_state_p > short_state_p and long_state_p > 0:
                futures_state_persistence = long_state_p
                futures_state_points = 2.0 if long_state_p >= 3 else 1.0 if long_state_p >= 2 else 0.0
                bull += futures_state_points
            elif short_state_p > 0:
                futures_state_persistence = short_state_p
                futures_state_points = 2.0 if short_state_p >= 3 else 1.0 if short_state_p >= 2 else 0.0
                bear += futures_state_points

        # ---------- 4) LIVE 3-MIN MONEY FLOW EXPANSION: max 1.5 ----------
        total_flow_3m_cr = None
        money_flow_acceleration_3m_cr = None
        money_flow_acceleration_x = None
        money_flow_points = 0.0
        if not ag.empty and "total_flow_3m_cr" in ag.columns:
            flow_series = pd.to_numeric(ag["total_flow_3m_cr"], errors="coerce")
            current_flow = flow_series.iloc[-1] if len(flow_series) else float("nan")
            prior = flow_series.iloc[:-1].dropna().tail(5)
            total_flow_3m_cr = None if pd.isna(current_flow) else float(current_flow)
            money_flow_acceleration_3m_cr = pd.to_numeric(
                ag.iloc[-1].get("money_flow_acceleration_3m_cr"), errors="coerce"
            )
            if pd.notna(current_flow) and len(prior) >= 3 and prior.mean() > 0:
                money_flow_acceleration_x = float(current_flow / prior.mean())
                money_flow_points = 1.5 if money_flow_acceleration_x >= 2.0 else 1.0 if money_flow_acceleration_x >= 1.5 else 0.0
                if money_flow_points:
                    if pd.notna(px) and px > 0:
                        bull += money_flow_points
                    elif pd.notna(px) and px < 0:
                        bear += money_flow_points

        # ---------- 5) PCR PERSISTENCE / 9-MIN TREND: max 1 ----------
        pcr_trend_9m = None
        pcr_trend_points = 0.0
        if not ag.empty and "pcr_change_3m" in ag.columns:
            pcr_changes = pd.to_numeric(ag["pcr_change_3m"], errors="coerce").dropna().tail(3)
            if len(pcr_changes) >= 2:
                pcr_trend_9m = float(pcr_changes.sum())
                positive = int((pcr_changes > 0).sum())
                negative = int((pcr_changes < 0).sum())
                if positive >= 2 and pcr_trend_9m > 0:
                    bull += 1.0
                    pcr_trend_points = 1.0
                elif negative >= 2 and pcr_trend_9m < 0:
                    bear += 1.0
                    pcr_trend_points = 1.0

        # ---------- 6) EXECUTED AGGRESSION: max 1 ----------
        aggression_points = 0.0
        if delta_eligible:
            if td >= 30:
                bull += 1.0
                aggression_points = 1.0
            elif td <= -30:
                bear += 1.0
                aggression_points = 1.0

        # ---------- 7) QUANTITY IMBALANCE: max 0.5 ----------
        imbalance_points = 0.0
        if pd.notna(imb):
            if imb >= 20:
                bull += 0.5
                imbalance_points = 0.5
            elif imb <= -20:
                bear += 0.5
                imbalance_points = 0.5

        # Keep existing option/same-strike fields as confirmation diagnostics.
        # They are intentionally NOT added to the primary v2.9 score.
        bull_option_confirmation = opt_pts(bo, bp)
        bear_option_confirmation = opt_pts(so, sp)
        if bull_opt_quality >= 3 and bo > so:
            bull_option_confirmation += 0.5
        if bear_opt_quality >= 3 and so > bo:
            bear_option_confirmation += 0.5
        bull_option_confirmation += min(1.5, same_strike_bull_points)
        bear_option_confirmation += min(1.5, same_strike_bear_points)

        # Stock-specific participation and AVWAP remain diagnostic confirmations.
        # They no longer add points to the core /10 score.
        # First-hour AVWAP confirmation contributes at most 1.5 points per side:
        # 0.5 for each continuing AVWAP above/below its frozen 09:15-10:15
        # reference, plus 0.5 for a fresh aligned crossing within 30 minutes.
        avwap_bull_points = avwap_bear_points = 0.0
        avwap_state = "NO DATA"
        avwap_high = avwap_low = hourly_avwap_high = hourly_avwap_low = None
        avwap_source_ts = pd.NaT
        if not av.empty:
            ar = av.iloc[-1]
            avwap_source_ts = ar.get("ts")
            avwap_high = pd.to_numeric(ar.get("avwap_high"), errors="coerce")
            avwap_low = pd.to_numeric(ar.get("avwap_low"), errors="coerce")
            hourly_avwap_high = pd.to_numeric(ar.get("hourly_avwap_high"), errors="coerce")
            hourly_avwap_low = pd.to_numeric(ar.get("hourly_avwap_low"), errors="coerce")
            if pd.notna(hourly_avwap_high) and pd.notna(hourly_avwap_low):
                if pd.notna(avwap_high):
                    avwap_bull_points += 0.5 if avwap_high > hourly_avwap_high else 0
                    avwap_bear_points += 0.5 if avwap_high < hourly_avwap_high else 0
                if pd.notna(avwap_low):
                    avwap_bull_points += 0.5 if avwap_low > hourly_avwap_low else 0
                    avwap_bear_points += 0.5 if avwap_low < hourly_avwap_low else 0
                cutoff = pd.Timestamp(ts) - pd.Timedelta(minutes=30)
                recent = av[av["ts"] >= cutoff]
                bull_cross = recent["high_cross"].eq("CROSS_ABOVE").any() or recent["low_cross"].eq("CROSS_ABOVE").any()
                bear_cross = recent["high_cross"].eq("CROSS_BELOW").any() or recent["low_cross"].eq("CROSS_BELOW").any()
                if bull_cross: avwap_bull_points += 0.5
                if bear_cross: avwap_bear_points += 0.5
                avwap_bull_points = min(1.5, avwap_bull_points)
                avwap_bear_points = min(1.5, avwap_bear_points)
                if avwap_bull_points > avwap_bear_points: avwap_state = "BULLISH CONFIRMATION"
                elif avwap_bear_points > avwap_bull_points: avwap_state = "BEARISH CONFIRMATION"
                else: avwap_state = "MIXED / NEUTRAL"
                # v2.9: AVWAP is confirmation-only; do not add to primary /10 score.

        extension_status = "UNKNOWN"
        if pd.notna(normalized_units):
            extension_status = ("EARLY" if normalized_units < 0.75 else
                                "NORMAL" if normalized_units < 1.50 else
                                "EXTENDED" if normalized_units < 2.50 else "EXTREME")

        m24 = pd.to_numeric(m.get("minutes_2_to_4"), errors="coerce") if m is not None else None
        t4 = m.get("time_4pct") if m is not None else pd.NaT
        # Acceleration point applies only once 4% milestone has actually occurred.
        if pd.notna(m24) and m24 <= 30 and pd.notna(t4):
            try:
                accel_active = pd.Timestamp(ts) >= pd.Timestamp(t4)
            except Exception:
                accel_active = False
            if accel_active:
                pass  # v2.9: milestone acceleration is diagnostic, not an extra score point.

        state, conviction, score, direction, absorption = classify_state(
            bull, bear, imb, px, td, delta_eligible, m24, bo, so, session_px
        )
        return {
            "ts": ts, "bull": min(10, bull), "bear": min(10, bear),
            "state": state, "conviction": conviction, "score": score,
            "direction": direction, "absorption": absorption,
            "option_bull_score": bo, "option_bear_score": so,
            "option_persistence": max(bp, sp),
            "total_qty_imbalance": imb,
            "imbalance_persistence": max(buy_p, sell_p),
            "price_change_3m_pct": px,
            "oi_change_3m_pct": oi,
            "session_price_pct": session_px,
            "cumulative_oi_pct": cumoi,
            "normalized_move_units": normalized_units,
            "option_oi_iv_confirmation": max(bull_opt_quality,bear_opt_quality),
            "same_strike_oi_signal": same_strike_signal,
            "same_strike_oi_points": max(same_strike_bull_points,same_strike_bear_points),
            "same_strike_oi_persistence": max(same_strike_bull_persistence,same_strike_bear_persistence),
            "same_strike_oi_strike": same_strike_strike,
            "same_strike_own_change_pct": same_strike_own_change_pct,
            "same_strike_opposite_change_pct": same_strike_opposite_change_pct,
            "relative_strength_pct": rel_strength,
            "volume_percentile": vol_pctile,
            "extension_status": extension_status,
            "trade_delta_pct": td,
            "classified_trade_count": classified_trades,
            "classified_qty": classified_qty,
            "delta_persistence": max(delta_buy_p, delta_sell_p),
            "aggression_quality": aggression_quality,
            "order_flow_agreement": flow_agreement,
            "option_source_ts": option_source_ts,
            "aggression_source_ts": aggression_source_ts
            ,"avwap_bull_points": avwap_bull_points,
            "avwap_bear_points": avwap_bear_points,
            "avwap_state": avwap_state,
            "avwap_high": avwap_high,
            "avwap_low": avwap_low,
            "hourly_avwap_high": hourly_avwap_high,
            "hourly_avwap_low": hourly_avwap_low,
            "avwap_source_ts": avwap_source_ts,
            "price_persistence_points": price_persistence_points,
            "oi_confirmation_points": oi_confirmation_points,
            "futures_state": futures_state,
            "futures_state_persistence": futures_state_persistence,
            "futures_state_points": futures_state_points,
            "total_flow_3m_cr": total_flow_3m_cr,
            "money_flow_acceleration_3m_cr": money_flow_acceleration_3m_cr,
            "money_flow_acceleration_x": money_flow_acceleration_x,
            "money_flow_points": money_flow_points,
            "pcr_trend_9m": pcr_trend_9m,
            "pcr_trend_points": pcr_trend_points,
            "aggression_points": aggression_points,
            "imbalance_points": imbalance_points
        }

    result = []
    snapshot_history = []

    for _, u in universe_df.sort_values("rank").iterrows():
        sym = u["symbol"]
        og = option_df[option_df["symbol"].eq(sym)].sort_values("ts").reset_index(drop=True) if not option_df.empty else pd.DataFrame()
        ag = aggression_df[aggression_df["symbol"].eq(sym)].sort_values("ts").reset_index(drop=True) if not aggression_df.empty else pd.DataFrame()
        av = avwap_df[avwap_df["symbol"].eq(sym)].sort_values("ts").reset_index(drop=True) if avwap_df is not None and not avwap_df.empty else pd.DataFrame()
        m = mm.get(sym)

        # First persistent directional option clues
        first_bo = first_persistent(og, "bull_option_score") if not og.empty else pd.NaT
        first_so = first_persistent(og, "bear_option_score") if not og.empty else pd.NaT

        # First reliable executed-aggression + price + OI clues.
        first_buy = first_sell = pd.NaT
        if not ag.empty:
            pxs = pd.to_numeric(ag["price_change_3m_pct"], errors="coerce")
            ois = pd.to_numeric(ag["oi_change_3m_pct"], errors="coerce")
            deltas = pd.to_numeric(ag["delta_pct"], errors="coerce")
            trades = pd.to_numeric(ag["classified_trade_count"], errors="coerce").fillna(0)
            qty = (pd.to_numeric(ag["aggressive_buy_qty"], errors="coerce").fillna(0) +
                   pd.to_numeric(ag["aggressive_sell_qty"], errors="coerce").fillna(0))
            eligible = (trades >= 5) & (qty > 0)
            lf = eligible & (deltas >= 30) & (pxs > 0) & (ois > 0)
            sf = eligible & (deltas <= -30) & (pxs < 0) & (ois > 0)
            if lf.any():
                first_buy = ag.loc[lf, "ts"].iloc[0]
            if sf.any():
                first_sell = ag.loc[sf, "ts"].iloc[0]

        # Build intraday state history on union of option/aggression timestamps.
        ts_values = []
        if not og.empty:
            ts_values += list(pd.to_datetime(og["ts"], errors="coerce").dropna())
        if not ag.empty:
            ts_values += list(pd.to_datetime(ag["ts"], errors="coerce").dropna())
        if not av.empty:
            ts_values += list(pd.to_datetime(av["ts"], errors="coerce").dropna())
        ts_values = sorted(set(ts_values))

        hist = [score_at(sym, ts, og, ag, av, m) for ts in ts_values]
        hdf = pd.DataFrame(hist)

        # Retain every reconstructed state so it can be persisted independently
        # of the current dashboard row. Re-running safely updates the same keys.
        if not hdf.empty:
            trading_date = u.get("trading_date")
            if pd.isna(trading_date):
                trading_date = pd.Timestamp(hdf["ts"].iloc[0]).tz_convert(IST).date()
            else:
                trading_date = pd.Timestamp(trading_date).date()
            history_copy = hdf.copy()
            history_copy["trading_date"] = trading_date
            history_copy["money_flow_rank"] = u.get("rank")
            history_copy["symbol"] = sym
            history_copy["bull_score"] = history_copy["bull"]
            history_copy["bear_score"] = history_copy["bear"]
            history_copy["absorption_flag"] = history_copy["absorption"]
            history_copy["minutes_2_to_4"] = (
                pd.to_numeric(m.get("minutes_2_to_4"), errors="coerce")
                if m is not None else None
            )
            snapshot_history.append(history_copy)

        if hdf.empty:
            current = {
                "state":"NEUTRAL","conviction":"LOW","score":0.0,"direction":"MIXED",
                "bull":0.0,"bear":0.0,"option_bull_score":0,"option_bear_score":0,
                "option_persistence":0,"total_qty_imbalance":None,"imbalance_persistence":0,
                "price_change_3m_pct":None,"oi_change_3m_pct":None,"trade_delta_pct":None,
                "session_price_pct":None,"cumulative_oi_pct":None,
                "normalized_move_units":None,"option_oi_iv_confirmation":None,
                "same_strike_oi_signal":None,"same_strike_oi_points":0.0,"same_strike_oi_persistence":0,
                "same_strike_oi_strike":None,"same_strike_own_change_pct":None,"same_strike_opposite_change_pct":None,
                "relative_strength_pct":None,"volume_percentile":None,
                "extension_status":"UNKNOWN",
                "classified_trade_count":0,"classified_qty":0,"delta_persistence":0,
                "aggression_quality":"NO DATA","order_flow_agreement":"NO DATA"
                ,"avwap_bull_points":0.0,"avwap_bear_points":0.0,
                "avwap_state":"NO DATA","avwap_high":None,"avwap_low":None,
                "hourly_avwap_high":None,"hourly_avwap_low":None,"avwap_source_ts":pd.NaT,
                "price_persistence_points":0.0,"oi_confirmation_points":0.0,
                "futures_state":"UNKNOWN","futures_state_persistence":0,"futures_state_points":0.0,
                "total_flow_3m_cr":None,"money_flow_acceleration_3m_cr":None,
                "money_flow_acceleration_x":None,"money_flow_points":0.0,
                "pcr_trend_9m":None,"pcr_trend_points":0.0,
                "aggression_points":0.0,"imbalance_points":0.0
            }
            peak_state, peak_conviction, peak_score, peak_time = "NEUTRAL","LOW",0.0,pd.NaT
            reversal, reversal_time = False, pd.NaT
        else:
            current = hdf.iloc[-1].to_dict()

            # Peak = highest directional score; warnings/conflict do not override a stronger clean directional state.
            clean = hdf[~hdf["state"].isin(["CONFLICT","SELL ABSORPTION","BUY ABSORPTION"])].copy()
            if clean.empty:
                clean = hdf.copy()
            peak_idx = clean["score"].astype(float).idxmax()
            peak = clean.loc[peak_idx]
            peak_state = peak["state"]
            peak_conviction = peak["conviction"]
            peak_score = float(peak["score"])
            peak_time = peak["ts"]

            # Reversal requires an opposite clean score >=6 to persist for at
            # least three minutes. The previous >=4 single-row rule reacted to
            # transient option/order-flow noise and overcounted reversals.
            reversal = False
            reversal_time = pd.NaT
            established_dir = None
            opposite_dir = None
            opposite_since = pd.NaT
            for _, hr in hdf.iterrows():
                d = hr["direction"]
                sc = float(hr["score"])
                clean_state = hr["state"] not in ("CONFLICT","SELL ABSORPTION","BUY ABSORPTION")
                if d not in ("BULL","BEAR") or sc < 6 or not clean_state:
                    continue
                if established_dir is None:
                    established_dir = d
                    continue
                if d == established_dir:
                    opposite_dir = None
                    opposite_since = pd.NaT
                    continue
                if opposite_dir != d:
                    opposite_dir = d
                    opposite_since = hr["ts"]
                    continue
                elapsed = (pd.Timestamp(hr["ts"])-pd.Timestamp(opposite_since)).total_seconds()/60.0
                if elapsed >= 3:
                    reversal = True
                    reversal_time = hr["ts"]
                    established_dir = d
                    opposite_dir = None
                    opposite_since = pd.NaT
                    break

        current_dir = current.get("direction", "MIXED")
        direction_clues = []
        if current_dir == "BULL":
            if pd.notna(first_bo): direction_clues.append(("OPTIONS BULL", first_bo))
            if pd.notna(first_buy): direction_clues.append(("BUY AGGRESSION", first_buy))
        elif current_dir == "BEAR":
            if pd.notna(first_so): direction_clues.append(("OPTIONS BEAR", first_so))
            if pd.notna(first_sell): direction_clues.append(("SELL AGGRESSION", first_sell))

        first_current_type, first_current_time = ("-", pd.NaT)
        if direction_clues:
            first_current_type, first_current_time = min(direction_clues, key=lambda x: pd.Timestamp(x[1]))

        # Preserve both-side clues for diagnostics.
        first_bull_candidates = [(k,t) for k,t in [("OPTIONS BULL", first_bo),("BUY AGGRESSION", first_buy)] if pd.notna(t)]
        first_bear_candidates = [(k,t) for k,t in [("OPTIONS BEAR", first_so),("SELL AGGRESSION", first_sell)] if pd.notna(t)]
        first_bull_time = min((t for _,t in first_bull_candidates), default=pd.NaT)
        first_bear_time = min((t for _,t in first_bear_candidates), default=pd.NaT)

        result.append({
            "money_flow_rank": u.get("rank"),
            "symbol": sym,

            "current_state": current.get("state"),
            "current_conviction": current.get("conviction"),
            "current_score": round(float(current.get("score",0)),1),
            "current_direction": current.get("direction"),
            "bull_score": round(float(current.get("bull",0)),1),
            "bear_score": round(float(current.get("bear",0)),1),

            "peak_state_today": peak_state,
            "peak_conviction_today": peak_conviction,
            "peak_score_today": round(float(peak_score),1),
            "peak_state_time": peak_time,

            "option_bull_score": current.get("option_bull_score",0),
            "option_bear_score": current.get("option_bear_score",0),
            "option_persistence": current.get("option_persistence",0),
            "total_qty_imbalance": current.get("total_qty_imbalance"),
            "imbalance_persistence": current.get("imbalance_persistence",0),
            "price_change_3m_pct": current.get("price_change_3m_pct"),
            "oi_change_3m_pct": current.get("oi_change_3m_pct"),
            "session_price_pct": current.get("session_price_pct"),
            "cumulative_oi_pct": current.get("cumulative_oi_pct"),
            "normalized_move_units": current.get("normalized_move_units"),
            "option_oi_iv_confirmation": current.get("option_oi_iv_confirmation"),
            "same_strike_oi_signal": current.get("same_strike_oi_signal"),
            "same_strike_oi_points": current.get("same_strike_oi_points",0),
            "same_strike_oi_persistence": current.get("same_strike_oi_persistence",0),
            "same_strike_oi_strike": current.get("same_strike_oi_strike"),
            "same_strike_own_change_pct": current.get("same_strike_own_change_pct"),
            "same_strike_opposite_change_pct": current.get("same_strike_opposite_change_pct"),
            "relative_strength_pct": current.get("relative_strength_pct"),
            "volume_percentile": current.get("volume_percentile"),
            "extension_status": current.get("extension_status","UNKNOWN"),
            "trade_delta_pct": current.get("trade_delta_pct"),
            "classified_trade_count": current.get("classified_trade_count",0),
            "classified_qty": current.get("classified_qty",0),
            "delta_persistence": current.get("delta_persistence",0),
            "aggression_quality": current.get("aggression_quality","NO DATA"),
            "order_flow_agreement": current.get("order_flow_agreement","NO DATA"),
            "avwap_bull_points": current.get("avwap_bull_points",0),
            "avwap_bear_points": current.get("avwap_bear_points",0),
            "avwap_state": current.get("avwap_state","NO DATA"),
            "avwap_high": current.get("avwap_high"),
            "avwap_low": current.get("avwap_low"),
            "hourly_avwap_high": current.get("hourly_avwap_high"),
            "hourly_avwap_low": current.get("hourly_avwap_low"),
            "avwap_source_ts": current.get("avwap_source_ts"),
            "price_persistence_points": current.get("price_persistence_points",0),
            "oi_confirmation_points": current.get("oi_confirmation_points",0),
            "futures_state": current.get("futures_state","UNKNOWN"),
            "futures_state_persistence": current.get("futures_state_persistence",0),
            "futures_state_points": current.get("futures_state_points",0),
            "total_flow_3m_cr": current.get("total_flow_3m_cr"),
            "money_flow_acceleration_3m_cr": current.get("money_flow_acceleration_3m_cr"),
            "money_flow_acceleration_x": current.get("money_flow_acceleration_x"),
            "money_flow_points": current.get("money_flow_points",0),
            "pcr_trend_9m": current.get("pcr_trend_9m"),
            "pcr_trend_points": current.get("pcr_trend_points",0),
            "aggression_points": current.get("aggression_points",0),
            "imbalance_points": current.get("imbalance_points",0),

            "minutes_2_to_4": pd.to_numeric(m.get("minutes_2_to_4"), errors="coerce") if m is not None else None,
            "time_2pct": m.get("time_2pct") if m is not None else pd.NaT,
            "time_4pct": m.get("time_4pct") if m is not None else pd.NaT,

            "first_current_direction_clue": first_current_type,
            "first_current_direction_time": first_current_time,
            "first_bull_clue_time": first_bull_time,
            "first_bear_clue_time": first_bear_time,

            "reversal": reversal,
            "reversal_time": reversal_time,
            "absorption_flag": current.get("absorption")
        })

    result_df = pd.DataFrame(result)
    result_df.attrs["snapshot_history"] = (
        pd.concat(snapshot_history, ignore_index=True)
        if snapshot_history else pd.DataFrame()
    )
    return result_df


def build_fast_reversal_events(history_df):
    """Detect strong-score invalidations and meaningful opposite-direction flips.

    A strong run begins at a clean score of at least 8. It is considered rapidly
    invalidated when the original directional component falls below 4 within the
    next two canonical three-minute observations. An opposite direction must then
    reach a clean score of at least 4 within six observations (18 minutes).
    """
    if history_df is None or history_df.empty:
        return pd.DataFrame()

    h = history_df.copy()
    h["ts"] = pd.to_datetime(h["ts"], utc=True, errors="coerce")
    h = h.dropna(subset=["ts", "symbol"])
    h["bucket"] = h["ts"].dt.floor("3min")
    h = (
        h.sort_values("ts")
        .groupby(["symbol", "bucket"], as_index=False)
        .tail(1)
        .sort_values(["symbol", "ts"])
    )

    excluded_states = {"CONFLICT", "SELL ABSORPTION", "BUY ABSORPTION"}
    events = []

    for symbol, group in h.groupby("symbol", sort=False):
        g = group.reset_index(drop=True)
        strong = (
            pd.to_numeric(g["score"], errors="coerce").fillna(0).ge(8)
            & g["direction"].isin(["BULL", "BEAR"])
            & ~g["state"].isin(excluded_states)
        )
        positions = list(g.index[strong])
        if not positions:
            continue

        runs = []
        run = [positions[0]]
        for pos in positions[1:]:
            if pos == run[-1] + 1 and g.loc[pos, "direction"] == g.loc[run[-1], "direction"]:
                run.append(pos)
            else:
                runs.append(run)
                run = [pos]
        runs.append(run)

        consumed_until = -1
        for run in runs:
            if run[0] <= consumed_until:
                continue
            peak_pos = max(run, key=lambda p: float(g.loc[p, "score"]))
            peak = g.loc[peak_pos]
            peak_direction = peak["direction"]
            component_col = "bull" if peak_direction == "BULL" else "bear"
            opposite = "BEAR" if peak_direction == "BULL" else "BULL"
            run_end = run[-1]

            invalidation_pos = None
            for pos in range(run_end + 1, min(len(g), run_end + 3)):
                component = pd.to_numeric(g.loc[pos, component_col], errors="coerce")
                if pd.isna(component) or component < 4:
                    invalidation_pos = pos
                    break
            if invalidation_pos is None:
                continue

            reversal_pos = None
            for pos in range(invalidation_pos, min(len(g), invalidation_pos + 7)):
                row = g.loc[pos]
                if (
                    row["direction"] == opposite
                    and float(row["score"]) >= 4
                    and row["state"] not in excluded_states
                ):
                    reversal_pos = pos
                    break

            invalidation = g.loc[invalidation_pos]
            reversal = g.loc[reversal_pos] if reversal_pos is not None else None
            minutes_to_invalidation = (
                invalidation["ts"] - peak["ts"]
            ).total_seconds() / 60.0
            minutes_to_reversal = (
                (reversal["ts"] - peak["ts"]).total_seconds() / 60.0
                if reversal is not None else None
            )

            events.append({
                "money_flow_rank": peak.get("money_flow_rank"),
                "symbol": symbol,
                "event": "FAST REVERSAL" if reversal is not None else "PEAK INVALIDATED",
                "peak_direction": peak_direction,
                "peak_state": peak["state"],
                "peak_score": float(peak["score"]),
                "peak_time": peak["ts"],
                "invalidation_state": invalidation["state"],
                "invalidation_score": float(invalidation["score"]),
                "invalidation_time": invalidation["ts"],
                "minutes_to_invalidation": round(minutes_to_invalidation, 1),
                "reversal_direction": opposite if reversal is not None else "-",
                "reversal_state": reversal["state"] if reversal is not None else "-",
                "reversal_score": float(reversal["score"]) if reversal is not None else None,
                "reversal_time": reversal["ts"] if reversal is not None else pd.NaT,
                "minutes_peak_to_reversal": round(minutes_to_reversal, 1) if minutes_to_reversal is not None else None,
            })
            if reversal_pos is not None:
                consumed_until = reversal_pos

    return pd.DataFrame(events)

# ============================================================
# UI
# ============================================================

st.title(f"Top {MONEY_FLOW_TOP_N} Money Flow — Early Detector v2.9")
st.caption("State + Conviction • Options → Executed Delta → Order Book → Price Response → Futures OI → Acceleration.")

universe = load_universe()
latest = build_master_score(load_latest_snapshots())
milestones = load_oi_milestones() if not universe.empty else pd.DataFrame()
spurt_stats = load_oi_spurt_stats() if not universe.empty else pd.DataFrame()
option_activity = load_option_activity_stats() if not universe.empty else pd.DataFrame()
dominant_options = load_latest_option_contracts() if not universe.empty else pd.DataFrame()
spurt_dominant_options = load_dominant_option_at_first_spurt() if not universe.empty else pd.DataFrame()
option_doubles = load_first_option_doubles() if not universe.empty else pd.DataFrame()
v2_option_baskets = load_v2_option_baskets() if not universe.empty else pd.DataFrame()
v2_aggression = load_v2_aggression() if not universe.empty else pd.DataFrame()
v2_volatility = load_v2_realized_volatility() if not universe.empty else pd.DataFrame()
avwap_history = load_avwap_history() if not universe.empty else pd.DataFrame()
zone_aggression_signals = load_zone_aggression_signals() if not universe.empty else pd.DataFrame()
aggression_sequences = load_aggression_followthrough_sequences() if not universe.empty else pd.DataFrame()
v2_board = build_v2_state(
    universe, v2_option_baskets, v2_aggression, milestones, v2_volatility,
    avwap_history
) if not universe.empty else pd.DataFrame()
reversal_events = build_fast_reversal_events(
    v2_board.attrs.get("snapshot_history", pd.DataFrame())
) if not v2_board.empty else pd.DataFrame()

if not v2_board.empty:
    try:
        persisted_rows = persist_early_detector_snapshots(
            v2_board.attrs.get("snapshot_history", pd.DataFrame())
        )
    except Exception as exc:
        st.warning(f"Early Detector score history could not be saved: {exc}")

if not latest.empty and not universe.empty:
    baseline = universe[["symbol", "spot_price", "future_price", "future_oi", "futures_value_cr"]].rename(
        columns={"spot_price":"spot_930", "future_price":"future_930", "future_oi":"oi_930", "futures_value_cr":"futures_value_930_cr"}
    )
    latest = latest.merge(baseline, on="symbol", how="left")
    if not milestones.empty:
        keep = ["symbol","time_2pct","oi_2pct","future_2pct","future_oi_2pct",
                "time_4pct","oi_4pct","future_4pct","future_oi_4pct",
                "time_8pct","oi_8pct","future_8pct","future_oi_8pct",
                "minutes_2_to_4","minutes_4_to_8"]
        latest = latest.merge(milestones[[c for c in keep if c in milestones.columns]], on="symbol", how="left")
    if not spurt_stats.empty:
        latest = latest.merge(spurt_stats, on="symbol", how="left")
    if not option_activity.empty:
        latest = latest.merge(option_activity, on="symbol", how="left")
    if not dominant_options.empty:
        latest = latest.merge(dominant_options, on="symbol", how="left")
    if not spurt_dominant_options.empty:
        latest = latest.merge(spurt_dominant_options, on="symbol", how="left")
    if not option_doubles.empty:
        latest = latest.merge(option_doubles, on="symbol", how="left")
    latest = build_early_detector(latest)

if universe.empty:
    st.warning(
        "No frozen money-flow universe is available yet. "
        f"Run the money-flow collector on the next trading day; the Top {MONEY_FLOW_TOP_N} should freeze around {FREEZE_LABEL} IST."
    )
    st.stop()

if len(universe) != MONEY_FLOW_TOP_N or universe["symbol"].nunique() != MONEY_FLOW_TOP_N:
    st.error(
        f"Frozen universe verification failed: received {len(universe)} rows and "
        f"{universe['symbol'].nunique()} unique symbols; expected exactly {MONEY_FLOW_TOP_N}."
    )
    st.stop()

latest_date = universe["trading_date"].max()
freeze_ts = universe["freeze_ts"].iloc[0] if "freeze_ts" in universe.columns else None

c1, c2, c3 = st.columns(3)
c1.metric("Universe date", str(latest_date))
c2.metric("Frozen stocks", len(universe))
c3.metric("Freeze time", ts_ist(freeze_ts))

if latest.empty:
    st.info(
        f"The Top {MONEY_FLOW_TOP_N} universe exists, but no stock-engine snapshots are available yet. "
        "Once the 3-minute collector starts writing, the two dashboards will populate automatically."
    )

tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
    "v2.2 State", "Master", "OI Detector", "Option Lead",
    "30-Day Monitor", "Liquidity", "Stock detail", "Fast Reversals",
    "Zone + Aggression", "Aggression Sequence", "First-hour AVWAP"
])

# ---------------- v2.1 STATE + CONVICTION + MEMORY ----------------
with tab0:
    if v2_aggression.empty:
        st.warning(
            "Futures aggression is unavailable for the current universe date. "
            "Scores are option-only and should not be compared with fully confirmed scores."
        )
    st.subheader("Early Detector v2.9 — Structure-First Current + Peak State")
    st.caption("Current state shows what is happening now. Peak state remembers the strongest clean intraday signal and when it occurred.")

    if v2_board.empty:
        st.info("Waiting for option-basket and futures-aggression data.")
    else:
        board = v2_board.copy()

        for c in ["peak_state_time","first_current_direction_time","first_bull_clue_time",
                  "first_bear_clue_time","reversal_time","time_2pct","time_4pct",
                  "avwap_source_ts"]:
            if c in board.columns:
                board[c] = board[c].apply(time_ist)

        order = {"VERY HIGH":0, "HIGH":1, "MEDIUM":2, "WARNING":3, "CONFLICT":4, "LOW":5}
        board["_peak_order"] = board["peak_conviction_today"].map(order).fillna(9)
        board = board.sort_values(
            ["_peak_order","peak_score_today","current_score","money_flow_rank"],
            ascending=[True,False,False,True]
        )

        k1,k2,k3,k4 = st.columns(4)
        k1.metric("Peak Very High", int((board["peak_conviction_today"]=="VERY HIGH").sum()))
        k2.metric("Peak High", int((board["peak_conviction_today"]=="HIGH").sum()))
        k3.metric("Reversals", int(board["reversal"].fillna(False).sum()))
        k4.metric("Current Absorption", int(board["absorption_flag"].notna().sum()))

        show = [
            "money_flow_rank","symbol",
            "current_state","current_conviction","current_score",
            "peak_state_today","peak_conviction_today","peak_score_today","peak_state_time",
            "first_current_direction_clue","first_current_direction_time",
            "reversal","reversal_time","absorption_flag",
            "option_bull_score","option_bear_score","option_persistence",
            "total_qty_imbalance","imbalance_persistence",
            "trade_delta_pct","classified_trade_count","classified_qty",
            "delta_persistence","aggression_quality","order_flow_agreement",
            "avwap_state","avwap_bull_points","avwap_bear_points",
            "avwap_high","hourly_avwap_high","avwap_low","hourly_avwap_low","avwap_source_ts",
            "session_price_pct","cumulative_oi_pct",
            "price_persistence_points","oi_confirmation_points",
            "futures_state","futures_state_persistence","futures_state_points",
            "total_flow_3m_cr","money_flow_acceleration_3m_cr","money_flow_acceleration_x","money_flow_points",
            "pcr_trend_9m","pcr_trend_points","aggression_points","imbalance_points",
            "normalized_move_units","extension_status",
            "option_oi_iv_confirmation",
            "same_strike_oi_signal","same_strike_oi_points","same_strike_oi_persistence",
            "same_strike_oi_strike","same_strike_own_change_pct","same_strike_opposite_change_pct",
            "relative_strength_pct","volume_percentile",
            "price_change_3m_pct","oi_change_3m_pct",
            "minutes_2_to_4","time_2pct","time_4pct"
        ]

        st.dataframe(
            board[[c for c in show if c in board.columns]],
            width="stretch",
            hide_index=True,
            column_config={
                "money_flow_rank":"MF Rank",
                "symbol":"Symbol",
                "current_state":"Current State",
                "current_conviction":"Current Conviction",
                "current_score":st.column_config.NumberColumn("Current /10",format="%.1f"),
                "peak_state_today":"Peak State Today",
                "peak_conviction_today":"Peak Conviction",
                "peak_score_today":st.column_config.NumberColumn("Peak /10",format="%.1f"),
                "peak_state_time":"Peak Time",
                "first_current_direction_clue":"First Current-Direction Clue",
                "first_current_direction_time":"First Current-Direction Time",
                "reversal":"Reversal?",
                "reversal_time":"Reversal Time",
                "absorption_flag":"Absorption",
                "option_bull_score":"Opt Bull /6",
                "option_bear_score":"Opt Bear /6",
                "option_persistence":"Opt Persist",
                "total_qty_imbalance":st.column_config.NumberColumn("Qty Imbal %",format="%.1f"),
                "imbalance_persistence":"Agg Persist",
                "trade_delta_pct":st.column_config.NumberColumn("Executed Delta %",format="%.1f"),
                "classified_trade_count":"Classified Trades",
                "classified_qty":"Classified Qty",
                "delta_persistence":"Delta Persist",
                "aggression_quality":"Delta Quality",
                "order_flow_agreement":"Book–Trade Agreement",
                "avwap_state":"First-hour AVWAP",
                "avwap_bull_points":st.column_config.NumberColumn("AVWAP Bull Pts",format="%.1f"),
                "avwap_bear_points":st.column_config.NumberColumn("AVWAP Bear Pts",format="%.1f"),
                "avwap_high":st.column_config.NumberColumn("Continuing AVWAP High",format="%.2f"),
                "hourly_avwap_high":st.column_config.NumberColumn("09:15–10:15 High",format="%.2f"),
                "avwap_low":st.column_config.NumberColumn("Continuing AVWAP Low",format="%.2f"),
                "hourly_avwap_low":st.column_config.NumberColumn("09:15–10:15 Low",format="%.2f"),
                "avwap_source_ts":"AVWAP Time",
                "session_price_pct":st.column_config.NumberColumn(f"Fut vs {FREEZE_LABEL} %",format="%.3f"),
                "cumulative_oi_pct":st.column_config.NumberColumn(f"Cum OI vs {FREEZE_LABEL} %",format="%.3f"),
                "price_persistence_points":st.column_config.NumberColumn("Price Persist /2",format="%.1f"),
                "oi_confirmation_points":st.column_config.NumberColumn("OI Confirm /2",format="%.1f"),
                "futures_state":"Futures State",
                "futures_state_persistence":"State Persist",
                "futures_state_points":st.column_config.NumberColumn("State /2",format="%.1f"),
                "total_flow_3m_cr":st.column_config.NumberColumn("3m Money Flow ₹Cr",format="%.2f"),
                "money_flow_acceleration_3m_cr":st.column_config.NumberColumn("Flow Δ3m ₹Cr",format="%.2f"),
                "money_flow_acceleration_x":st.column_config.NumberColumn("Flow Accel ×",format="%.2f"),
                "money_flow_points":st.column_config.NumberColumn("Flow /1.5",format="%.1f"),
                "pcr_trend_9m":st.column_config.NumberColumn("PCR 9m Trend",format="%.4f"),
                "pcr_trend_points":st.column_config.NumberColumn("PCR /1",format="%.1f"),
                "aggression_points":st.column_config.NumberColumn("Aggression /1",format="%.1f"),
                "imbalance_points":st.column_config.NumberColumn("Imbalance /0.5",format="%.1f"),
                "normalized_move_units":st.column_config.NumberColumn("Vol-normalized move",format="%.2f×"),
                "extension_status":"Extension",
                "option_oi_iv_confirmation":st.column_config.NumberColumn("Option OI/IV confirm",format="%.0f"),
                "same_strike_oi_signal":"09:20 Same-strike OI",
                "same_strike_oi_points":st.column_config.NumberColumn("OI Pair Pts",format="%.1f"),
                "same_strike_oi_persistence":"OI Pair Persist",
                "same_strike_oi_strike":st.column_config.NumberColumn("OI Pair Strike",format="%.0f"),
                "same_strike_own_change_pct":st.column_config.NumberColumn("Unwind % vs 09:20",format="%.1f"),
                "same_strike_opposite_change_pct":st.column_config.NumberColumn("Opposite OI % vs 09:20",format="%.1f"),
                "relative_strength_pct":st.column_config.NumberColumn(f"vs Top-{MONEY_FLOW_TOP_N} median %",format="%.2f"),
                "volume_percentile":st.column_config.NumberColumn("Fut volume percentile",format="%.2f"),
                "price_change_3m_pct":st.column_config.NumberColumn("Fut Price 3m %",format="%.3f"),
                "oi_change_3m_pct":st.column_config.NumberColumn("Fut OI 3m %",format="%.3f"),
                "minutes_2_to_4":st.column_config.NumberColumn("OI 2→4 min",format="%.0f"),
                "time_2pct":"OI 2%",
                "time_4pct":"OI 4%"
            }
        )

        st.markdown("#### v2.9 structure-first scoring logic")
        st.caption(
            "Primary /10 score: Price persistence 2 + Futures OI confirmation 2 + "
            "LONG/SHORT buildup persistence 2 + 3-minute money-flow expansion 1.5 + "
            "PCR rolling trend 1 + executed aggression 1 + quantity imbalance 0.5. "
            "Options, same-strike OI, AVWAP, relative strength and volume remain confirmation diagnostics."
        )
        st.caption(
            "First-hour AVWAP adds at most 1.5 aligned points after 10:15 IST: "
            "0.5 for each continuing high/low AVWAP beyond its frozen first-hour level, "
            "plus 0.5 for an aligned crossing during the latest 30 minutes."
        )
        st.caption("Research dashboard only; state and conviction are analytical labels, not trade recommendations.")

# ---------------- MASTER ----------------
with tab1:
    st.subheader("Master Dashboard")

    if not latest.empty:
        master = latest.sort_values(
            ["master_attention_score", "money_flow_rank"],
            ascending=[False, True]
        ).copy()

        show_cols = [
            "money_flow_rank",
            "symbol",
            "master_status",
            "master_attention_score",
            "confluence",
            "spot",
            "future_oi_change_3m",
            "call_oi_change_3m",
            "put_oi_change_3m",
            "pcr",
            "pcr_change_3m",
            "call_iv",
            "put_iv",
            "call_fresh_value_cr",
            "put_fresh_value_cr",
            "zone_state",
        ]
        show_cols = [c for c in show_cols if c in master.columns]

        formatted = master[show_cols].copy()
        if "master_attention_score" in formatted:
            formatted["master_attention_score"] = formatted["master_attention_score"].round(1)
        if "call_iv" in formatted:
            formatted["call_iv"] = (pd.to_numeric(formatted["call_iv"], errors="coerce") * 100).round(2)
        if "put_iv" in formatted:
            formatted["put_iv"] = (pd.to_numeric(formatted["put_iv"], errors="coerce") * 100).round(2)

        st.dataframe(
            formatted,
            width="stretch",
            hide_index=True,
            column_config={
                "money_flow_rank": "Rank",
                "symbol": "Symbol",
                "master_status": "Status",
                "master_attention_score": st.column_config.ProgressColumn(
                    "Attention",
                    min_value=0,
                    max_value=100,
                    format="%.1f",
                ),
                "confluence": "Confluence",
                "spot": st.column_config.NumberColumn("Spot", format="%.2f"),
                "future_oi_change_3m": "Fut ΔOI 3m",
                "call_oi_change_3m": "Call ΔOI 3m",
                "put_oi_change_3m": "Put ΔOI 3m",
                "pcr": st.column_config.NumberColumn("PCR", format="%.3f"),
                "pcr_change_3m": st.column_config.NumberColumn("PCR Δ3m", format="%.4f"),
                "call_iv": st.column_config.NumberColumn("Call IV %", format="%.2f"),
                "put_iv": st.column_config.NumberColumn("Put IV %", format="%.2f"),
                "call_fresh_value_cr": st.column_config.NumberColumn("Call Fresh ₹Cr", format="%.3f"),
                "put_fresh_value_cr": st.column_config.NumberColumn("Put Fresh ₹Cr", format="%.3f"),
                "zone_state": "Zone",
            }
        )

        top = master.iloc[0]
        st.markdown("#### Highest-attention stock")
        a, b, c, d = st.columns(4)
        a.metric("Symbol", str(top.get("symbol", "-")))
        b.metric("Master", str(top.get("master_status", "-")))
        c.metric("Attention", num(top.get("master_attention_score"), 1, "/100"))
        d.metric("Confluence", str(top.get("confluence", "-")))

        st.markdown(
            '<div class="small-note">'
        f'The Master score is cross-sectional: it compares the current 3-minute activity of the Top {MONEY_FLOW_TOP_N} against one another. '
            'It is not yet a historical probability or trading recommendation.'
            '</div>',
            unsafe_allow_html=True,
        )


# ---------------- EARLY DETECTOR ----------------
with tab2:
    st.subheader("Early Detector v1.3 — OI Spurt")
    st.caption("Study rules: 3m futures OI >=0.50% = SPURT; >=1.00% = STRONG SPURT. Acceleration remains first +2%→+4% in <=30 min. Final progression: WATCH → OI SPURT → ACCELERATING → STRONG.")

    if not latest.empty:
        early = latest.sort_values(["build_priority", "money_flow_rank"], ascending=[True, True]).copy()
        accel_long = int((early["acceleration_state"] == "ACCELERATING LONG").sum())
        accel_short = int((early["acceleration_state"] == "ACCELERATING SHORT").sum())
        current_spurts = int((pd.to_numeric(early.get("current_spurt_pct"), errors="coerce") >= OI_SPURT_PCT).sum())
        strong_current_spurts = int((pd.to_numeric(early.get("current_spurt_pct"), errors="coerce") >= OI_STRONG_SPURT_PCT).sum())
        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Current OI Spurts", current_spurts)
        m2.metric("Strong Spurts", strong_current_spurts)
        m3.metric("Accelerating Long", accel_long)
        m4.metric("Accelerating Short", accel_short)

        active_spurts = early[pd.to_numeric(early.get("current_spurt_pct"), errors="coerce") >= OI_SPURT_PCT].copy()
        if not active_spurts.empty:
            st.markdown("### ⚡ Live OI Spurt highlights")
            active_spurts = active_spurts.sort_values("current_spurt_pct", ascending=False)
            for _, r in active_spurts.head(8).iterrows():
                st.markdown(
                    f"**#{r.get('money_flow_rank','-')} {r.get('symbol','-')} — {r.get('current_spurt_state','-')}**  "
                    f"3m OI: **{num(r.get('current_spurt_pct'),3,'%')}** · "
                    f"Fut vs {FREEZE_LABEL}: **{num(r.get('spurt_future_vs_930_pct'),2,'%')}** · "
                    f"Spurts today: **{integer(r.get('spurt_count'))}** · "
                    f"Max: **{num(r.get('max_spurt_pct'),3,'%')}** · "
                    f"First: **{time_ist(r.get('first_spurt_time'))}**"
                )

        active_accel = early[early["acceleration_state"].isin(["ACCELERATING LONG", "ACCELERATING SHORT"])]
        if not active_accel.empty:
            st.markdown("### 🚨 Acceleration highlights")
            for _,r in active_accel.iterrows():
                border = "#2ea043" if "LONG" in r["acceleration_state"] else "#d1242f"
                html = (
                    f'<div style="border:2px solid {border};border-radius:14px;padding:12px 14px;margin:8px 0;">'
                    f'<b>{r.get("build_state","-")} — #{r.get("money_flow_rank","-")} {r.get("symbol","-")}</b><br>'
                    f'Detected: <b>{time_ist(r.get("time_4pct"))} IST</b> &nbsp;|&nbsp; '
                    f'2% crossed: <b>{time_ist(r.get("time_2pct"))}</b> &nbsp;|&nbsp; '
                    f'4% crossed: <b>{time_ist(r.get("time_4pct"))}</b><br>'
                    f'2→4 OI: <b>{num(r.get("minutes_2_to_4"),0," min")}</b> &nbsp;|&nbsp; '
                    f'Fut @4%: <b>{num(r.get("future_move_at_4pct"),2,"%")}</b> &nbsp;|&nbsp; '
                    f'Exposure @4%: <b>{num(r.get("exposure_at_4pct_cr"),2," Cr")}</b><br>'
                    f'Current OI vs T0: <b>{num(r.get("future_oi_change_pct_t0"),2,"%")}</b> &nbsp;|&nbsp; '
                    f'Current Fut vs {FREEZE_LABEL}: <b>{num(r.get("future_price_change_from_930_pct"),2,"%")}</b> &nbsp;|&nbsp; '
                    f'Zone: <b>{r.get("zone_state","-")}</b></div>'
                )
                st.markdown(html, unsafe_allow_html=True)
        else:
            st.info("No Accelerating Long/Short state has qualified yet on the latest trading day.")

        cols = ["money_flow_rank","symbol","build_state",
                "current_option_acceleration_x","current_option_fresh_3m_cr","first_option_lead_time",
                "dominant_option_type","dominant_strike","dominant_wing_no","dominant_price_multiple","dominant_oi_change_3m",
                "current_spurt_state","current_spurt_pct","spurt_count","max_spurt_pct","first_spurt_time",
                "time_4pct","time_2pct","minutes_2_to_4","first_option_2x_time",
                "option_lead_to_spurt_min","spurt_to_accel_min","accel_to_option_2x_min",
                "oi_stage","future_oi_change_pct_t0","future_price_change_from_930_pct",
                "futures_exposure_change_cr","minutes_4_to_8","future_move_at_4pct",
                "exposure_at_4pct_cr","zone_state","next_zone"]
        view = early[[c for c in cols if c in early.columns]].copy()
        if "time_4pct" in view:
            view["time_4pct"] = view["time_4pct"].apply(time_ist)
        if "time_2pct" in view:
            view["time_2pct"] = view["time_2pct"].apply(time_ist)
        if "first_spurt_time" in view:
            view["first_spurt_time"] = view["first_spurt_time"].apply(time_ist)
        if "first_option_lead_time" in view:
            view["first_option_lead_time"] = view["first_option_lead_time"].apply(time_ist)
        if "first_option_2x_time" in view:
            view["first_option_2x_time"] = view["first_option_2x_time"].apply(time_ist)
        for c in ["current_option_acceleration_x","current_option_fresh_3m_cr","dominant_price_multiple",
                  "current_spurt_pct","max_spurt_pct","future_oi_change_pct_t0","future_price_change_from_930_pct","futures_exposure_change_cr",
                  "minutes_2_to_4","minutes_4_to_8","future_move_at_4pct","exposure_at_4pct_cr",
                  "option_lead_to_spurt_min","spurt_to_accel_min","accel_to_option_2x_min"]:
            if c in view: view[c] = pd.to_numeric(view[c], errors="coerce").round(2)
        st.markdown(f"### All Top-{MONEY_FLOW_TOP_N} stocks")
        st.dataframe(view, width="stretch", hide_index=True, column_config={
            "money_flow_rank":"Rank", "symbol":"Symbol", "build_state":"Build State",
            "current_option_acceleration_x":st.column_config.NumberColumn("Option Accel ×",format="%.2f"),
            "current_option_fresh_3m_cr":st.column_config.NumberColumn("Option Fresh 3m ₹Cr",format="%.2f"),
            "first_option_lead_time":"Option Lead Time",
            "dominant_option_type":"Dominant Type",
            "dominant_strike":st.column_config.NumberColumn("Dominant Strike",format="%.0f"),
            "dominant_wing_no":st.column_config.NumberColumn("Wing",format="%.0f"),
            "dominant_price_multiple":st.column_config.NumberColumn("Dominant LTP ×",format="%.2f"),
            "dominant_oi_change_3m":st.column_config.NumberColumn("Dominant ΔOI 3m",format="%.0f"),
            "current_spurt_state":"Current Spurt",
            "current_spurt_pct":st.column_config.NumberColumn("3m OI Spurt",format="%.3f%%"),
            "spurt_count":st.column_config.NumberColumn("Spurt Count",format="%.0f"),
            "max_spurt_pct":st.column_config.NumberColumn("Max Spurt",format="%.3f%%"),
            "first_spurt_time":"First Spurt",
            "time_4pct":"Detection Time", "time_2pct":"2% Time", "first_option_2x_time":"First Option 2×",
            "option_lead_to_spurt_min":st.column_config.NumberColumn("Lead→Spurt min",format="%.0f"),
            "spurt_to_accel_min":st.column_config.NumberColumn("Spurt→Accel min",format="%.0f"),
            "accel_to_option_2x_min":st.column_config.NumberColumn("Accel→2× min",format="%.0f"),
            "oi_stage":"OI Stage",
            "future_oi_change_pct_t0":st.column_config.NumberColumn(f"OI vs {FREEZE_LABEL}",format="%.2f%%"),
            "future_price_change_from_930_pct":st.column_config.NumberColumn(f"Fut vs {FREEZE_LABEL}",format="%.2f%%"),
            "futures_exposure_change_cr":st.column_config.NumberColumn("Exposure Δ ₹Cr",format="%.2f"),
            "minutes_2_to_4":st.column_config.NumberColumn("2→4 min",format="%.0f"),
            "minutes_4_to_8":st.column_config.NumberColumn("4→8 min",format="%.0f"),
            "future_move_at_4pct":st.column_config.NumberColumn("Fut @4%",format="%.2f%%"),
            "exposure_at_4pct_cr":st.column_config.NumberColumn("Exposure @4% ₹Cr",format="%.2f"),
            "zone_state":"Zone", "next_zone":"Next Zone"
        })
        st.markdown('<div class="small-note">OI Spurt v1.0 threshold is frozen at 0.50% (strong at 1.00%) for the one-month study. The existing 2→4 acceleration rule is unchanged.</div>', unsafe_allow_html=True)

# ---------------- OPTION LEAD ----------------
with tab3:
    st.subheader("Option Lead — Frozen 3CE + 3PE")
    st.caption(
        "Research view only. Aggregate option acceleration uses Call Fresh + Put Fresh from the stock engine. "
        "The dominant exact contract is selected from today's frozen six-option table by |3m ΔOI × LTP|. "
        "That is an approximate intensity ranking, not literal cash flow."
    )

    if latest.empty:
        st.info("No current stock snapshots are available yet.")
    elif dominant_options.empty:
        st.info(f"The individual option table exists but has no rows yet. It should populate after the collector freezes today's Top-{MONEY_FLOW_TOP_N} and begins 3-minute writes.")
    else:
        opt = latest.sort_values(["current_option_acceleration_x","money_flow_rank"], ascending=[False,True]).copy()

        lead_now = int((opt.get("current_option_lead_flag", pd.Series(False, index=opt.index)).fillna(False) == True).sum())
        doubled_now = int((opt.get("dominant_doubled", pd.Series(False, index=opt.index)).fillna(False) == True).sum())
        q1,q2,q3,q4 = st.columns(4)
        q1.metric("Research Option Leads now", lead_now)
        q2.metric("Stocks with a 2× option", int(opt.get("first_option_2x_time", pd.Series(dtype=object)).notna().sum()))
        q3.metric("Dominant option doubled now", doubled_now)
        q4.metric("Frozen option stocks", dominant_options["symbol"].nunique())

        st.markdown("### Current option activity + dominant contract")
        ocols = [
            "money_flow_rank","symbol","current_option_acceleration_x","current_option_fresh_3m_cr",
            "call_fresh_15m_cr","put_fresh_15m_cr","first_option_lead_time","option_lead_count",
            "dominant_option_type","dominant_strike","dominant_wing_no","dominant_ltp","dominant_opening_ltp",
            "dominant_price_multiple","dominant_oi_change_3m","dominant_iv",
            "spurt_dominant_option_type","spurt_dominant_strike","spurt_dominant_wing_no",
            "spurt_dominant_price_multiple","spurt_dominant_oi_change_3m","first_option_2x_time",
            "first_2x_option_type","first_2x_strike","first_2x_multiple","first_spurt_time","time_4pct",
            "option_lead_to_spurt_min","spurt_to_accel_min","accel_to_option_2x_min"
        ]
        ov = opt[[c for c in ocols if c in opt.columns]].copy()
        for c in ["first_option_lead_time","first_option_2x_time","first_spurt_time","time_4pct"]:
            if c in ov: ov[c] = ov[c].apply(time_ist)
        for c in ["current_option_acceleration_x","current_option_fresh_3m_cr","call_fresh_15m_cr","put_fresh_15m_cr",
                  "dominant_ltp","dominant_opening_ltp","dominant_price_multiple","dominant_iv",
                  "spurt_dominant_price_multiple","first_2x_multiple",
                  "option_lead_to_spurt_min","spurt_to_accel_min","accel_to_option_2x_min"]:
            if c in ov: ov[c] = pd.to_numeric(ov[c], errors="coerce").round(2)
        st.dataframe(ov, width="stretch", hide_index=True, column_config={
            "money_flow_rank":"Rank", "symbol":"Symbol",
            "current_option_acceleration_x":st.column_config.NumberColumn("Option Accel ×",format="%.2f"),
            "current_option_fresh_3m_cr":st.column_config.NumberColumn("Fresh 3m ₹Cr",format="%.2f"),
            "call_fresh_15m_cr":st.column_config.NumberColumn("Call Fresh 15m ₹Cr",format="%.2f"),
            "put_fresh_15m_cr":st.column_config.NumberColumn("Put Fresh 15m ₹Cr",format="%.2f"),
            "first_option_lead_time":"Option Lead", "option_lead_count":"Lead Count",
            "dominant_option_type":"Dominant", "dominant_strike":"Strike", "dominant_wing_no":"Wing",
            "dominant_ltp":st.column_config.NumberColumn("LTP",format="%.2f"),
            "dominant_opening_ltp":st.column_config.NumberColumn("Open LTP",format="%.2f"),
            "dominant_price_multiple":st.column_config.NumberColumn("LTP ×",format="%.2f"),
            "dominant_oi_change_3m":st.column_config.NumberColumn("ΔOI 3m",format="%.0f"),
            "dominant_iv":st.column_config.NumberColumn("IV",format="%.3f"),
            "spurt_dominant_option_type":"At Spurt Type",
            "spurt_dominant_strike":"At Spurt Strike",
            "spurt_dominant_wing_no":"At Spurt Wing",
            "spurt_dominant_price_multiple":st.column_config.NumberColumn("At Spurt LTP ×",format="%.2f"),
            "spurt_dominant_oi_change_3m":st.column_config.NumberColumn("At Spurt ΔOI",format="%.0f"),
            "first_option_2x_time":"First 2×", "first_2x_option_type":"2× Type", "first_2x_strike":"2× Strike",
            "first_2x_multiple":st.column_config.NumberColumn("2× Multiple",format="%.2f"),
            "first_spurt_time":"OI Spurt", "time_4pct":"Acceleration",
            "option_lead_to_spurt_min":st.column_config.NumberColumn("Lead→Spurt min",format="%.0f"),
            "spurt_to_accel_min":st.column_config.NumberColumn("Spurt→Accel min",format="%.0f"),
            "accel_to_option_2x_min":st.column_config.NumberColumn("Accel→2× min",format="%.0f"),
        })

        st.markdown(
            '<div class="small-note">Research Option Lead is observational only: current 3-minute aggregate option fresh value ≥ ₹0.50Cr and ≥2× its previous-five-snapshot average. It does not alter the OI-spurt or acceleration signal.</div>',
            unsafe_allow_html=True,
        )

# ---------------- 30-DAY MONITOR ----------------
with tab4:
    st.subheader("30-Day Early Detector Monitor")
    st.caption("Reconstructed from existing Neon snapshots; no Railway collector change required.")
    hist_events = load_early_detector_history(31)
    if hist_events.empty:
        st.info("No historical +2% to +4% events are available yet.")
    else:
        al = hist_events[hist_events["detector_state"] == "ACCELERATING LONG"]
        ash = hist_events[hist_events["detector_state"] == "ACCELERATING SHORT"]
        h1,h2,h3,h4 = st.columns(4)
        h1.metric("Accelerating Long events", len(al))
        h2.metric("Accelerating Short events", len(ash))
        h3.metric("Trading days", hist_events["trading_date"].nunique())
        h4.metric("Total 4% crossings", len(hist_events))
        only_accel = hist_events[hist_events["detector_state"] != "NO ACCELERATION"].copy()
        only_accel["detection_time"] = only_accel["time_4pct"].apply(time_ist)
        only_accel["time_2pct_display"] = only_accel["time_2pct"].apply(time_ist)
        only_accel["time_4pct_display"] = only_accel["time_4pct"].apply(time_ist)
        only_accel["time_8pct_display"] = only_accel["time_8pct"].apply(time_ist)
        dcols = ["trading_date","money_flow_rank","symbol","detector_state","detection_time",
                 "time_2pct_display","time_4pct_display","minutes_2_to_4","oi_4pct",
                 "future_move_4pct","exposure_change_4pct_cr","zone_4pct","time_8pct_display"]
        st.dataframe(only_accel[[c for c in dcols if c in only_accel.columns]], width="stretch", hide_index=True, column_config={
            "detector_state":"Acceleration", "detection_time":"Detection Time",
            "time_2pct_display":"2% Time", "time_4pct_display":"4% Time",
            "time_8pct_display":"8% Time", "minutes_2_to_4":"2→4 min",
            "oi_4pct":st.column_config.NumberColumn("OI @4%",format="%.2f%%"),
            "future_move_4pct":st.column_config.NumberColumn("Fut @4%",format="%.2f%%"),
            "exposure_change_4pct_cr":st.column_config.NumberColumn("Exposure @4% ₹Cr",format="%.2f"),
            "zone_4pct":"Zone @4%"
        })

# ---------------- LIQUIDITY ----------------
with tab5:
    st.subheader("Liquidity Dashboard")

    if not latest.empty:
        liq = latest.sort_values("money_flow_rank").copy()

        for _, row in liq.iterrows():
            symbol = row.get("symbol", "-")
            rank = row.get("money_flow_rank", "-")
            zone = row.get("zone_state", "-")
            nxt = row.get("next_zone", "-")
            spot = row.get("spot")
            dpoc = row.get("dpoc")
            basis = row.get("future_basis")

            st.markdown(
                f"""
                <div class="stock-card">
                    <b>#{rank} {symbol}</b><br>
                    Spot: <b>{num(spot,2)}</b> &nbsp; | &nbsp;
                    DPOC: <b>{num(dpoc,2)}</b> &nbsp; | &nbsp;
                    Fut basis: <b>{num(basis,2)}</b><br>
                    Current zone: <b>{zone}</b><br>
                    Next zone: <b>{nxt}</b>
                </div>
                """,
                unsafe_allow_html=True,
            )

# ---------------- DETAIL ----------------
with tab6:
    st.subheader("Stock detail")

    symbols = universe.sort_values("rank")["symbol"].tolist()
    selected = st.selectbox("Select stock", symbols)

    selected_row = latest[latest["symbol"] == selected] if not latest.empty else pd.DataFrame()

    if not selected_row.empty:
        r = selected_row.iloc[0]

        st.markdown(f"### #{r.get('money_flow_rank','-')} {selected}")

        x1, x2, x3, x4 = st.columns(4)
        x1.metric("Spot", num(r.get("spot"), 2))
        x2.metric("Future", num(r.get("future"), 2))
        x3.metric("PCR", num(r.get("pcr"), 3))
        x4.metric("Zone", str(r.get("zone_state", "-")))

        y1, y2, y3, y4 = st.columns(4)
        y1.metric("Fut ΔOI 3m", integer(r.get("future_oi_change_3m")))
        y2.metric("Call ΔOI 3m", integer(r.get("call_oi_change_3m")))
        y3.metric("Put ΔOI 3m", integer(r.get("put_oi_change_3m")))
        y4.metric("Next zone", str(r.get("next_zone", "-")))

        sp1,sp2,sp3,sp4 = st.columns(4)
        sp1.metric("3m OI Spurt", num(r.get("current_spurt_pct"),3,"%"))
        sp2.metric("Spurt State", str(r.get("current_spurt_state","-")))
        sp3.metric("Spurt Count", integer(r.get("spurt_count")))
        sp4.metric("First Spurt", time_ist(r.get("first_spurt_time")))

        op1,op2,op3,op4 = st.columns(4)
        dom_label = "-"
        if pd.notna(r.get("dominant_strike")):
            dom_label = f"{int(float(r.get('dominant_strike')))} {r.get('dominant_option_type','')}"
        op1.metric("Option Accel ×", num(r.get("current_option_acceleration_x"),2))
        op2.metric("Option Fresh 3m ₹Cr", num(r.get("current_option_fresh_3m_cr"),2))
        op3.metric("Dominant Option", dom_label)
        op4.metric("Dominant LTP ×", num(r.get("dominant_price_multiple"),2))

        z1, z2, z3, z4 = st.columns(4)
        z1.metric("Call IV", iv_pct(r.get("call_iv")))
        z2.metric("Put IV", iv_pct(r.get("put_iv")))
        z3.metric("Call fresh ₹Cr", num(r.get("call_fresh_value_cr"), 3))
        z4.metric("Put fresh ₹Cr", num(r.get("put_fresh_value_cr"), 3))

        hist = load_symbol_history(selected, 40)

        if not hist.empty:
            hist = hist.copy()
            hist["IST"] = pd.to_datetime(hist["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
            plot = hist.set_index("IST")

            if "spot" in plot:
                st.markdown("#### Spot — recent 3-minute history")
                st.line_chart(plot[["spot"]], width="stretch")

            if {"call_iv", "put_iv"}.issubset(plot.columns):
                iv_plot = plot[["call_iv", "put_iv"]].apply(pd.to_numeric, errors="coerce") * 100
                st.markdown("#### IV — recent 3-minute history")
                st.line_chart(iv_plot, width="stretch")

            if {"future_oi", "future_oi_change_3m"}.issubset(hist.columns):
                prev_oi = pd.to_numeric(hist["future_oi"], errors="coerce") - pd.to_numeric(hist["future_oi_change_3m"], errors="coerce")
                hist["oi_spurt_3m_pct"] = (pd.to_numeric(hist["future_oi_change_3m"], errors="coerce") / prev_oi.replace(0, pd.NA)) * 100.0

            history_cols = [
                "ts",
                "spot",
                "future_basis",
                "future_oi_change_3m",
                "oi_spurt_3m_pct",
                "call_oi_change_3m",
                "put_oi_change_3m",
                "pcr",
                "pcr_change_3m",
                "zone_state",
                "next_zone",
            ]
            history_cols = [c for c in history_cols if c in hist.columns]
            st.markdown("#### Recent snapshots")
            st.dataframe(
                hist[history_cols].sort_values("ts", ascending=False),
                width="stretch",
                hide_index=True,
            )


        option_hist = load_symbol_option_history(selected, 120)
        if not option_hist.empty:
            option_hist = option_hist.copy()
            option_hist["time_ist"] = option_hist["ts"].apply(time_ist)
            show_opt = ["time_ist","option_type","wing_no","strike","ltp","opening_ltp","price_multiple","doubled","oi_change_3m","iv"]
            st.markdown("#### Frozen six-option history")
            st.dataframe(
                option_hist[[c for c in show_opt if c in option_hist.columns]],
                width="stretch",
                hide_index=True,
                column_config={
                    "time_ist":"Time", "option_type":"Type", "wing_no":"Wing", "strike":"Strike",
                    "ltp":st.column_config.NumberColumn("LTP",format="%.2f"),
                    "opening_ltp":st.column_config.NumberColumn("Open LTP",format="%.2f"),
                    "price_multiple":st.column_config.NumberColumn("Multiple",format="%.2f"),
                    "doubled":"2×", "oi_change_3m":st.column_config.NumberColumn("ΔOI 3m",format="%.0f"),
                    "iv":st.column_config.NumberColumn("IV",format="%.3f"),
                }
            )
    else:
        st.info("No 3-minute snapshot exists yet for this stock.")


# ---------------- FAST REVERSALS ----------------
with tab7:
    st.subheader("Fast Reversals — Strong Peak Invalidation")
    st.caption(
        "Flags a clean score ≥8 whose original directional score falls below 4 within two "
        "three-minute observations. A Fast Reversal additionally requires the opposite direction "
        "to reach a clean score ≥4 within 18 minutes."
    )

    if reversal_events.empty:
        st.info("No strong-peak invalidation or fast reversal has been detected for this trading date.")
    else:
        rev = reversal_events.copy()
        fast_count = int(rev["event"].eq("FAST REVERSAL").sum())
        invalidated_count = int(rev["event"].eq("PEAK INVALIDATED").sum())
        bear_to_bull = int(
            ((rev["event"] == "FAST REVERSAL") &
             (rev["peak_direction"] == "BEAR") &
             (rev["reversal_direction"] == "BULL")).sum()
        )
        bull_to_bear = int(
            ((rev["event"] == "FAST REVERSAL") &
             (rev["peak_direction"] == "BULL") &
             (rev["reversal_direction"] == "BEAR")).sum()
        )

        q1, q2, q3, q4 = st.columns(4)
        q1.metric("Fast reversals", fast_count)
        q2.metric("Peak invalidations", invalidated_count)
        q3.metric("Bear → Bull", bear_to_bull)
        q4.metric("Bull → Bear", bull_to_bear)

        for col in ["peak_time", "invalidation_time", "reversal_time"]:
            rev[col] = rev[col].apply(time_ist)

        show = [
            "money_flow_rank", "symbol", "event",
            "peak_direction", "peak_state", "peak_score", "peak_time",
            "invalidation_state", "invalidation_score", "invalidation_time",
            "minutes_to_invalidation", "reversal_direction", "reversal_state",
            "reversal_score", "reversal_time", "minutes_peak_to_reversal",
        ]
        rev = rev.sort_values(
            ["event", "minutes_peak_to_reversal", "peak_score"],
            ascending=[True, True, False],
            na_position="last",
        )
        st.dataframe(
            rev[show],
            width="stretch",
            hide_index=True,
            column_config={
                "money_flow_rank": "Rank",
                "symbol": "Symbol",
                "event": "Event",
                "peak_direction": "Peak Direction",
                "peak_state": "Peak State",
                "peak_score": st.column_config.NumberColumn("Peak", format="%.1f"),
                "peak_time": "Peak Time",
                "invalidation_state": "Invalidation State",
                "invalidation_score": st.column_config.NumberColumn("After Peak", format="%.1f"),
                "invalidation_time": "Invalidated",
                "minutes_to_invalidation": st.column_config.NumberColumn("Peak→Invalid (min)", format="%.1f"),
                "reversal_direction": "New Direction",
                "reversal_state": "Reversal State",
                "reversal_score": st.column_config.NumberColumn("Reversal Score", format="%.1f"),
                "reversal_time": "Reversal Time",
                "minutes_peak_to_reversal": st.column_config.NumberColumn("Peak→Reversal (min)", format="%.1f"),
            },
        )

        st.caption(
            "Peak Invalidated means the strong direction collapsed quickly but the opposite side "
            "did not yet achieve meaningful confirmation."
        )


# ---------------- ZONE + FUTURES AGGRESSION ----------------
with tab8:
    st.subheader("Zone + Futures Aggression")
    st.caption(
        "Highlights only two aligned conditions: Buy Aggression while price is above Strong "
        "Supply, and Sell Aggression after Weak Demand has broken. Aggression is measured from "
        "futures executions, futures price and futures OI."
    )

    if zone_aggression_signals.empty:
        st.info("No qualifying Zone + Aggression setup has been recorded for this trading date.")
    else:
        signals = zone_aggression_signals.copy()
        signals["ts"] = pd.to_datetime(signals["ts"], errors="coerce", utc=True)

        bullish_name = "ABOVE STRONG SUPPLY + BUY AGGRESSION"
        bearish_name = "BELOW WEAK DEMAND + SELL AGGRESSION"
        bullish = signals[signals["setup"].eq(bullish_name)].copy()
        bearish = signals[signals["setup"].eq(bearish_name)].copy()

        z1, z2, z3 = st.columns(3)
        z1.metric("Bullish setup stocks", int(bullish["symbol"].nunique()))
        z2.metric("Bearish setup stocks", int(bearish["symbol"].nunique()))
        z3.metric("Total qualifying events", int(len(signals)))

        def setup_summary(frame):
            if frame.empty:
                return pd.DataFrame()

            ordered = frame.sort_values(["symbol", "ts"]).copy()
            first = ordered.groupby("symbol", as_index=False).first()
            latest_rows = ordered.groupby("symbol", as_index=False).tail(1).copy()
            counts = ordered.groupby("symbol").size().rename("event_count")
            latest_rows = latest_rows.merge(
                first[["symbol", "ts"]].rename(columns={"ts": "first_signal_ts"}),
                on="symbol", how="left"
            )
            latest_rows = latest_rows.merge(counts, on="symbol", how="left")
            latest_rows["first_signal_time"] = latest_rows["first_signal_ts"].apply(time_ist)
            latest_rows["latest_signal_time"] = latest_rows["ts"].apply(time_ist)
            return latest_rows.sort_values(["money_flow_rank", "symbol"])

        display_columns = [
            "money_flow_rank", "symbol", "first_signal_time", "latest_signal_time",
            "event_count", "spot", "delta_pct", "price_change_3m_pct",
            "oi_change_3m_pct", "classified_trade_count", "zone_state", "next_zone"
        ]
        display_config = {
            "money_flow_rank": "MF Rank", "symbol": "Symbol",
            "first_signal_time": "First Signal", "latest_signal_time": "Latest Signal",
            "event_count": "Events",
            "spot": st.column_config.NumberColumn("Spot", format="%.2f"),
            "delta_pct": st.column_config.NumberColumn("Executed Delta %", format="%.1f"),
            "price_change_3m_pct": st.column_config.NumberColumn("Fut Price 3m %", format="%.3f"),
            "oi_change_3m_pct": st.column_config.NumberColumn("Fut OI 3m %", format="%.3f"),
            "classified_trade_count": "Classified Trades",
            "zone_state": "Zone State", "next_zone": "Next Zone"
        }

        st.markdown("#### Above Strong Supply + Buy Aggression")
        bull_summary = setup_summary(bullish)
        if bull_summary.empty:
            st.info("No bullish aligned setup has been recorded today.")
        else:
            st.dataframe(
                bull_summary[[c for c in display_columns if c in bull_summary.columns]],
                width="stretch", hide_index=True, column_config=display_config
            )

        st.markdown("#### Below Weak Demand + Sell Aggression")
        bear_summary = setup_summary(bearish)
        if bear_summary.empty:
            st.info("No bearish aligned setup has been recorded today.")
        else:
            st.dataframe(
                bear_summary[[c for c in display_columns if c in bear_summary.columns]],
                width="stretch", hide_index=True, column_config=display_config
            )

        st.caption(
            "Each row shows the first and latest occurrence for the stock. Event count is the "
            "number of qualifying three-minute futures-aggression snapshots in the same setup."
        )


# ---------------- AGGRESSION FOLLOW-THROUGH SEQUENCES ----------------
with tab9:
    st.subheader("Aggression Follow-Through Sequence")
    st.caption(
        "Tracks sequence only; liquidity zones are not used. Bullish sequence: first Fresh Buy "
        "Aggression followed by Short Covering. Bearish sequence: first Fresh Sell Aggression "
        "followed by Long Unwinding."
    )

    if aggression_sequences.empty:
        st.info("No qualifying fresh Buy or Sell Aggression has been recorded for this trading date.")
    else:
        seq = aggression_sequences.copy()
        for c in ["start_ts", "follow_ts"]:
            seq[c] = pd.to_datetime(seq[c], errors="coerce", utc=True)
        seq["first_aggression_time"] = seq["start_ts"].apply(time_ist)
        seq["follow_through_time"] = seq["follow_ts"].apply(time_ist)

        buy_name = "BUY AGGRESSION TO SHORT COVERING"
        sell_name = "SELL AGGRESSION TO LONG UNWINDING"
        buy_seq = seq[seq["sequence_type"].eq(buy_name)].copy()
        sell_seq = seq[seq["sequence_type"].eq(sell_name)].copy()

        if not buy_seq.empty:
            buy_seq["sequence_result"] = "NOT PASSED"
            buy_seq.loc[buy_seq["sequence_status"].eq("WAITING"), "sequence_result"] = "WAITING"
            buy_pass = (
                buy_seq["sequence_status"].eq("SEQUENCE COMPLETE")
                & pd.to_numeric(buy_seq["minutes_to_follow"], errors="coerce").le(30)
                & pd.to_numeric(buy_seq["future_move_to_follow_pct"], errors="coerce").gt(0)
            )
            buy_seq.loc[buy_pass, "sequence_result"] = "PASSED"
            buy_seq.loc[
                buy_seq["sequence_status"].eq("SEQUENCE COMPLETE")
                & pd.to_numeric(buy_seq["minutes_to_follow"], errors="coerce").gt(30),
                "sequence_result"
            ] = "NOT PASSED - LATE"
            buy_seq.loc[
                buy_seq["sequence_status"].eq("SEQUENCE COMPLETE")
                & pd.to_numeric(buy_seq["minutes_to_follow"], errors="coerce").le(30)
                & pd.to_numeric(buy_seq["future_move_to_follow_pct"], errors="coerce").le(0),
                "sequence_result"
            ] = "NOT PASSED - NO POSITIVE MOVE"

        passed_buy = int(buy_seq["sequence_result"].eq("PASSED").sum()) if not buy_seq.empty else 0
        not_passed_buy = int(buy_seq["sequence_result"].str.startswith("NOT PASSED").sum()) if not buy_seq.empty else 0
        completed_sell = int(sell_seq["sequence_status"].eq("SEQUENCE COMPLETE").sum())
        waiting_buy = int(buy_seq["sequence_result"].eq("WAITING").sum()) if not buy_seq.empty else 0

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Buy sequence passed", passed_buy)
        s2.metric("Buy sequence not passed", not_passed_buy)
        s3.metric("Buy sequence waiting", waiting_buy)
        s4.metric("Sell sequence complete", completed_sell)

        sequence_columns = [
            "money_flow_rank", "symbol", "sequence_result", "sequence_status", "timing_bucket",
            "first_aggression_time", "follow_through_time", "minutes_to_follow",
            "start_future", "follow_future", "future_move_to_follow_pct",
            "start_delta_pct", "start_price_change_3m_pct", "start_oi_change_3m_pct",
            "follow_delta_pct", "follow_price_change_3m_pct", "follow_oi_change_3m_pct"
        ]
        sequence_config = {
            "money_flow_rank": "MF Rank", "symbol": "Symbol",
            "sequence_result": "Result",
            "sequence_status": "Status", "timing_bucket": "Timing",
            "first_aggression_time": "First Aggression",
            "follow_through_time": "First Follow-Through",
            "minutes_to_follow": st.column_config.NumberColumn("Minutes", format="%.0f"),
            "start_future": st.column_config.NumberColumn("Future at Aggression", format="%.2f"),
            "follow_future": st.column_config.NumberColumn("Future at Follow-Through", format="%.2f"),
            "future_move_to_follow_pct": st.column_config.NumberColumn("Future Move %", format="%.2f"),
            "start_delta_pct": st.column_config.NumberColumn("Start Delta %", format="%.1f"),
            "start_price_change_3m_pct": st.column_config.NumberColumn("Start Price 3m %", format="%.3f"),
            "start_oi_change_3m_pct": st.column_config.NumberColumn("Start OI 3m %", format="%.3f"),
            "follow_delta_pct": st.column_config.NumberColumn("Follow Delta %", format="%.1f"),
            "follow_price_change_3m_pct": st.column_config.NumberColumn("Follow Price 3m %", format="%.3f"),
            "follow_oi_change_3m_pct": st.column_config.NumberColumn("Follow OI 3m %", format="%.3f")
        }

        st.markdown("#### Passed: Buy Aggression → Short Covering")
        st.caption(
            "Pass rule: Short Covering occurs within 30 minutes and futures remain above the "
            "initial Buy Aggression price."
        )
        if buy_seq.empty:
            st.info("No Fresh Buy Aggression has been recorded today.")
        else:
            passed = buy_seq[buy_seq["sequence_result"].eq("PASSED")].sort_values(
                ["minutes_to_follow", "future_move_to_follow_pct"], ascending=[True, False]
            )
            not_passed = buy_seq[~buy_seq["sequence_result"].eq("PASSED")].sort_values(
                ["sequence_result", "minutes_to_follow"], na_position="last"
            )

            if passed.empty:
                st.info("No stock has passed the Buy Aggression sequence today.")
            else:
                st.dataframe(
                    passed[[c for c in sequence_columns if c in passed.columns]],
                    width="stretch", hide_index=True, column_config=sequence_config
                )

            with st.expander("Not passed / waiting", expanded=False):
                if not_passed.empty:
                    st.info("No failed or waiting Buy Aggression sequence.")
                else:
                    st.dataframe(
                        not_passed[[c for c in sequence_columns if c in not_passed.columns]],
                        width="stretch", hide_index=True, column_config=sequence_config
                    )

        st.markdown("#### Sell Aggression → Long Unwinding")
        if sell_seq.empty:
            st.info("No Fresh Sell Aggression has been recorded today.")
        else:
            st.dataframe(
                sell_seq[[c for c in sequence_columns if c in sell_seq.columns]],
                width="stretch", hide_index=True, column_config=sequence_config
            )

        st.caption(
            "The follow-through stage uses futures price and OI only: price up + OI down is "
            "Short Covering; price down + OI down is Long Unwinding. Executed delta remains "
            "visible as supporting evidence but is not required for the second stage."
        )

with tab10:
    st.subheader("First-hour AVWAP confirmation")
    st.caption(
        "The 09:15–10:15 futures high/low reference is frozen at 10:15. "
        "Subsequent continuing AVWAP structure and crossings contribute up to 1.5 points."
    )
    if avwap_history.empty:
        st.info("No AVWAP rows are available yet. Confirm that the combined collector service is running before 09:15 IST.")
    else:
        av_latest = (
            avwap_history.sort_values("ts").groupby("symbol", as_index=False).tail(1)
            .merge(universe[["symbol", "rank"]], on="symbol", how="left")
            .sort_values("rank")
        )
        av_latest["time_ist"] = av_latest["ts"].apply(time_ist)
        av_latest["structure"] = "MIXED / WAITING"
        bull_structure = (
            (pd.to_numeric(av_latest["avwap_high"], errors="coerce") > pd.to_numeric(av_latest["hourly_avwap_high"], errors="coerce")) &
            (pd.to_numeric(av_latest["avwap_low"], errors="coerce") > pd.to_numeric(av_latest["hourly_avwap_low"], errors="coerce"))
        )
        bear_structure = (
            (pd.to_numeric(av_latest["avwap_high"], errors="coerce") < pd.to_numeric(av_latest["hourly_avwap_high"], errors="coerce")) &
            (pd.to_numeric(av_latest["avwap_low"], errors="coerce") < pd.to_numeric(av_latest["hourly_avwap_low"], errors="coerce"))
        )
        av_latest.loc[bull_structure, "structure"] = "BULLISH"
        av_latest.loc[bear_structure, "structure"] = "BEARISH"
        cols = ["rank", "symbol", "time_ist", "structure", "avwap_high",
                "hourly_avwap_high", "high_cross", "avwap_low",
                "hourly_avwap_low", "low_cross"]
        st.dataframe(
            av_latest[[c for c in cols if c in av_latest.columns]],
            width="stretch", hide_index=True,
            column_config={
                "rank":"MF Rank", "symbol":"Symbol", "time_ist":"Time",
                "structure":"AVWAP Structure", "high_cross":"High Cross",
                "low_cross":"Low Cross",
                "avwap_high":st.column_config.NumberColumn("Continuing High",format="%.2f"),
                "hourly_avwap_high":st.column_config.NumberColumn("Frozen High",format="%.2f"),
                "avwap_low":st.column_config.NumberColumn("Continuing Low",format="%.2f"),
                "hourly_avwap_low":st.column_config.NumberColumn("Frozen Low",format="%.2f"),
            }
        )

st.divider()

r1, r2 = st.columns(2)
with r1:
    if st.button("Refresh now", width="stretch"):
        st.rerun()

with r2:
    st.caption("Keep this page open on your phone. Use Refresh now after each 3-minute collector cycle.")


with st.expander("09:20 Same-strike OI — last 5 trading days backtest", expanded=False):
    st.caption("Default research assumptions: target +0.50%, stop -0.30%, round-trip cost 10 bps. First qualifying same-strike signal per direction/day; target-before-stop is evaluated in timestamp order on stored 3-minute futures snapshots.")
    try:
        bt=load_same_strike_oi_backtest(5,0.50,0.30,10.0)
        if bt.empty:
            st.info("No qualifying same-strike 09:20 OI events are stored yet. New deployments will populate these fields prospectively; historical rows cannot be recreated unless 09:20 baseline OI exists for those sessions.")
        else:
            btx=bt.copy()
            for c in ["ts","entry_ts","outcome_ts"]:
                if c in btx: btx[c]=btx[c].apply(time_ist)
            st.dataframe(btx,width="stretch",hide_index=True)
            resolved=bt[bt["target_before_stop"].notna()]
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Signals",len(bt))
            c2.metric("Resolved",len(resolved))
            c3.metric("Target before stop", f"{(resolved['target_before_stop'].mean()*100):.1f}%" if not resolved.empty else "-")
            c4.metric("Avg cost-adjusted", f"{pd.to_numeric(bt['cost_adjusted_return_pct'],errors='coerce').mean():.3f}%")
    except Exception as exc:
        st.warning(f"Five-day same-strike OI backtest unavailable: {exc}")

st.caption(
    "Data source: your Neon database. The dashboard stores reconstructed Early Detector score history; it does not place trades."
)
