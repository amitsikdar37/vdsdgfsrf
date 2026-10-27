import urllib.request
import json
import time
import sys
from datetime import datetime, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets"

def fetch_json(url: str, retries: int = 3, delay: int = 2):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, 
                headers={"User-Agent": "UK-DayAhead-Procurement-App/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  [Attempt {attempt}/{retries}] Failed to fetch {url}: {e}")
            if attempt < retries:
                time.sleep(delay)
            else:
                raise


def fetch_all_actuals():
    print("Fetching FUELHH (Actuals)...")
    actuals = {}
    
    date_ranges = [
        ("2026-01-02", "2026-01-08"),
        ("2026-01-09", "2026-01-15"),
        ("2026-01-16", "2026-01-22"),
        ("2026-01-23", "2026-01-29"),
        ("2026-01-30", "2026-02-01"),
    ]
    
    for start_date, end_date in date_ranges:
        url = f"{BASE_URL}/FUELHH/stream?settlementDateFrom={start_date}&settlementDateTo={end_date}"
        print(f"  -> Querying FUELHH: {start_date} to {end_date}")
        records = fetch_json(url)

        for rec in records:
            if rec.get("fuelType") == "WIND":
                start_time = rec["startTime"]
                actuals[start_time] = {
                    "startTime": start_time,
                    "settlementDate": rec["settlementDate"],
                    "settlementPeriod": rec["settlementPeriod"],
                    "actual_mw": float(rec["generation"])
                }
    
    print(f"[OK] Total Actuals retrieved: {len(actuals)} settlement periods.")
    return actuals

def fetch_all_forecasts():
    print("Fetching WINDFOR (Forecasts)...")
    all_forecasts = []
    
    current = datetime(2025, 12, 31, 0, 0, 0)
    end_limit = datetime(2026, 1, 31, 12, 0, 0)
    
    while current < end_limit:
        next_chunk = min(current + timedelta(days=3), end_limit)
        p_from = current.strftime("%Y-%m-%dT%H:%M:%SZ")
        p_to = next_chunk.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        url = f"{BASE_URL}/WINDFOR/stream?publishDateTimeFrom={p_from}&publishDateTimeTo={p_to}"
        print(f"  -> Querying WINDFOR published between: {p_from} and {p_to}")
        records = fetch_json(url)
        all_forecasts.extend(records)
        
        current = next_chunk
        time.sleep(0.5)
        
    print(f"[OK] Total raw forecast records retrieved: {len(all_forecasts)}")
    return all_forecasts

def align_day_ahead_forecasts(raw_forecasts):
    """
    Groups forecasts by delivery target day D.
    For each delivery day, enforce cutoff = (D - 1 day) at 11:00:00 UTC.
    Select the latest forecast published before cutoff for each 30-min window.
    """
    print("Aligning forecasts to Day-Ahead 11:00 AM Gate Closure...")
    
    by_target = {}
    for f in raw_forecasts:
        target = f.get("startTime")
        if target:
            by_target.setdefault(target, []).append(f)
            
    frozen_forecasts = {}
    
    for start_time_str, candidate_forecasts in by_target.items():
        target_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        delivery_date = target_dt.date()
        
        if not (datetime(2026, 1, 2).date() <= delivery_date <= datetime(2026, 2, 1).date()):
            continue
        decision_date = delivery_date - timedelta(days=1)
        cutoff_dt = datetime(decision_date.year, decision_date.month, decision_date.day, 11, 0, 0, tzinfo=target_dt.tzinfo)

        valid_candidates = []
        for cand in candidate_forecasts:
            pub_dt = datetime.fromisoformat(cand["publishTime"].replace("Z", "+00:00"))
            if pub_dt <= cutoff_dt:
                valid_candidates.append((pub_dt, float(cand["generation"])))
                
        if valid_candidates:
            latest_pub_dt, chosen_generation = max(valid_candidates, key=lambda x: x[0])
            frozen_forecasts[start_time_str] = {
                "forecast_mw": chosen_generation,
                "publishTime": latest_pub_dt.isoformat(),
                "cutoffTime": cutoff_dt.isoformat()
            }
            
    print(f"[OK] Aligned frozen forecasts for {len(frozen_forecasts)} settlement periods.")
    return frozen_forecasts


