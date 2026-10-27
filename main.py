import sqlite3
import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="UK Day-Ahead Wind Procurement & Analytics API",
    description="Operational 48-SP procurement schedules and native meteorological error analytics for UK wholesale power trading.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "market_data.db")

def get_db():
    """Helper to connect to the SQLite database with dictionary-like row access."""
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=500, detail="Database file not found. Please run etl_pipeline.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row 
    return conn


@app.get("/api/v1/health", tags=["System"])
def health_check():
    """Returns the operational status of the API and SQLite connection."""
    db_connected = os.path.exists(DB_PATH)
    return {
        "status": "healthy",
        "database_connected": db_connected,
        "environment": "production" if os.getenv("RENDER") or os.getenv("VERCEL") else "development"
    }


@app.get("/api/v1/summary", tags=["Analytics"])
def get_summary():
    """
    Returns January 2026 macro KPIs calculated on native 1-hour resolution 
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT metric, value FROM summary_metrics")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No summary metrics found.")

    metrics = {row["metric"]: row["value"] for row in rows}
    
    bias = metrics.get("bias_mw", 0.0)
    return {
        "mae_mw": metrics.get("mae_mw"),
        "rmse_mw": metrics.get("rmse_mw"),
        "bias_mw": bias,
        "bias_direction": "Over-forecast (Shortage Risk)" if bias > 0 else "Under-forecast (Surplus Risk)",
        "total_actual_mwh": metrics.get("total_actual_mwh"),
        "total_forecast_mwh": metrics.get("total_forecast_mwh"),
        "evaluated_hours": int(metrics.get("evaluated_hours", 0)),
        "total_48_sp_count": int(metrics.get("total_48_sp_count", 0)),
    }


@app.get("/api/v1/settlement-periods", tags=["Trading Desk"])
def get_settlement_periods(
    date: str = Query(..., description="Target delivery date in YYYY-MM-DD format (e.g. 2026-01-15)")
):
    """
    Returns all 48 half-hour settlement periods for a selected delivery date.
    Even-numbered periods derived from interval holds are explicitly flagged with is_interpolated=1.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            start_time,
            settlement_date,
            settlement_period,
            forecast_mw,
            actual_mw,
            error_mw,
            is_interpolated,
            publish_time,
            cutoff_time
        FROM settlement_periods_48 
        WHERE settlement_date = ?
        ORDER BY settlement_period ASC
    """, (date,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        raise HTTPException(
            status_code=404, 
            detail=f"No data available for delivery date '{date}'. Try dates between 2026-01-02 and 2026-02-01."
        )

    return [
        {
            "startTime": r["start_time"],
            "settlementDate": r["settlement_date"],
            "sp": r["settlement_period"],
            "forecast_mw": r["forecast_mw"],
            "actual_mw": r["actual_mw"],
            "error_mw": r["error_mw"],
            "is_interpolated": bool(r["is_interpolated"]),
            "publishTime": r["publish_time"],
            "cutoffTime": r["cutoff_time"]
        }
        for r in rows
    ]


@app.get("/api/v1/error-profile", tags=["Analytics"])
def get_error_profile(resolution: str = Query("sp", description="'sp' for 48 settlement periods or 'hour' for 24 hours")):
    """
    Returns the diurnal error profile across January 2026.
    Supports either full 48 settlement periods ('sp') or 24-hour native blocks ('hour').
    """
    conn = get_db()
    cur = conn.cursor()
    
    if resolution == "hour":
        cur.execute("""
            SELECT 
                hour_of_day as period,
                avg_forecast_mw,
                avg_actual_mw,
                mae,
                rmse,
                bias,
                sample_count
            FROM hourly_error_profile
            ORDER BY hour_of_day ASC
        """)
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "period": r["period"],
                "label": f"{r['period']:02d}:00",
                "avg_forecast_mw": r["avg_forecast_mw"],
                "avg_actual_mw": r["avg_actual_mw"],
                "mae": r["mae"],
                "rmse": r["rmse"],
                "bias": r["bias"],
                "sample_count": r["sample_count"]
            }
            for r in rows
        ]
    else:
        # Full 48 Settlement Periods Profile
        cur.execute("""
            SELECT 
                settlement_period as period,
                ROUND(AVG(forecast_mw), 2) as avg_forecast_mw,
                ROUND(AVG(actual_mw), 2) as avg_actual_mw,
                ROUND(AVG(abs(error_mw)), 2) as mae,
                ROUND(SQRT(AVG(error_mw * error_mw)), 2) as rmse,
                ROUND(AVG(error_mw), 2) as bias,
                COUNT(*) as sample_count
            FROM settlement_periods_48
            GROUP BY settlement_period
            ORDER BY settlement_period ASC
        """)
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "period": r["period"],
                "label": f"SP {r['period']}",
                "avg_forecast_mw": r["avg_forecast_mw"],
                "avg_actual_mw": r["avg_actual_mw"],
                "mae": r["mae"],
                "rmse": r["rmse"],
                "bias": r["bias"],
                "sample_count": r["sample_count"]
            }
            for r in rows
        ]

