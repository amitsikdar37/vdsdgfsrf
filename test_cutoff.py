import pytest
from datetime import datetime
from etl_pipeline import align_day_ahead_forecasts
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


# --- TEST 1: The Lookahead Bias Test (Reject Post-Cutoff Forecasts) ---
def test_cutoff_rejects_late_forecast():
    """
    Delivery Date: 2026-01-15 (Target startTime: 2026-01-15T14:00:00Z)
    Decision Cutoff: 2026-01-14 at 11:00:00Z
    
    A forecast published at 11:05:00Z on Jan 14 must be DISQUALIFIED.
    """
    mock_forecasts = [
        {
            "startTime": "2026-01-15T14:00:00Z",
            "publishTime": "2026-01-14T11:05:00Z",  
            "generation": 15000.0
        }
    ]

    result = align_day_ahead_forecasts(mock_forecasts)
    
    assert "2026-01-15T14:00:00Z" not in result, "CRITICAL: Lookahead bias! Post-cutoff forecast was accepted!"


# --- TEST 2: The Valid Pre-Cutoff Ingestion Test ---
def test_cutoff_accepts_valid_forecast():
    """
    A forecast published at 10:55:00Z on Jan 14 (5 minutes before cutoff)
    must be ACCEPTED.
    """
    mock_forecasts = [
        {
            "startTime": "2026-01-15T14:00:00Z",
            "publishTime": "2026-01-14T10:55:00Z", 
            "generation": 12500.0
        }
    ]

    result = align_day_ahead_forecasts(mock_forecasts)
    
    assert "2026-01-15T14:00:00Z" in result
    assert result["2026-01-15T14:00:00Z"]["forecast_mw"] == 12500.0


# --- TEST 3: The 'Latest Run Wins' Test ---
def test_cutoff_picks_latest_valid_run():
    """
    If multiple forecasts were published before cutoff:
    Run 1: 06:00 AM (8,000 MW)
    Run 2: 10:30 AM (9,500 MW)
    
    The engine must choose Run 2 (9,500 MW) as the latest available intelligence.
    """
    mock_forecasts = [
        {
            "startTime": "2026-01-15T14:00:00Z",
            "publishTime": "2026-01-14T06:00:00Z",
            "generation": 8000.0
        },
        {
            "startTime": "2026-01-15T14:00:00Z",
            "publishTime": "2026-01-14T10:30:00Z",  
            "generation": 9500.0
        },
        {
            "startTime": "2026-01-15T14:00:00Z",
            "publishTime": "2026-01-14T14:00:00Z",  
            "generation": 11000.0
        }
    ]

    result = align_day_ahead_forecasts(mock_forecasts)
    
    assert "2026-01-15T14:00:00Z" in result
    assert result["2026-01-15T14:00:00Z"]["forecast_mw"] == 9500.0


# --- TEST 4: FastAPI Integration Smoke Test ---
def test_api_health_and_summary():
    """Verifies that FastAPI serves valid JSON responses from the database."""
    health_res = client.get("/api/v1/health")
    assert health_res.status_code == 200
    assert health_res.json()["status"] == "healthy"

    summary_res = client.get("/api/v1/summary")
    assert summary_res.status_code == 200
    data = summary_res.json()
    assert "mae_mw" in data
    assert "bias_mw" in data
    assert data["mae_mw"] > 0