def save_and_analyze_dataset(actuals, frozen_forecasts, db_path="data/market_data.db"):
    """
    Merges actuals and frozen forecasts, computes quantitative metrics 
    (MAE, RMSE, Bias, SP profiles), and persists to a lightweight SQLite database.
    """
    import os
    import sqlite3
    import math

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settlement_periods (
        start_time TEXT PRIMARY KEY,
        settlement_date TEXT,
        settlement_period INTEGER,
        forecast_mw REAL,
        actual_mw REAL,
        error_mw REAL,
        abs_error_mw REAL,
        publish_time TEXT,
        cutoff_time TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS summary_metrics (
        metric TEXT PRIMARY KEY,
        value REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sp_error_profile (
        settlement_period INTEGER PRIMARY KEY,
        avg_forecast_mw REAL,
        avg_actual_mw REAL,
        mae REAL,
        rmse REAL,
        bias REAL,
        sample_count INTEGER
    )
    """)

    common_keys = sorted(set(actuals.keys()) & set(frozen_forecasts.keys()))
    print(f"\nMerging {len(common_keys)} aligned settlement periods...")

    rows_to_insert = []
    total_abs_error = 0.0
    total_sq_error = 0.0
    total_error = 0.0
    total_actual_mw = 0.0
    total_forecast_mw = 0.0

    sp_buckets = {sp: {"errors": [], "forecasts": [], "actuals": []} for sp in range(1, 49)}

    for st in common_keys:
        act = actuals[st]
        fc = frozen_forecasts[st]
        
        forecast_mw = fc["forecast_mw"]
        actual_mw = act["actual_mw"]
        sp = act["settlementPeriod"]
        
        error_mw = forecast_mw - actual_mw
        abs_error = abs(error_mw)
        sq_error = error_mw ** 2
        
        total_abs_error += abs_error
        total_sq_error += sq_error
        total_error += error_mw
        total_actual_mw += actual_mw
        total_forecast_mw += forecast_mw
        
        if 1 <= sp <= 48:
            sp_buckets[sp]["errors"].append(error_mw)
            sp_buckets[sp]["forecasts"].append(forecast_mw)
            sp_buckets[sp]["actuals"].append(actual_mw)
            
        rows_to_insert.append((
            st,
            act["settlementDate"],
            sp,
            forecast_mw,
            actual_mw,
            error_mw,
            abs_error,
            fc["publishTime"],
            fc["cutoffTime"]
        ))

    cur.executemany("""
    INSERT OR REPLACE INTO settlement_periods 
    (start_time, settlement_date, settlement_period, forecast_mw, actual_mw, error_mw, abs_error_mw, publish_time, cutoff_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows_to_insert)

    n = len(common_keys)
    if n > 0:
        overall_mae = total_abs_error / n
        overall_rmse = math.sqrt(total_sq_error / n)
        overall_bias = total_error / n
        total_actual_mwh = total_actual_mw * 0.5
        total_forecast_mwh = total_forecast_mw * 0.5
        
        summary_rows = [
            ("total_periods", float(n)),
            ("mae_mw", round(overall_mae, 2)),
            ("rmse_mw", round(overall_rmse, 2)),
            ("bias_mw", round(overall_bias, 2)),
            ("total_actual_mwh", round(total_actual_mwh, 2)),
            ("total_forecast_mwh", round(total_forecast_mwh, 2)),
        ]
        cur.executemany("INSERT OR REPLACE INTO summary_metrics (metric, value) VALUES (?, ?)", summary_rows)

        profile_rows = []
        for sp in range(1, 49):
            errs = sp_buckets[sp]["errors"]
            fcs = sp_buckets[sp]["forecasts"]
            acts = sp_buckets[sp]["actuals"]
            count = len(errs)
            if count > 0:
                sp_mae = sum(abs(e) for e in errs) / count
                sp_rmse = math.sqrt(sum(e**2 for e in errs) / count)
                sp_bias = sum(errs) / count
                avg_f = sum(fcs) / count
                avg_a = sum(acts) / count
                profile_rows.append((sp, round(avg_f, 2), round(avg_a, 2), round(sp_mae, 2), round(sp_rmse, 2), round(sp_bias, 2), count))
                
        cur.executemany("""
        INSERT OR REPLACE INTO sp_error_profile 
        (settlement_period, avg_forecast_mw, avg_actual_mw, mae, rmse, bias, sample_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, profile_rows)

    conn.commit()
    conn.close()

    print("\n==========================================")
    print("      JANUARY 2026 QUANTITATIVE SUMMARY   ")
    print("==========================================")
    print(f"Total Evaluated Periods : {n} (out of {31 * 48} possible)")
    print(f"Overall MAE             : {overall_mae:.2f} MW")
    print(f"Overall RMSE            : {overall_rmse:.2f} MW")
    print(f"Systematic Bias         : {overall_bias:.2f} MW ({'Over-forecast' if overall_bias > 0 else 'Under-forecast'})")
    print(f"Total Actual Wind Energy: {total_actual_mwh:,.0f} MWh")
    print(f"Database successfully saved to: {db_path}")
    print("==========================================\n")


if __name__ == "__main__":
    print("Starting Day-Ahead Wind Ingestion & Analytics Pipeline...")
    t0 = time.time()

    actuals = fetch_all_actuals()
    
    raw_forecasts = fetch_all_forecasts()

    frozen_forecasts = align_day_ahead_forecasts(raw_forecasts)
    
    save_and_analyze_dataset(actuals, frozen_forecasts)
    
    print(f"[OK] Pipeline execution completed in {time.time() - t0:.1f} seconds.")
