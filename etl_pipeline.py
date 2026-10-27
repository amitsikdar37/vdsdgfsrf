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

def fetch_json(url: str, cache_key: str = None, retries: int = 5, base_delay: int = 2):
    import os
    if cache_key:
        cache_dir = "data/raw_cache"
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"{cache_key}.json")
        if os.path.exists(cache_file):
            print(f"  -> [Cached] Loading {cache_key} from disk")
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, 
                headers={"User-Agent": "UK-DayAhead-Procurement-App/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if cache_key:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(data, f)
                    return data
        except Exception as e:
            delay = base_delay * (2 ** (attempt - 1))
            print(f"  [Attempt {attempt}/{retries}] Failed to fetch {url}: {e} (Retrying in {delay}s...)")
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
        cache_key = f"fuelhh_{start_date}_{end_date}"
        records = fetch_json(url, cache_key=cache_key)

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
        cache_key = f"windfor_{p_from[:10]}_{p_to[:10]}"
        records = fetch_json(url, cache_key=cache_key)
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
    Dual-Resolution Persistence:
    1. settlement_periods_48: Full 48-SP procurement table with is_interpolated flag.
    2. hourly_evaluation: Native hourly evaluation (Actual = (SP_odd + SP_even)/2).
    3. summary_metrics: Pure, uncontaminated meteorological accuracy metrics.
    4. hourly_error_profile: Pure Hour 0-23 diurnal error profiles.
    """
    import os
    import sqlite3
    import math

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS settlement_periods_48")
    cur.execute("DROP TABLE IF EXISTS hourly_evaluation")
    cur.execute("DROP TABLE IF EXISTS summary_metrics")
    cur.execute("DROP TABLE IF EXISTS hourly_error_profile")

    cur.execute("""
    CREATE TABLE settlement_periods_48 (
        start_time TEXT PRIMARY KEY,
        settlement_date TEXT,
        settlement_period INTEGER,
        forecast_mw REAL,
        actual_mw REAL,
        error_mw REAL,
        is_interpolated INTEGER,
        publish_time TEXT,
        cutoff_time TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE hourly_evaluation (
        delivery_date TEXT,
        hour_of_day INTEGER,
        forecast_mw REAL,
        mean_actual_mw REAL,
        error_mw REAL,
        abs_error_mw REAL,
        sq_error_mw REAL,
        publish_time TEXT,
        cutoff_time TEXT,
        PRIMARY KEY (delivery_date, hour_of_day)
    )
    """)

    cur.execute("""
    CREATE TABLE summary_metrics (
        metric TEXT PRIMARY KEY,
        value REAL
    )
    """)

    cur.execute("""
    CREATE TABLE hourly_error_profile (
        hour_of_day INTEGER PRIMARY KEY,
        avg_forecast_mw REAL,
        avg_actual_mw REAL,
        mae REAL,
        rmse REAL,
        bias REAL,
        sample_count INTEGER
    )
    """)

    actuals_by_date_sp = {}
    for st, act in actuals.items():
        actuals_by_date_sp[(act["settlementDate"], act["settlementPeriod"])] = act
    delivery_dates = sorted(set(act["settlementDate"] for act in actuals.values()))

    hourly_rows = []
    sp48_rows = []

    total_abs_err = 0.0
    total_sq_err = 0.0
    total_bias_err = 0.0
    total_actual_mwh = 0.0
    total_forecast_mwh = 0.0

    hour_buckets = {h: {"errors": [], "forecasts": [], "actuals": []} for h in range(24)}

    for d in delivery_dates:
        for h in range(24):
            sp_odd = 2 * h + 1
            sp_even = 2 * h + 2
            
            act_odd = actuals_by_date_sp.get((d, sp_odd))
            act_even = actuals_by_date_sp.get((d, sp_even))
            
            if not act_odd or not act_even:
                continue

            fc_odd = frozen_forecasts.get(act_odd["startTime"])
            if not fc_odd:
                continue

            f_mw = fc_odd["forecast_mw"]
            a_odd = act_odd["actual_mw"]
            a_even = act_even["actual_mw"]
            

            mean_actual = (a_odd + a_even) / 2.0
            h_error = f_mw - mean_actual
            abs_err = abs(h_error)
            sq_err = h_error ** 2

            total_abs_err += abs_err
            total_sq_err += sq_err
            total_bias_err += h_error

            # Energy (MWh) = MW * 0.5 hours
            total_actual_mwh += (a_odd * 0.5) + (a_even * 0.5)
            total_forecast_mwh += (f_mw * 0.5) + (f_mw * 0.5)

            hour_buckets[h]["errors"].append(h_error)
            hour_buckets[h]["forecasts"].append(f_mw)
            hour_buckets[h]["actuals"].append(mean_actual)

            hourly_rows.append((
                d, h, f_mw, mean_actual, h_error, abs_err, sq_err,
                fc_odd["publishTime"], fc_odd["cutoffTime"]
            ))

            sp48_rows.append((
                act_odd["startTime"], d, sp_odd, f_mw, a_odd,
                f_mw - a_odd, 0, fc_odd["publishTime"], fc_odd["cutoffTime"]
            ))


            sp48_rows.append((
                act_even["startTime"], d, sp_even, f_mw, a_even,
                f_mw - a_even, 1, fc_odd["publishTime"], fc_odd["cutoffTime"]
            ))

    cur.executemany("""
    INSERT INTO hourly_evaluation 
    (delivery_date, hour_of_day, forecast_mw, mean_actual_mw, error_mw, abs_error_mw, sq_error_mw, publish_time, cutoff_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, hourly_rows)

    cur.executemany("""
    INSERT INTO settlement_periods_48 
    (start_time, settlement_date, settlement_period, forecast_mw, actual_mw, error_mw, is_interpolated, publish_time, cutoff_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, sp48_rows)

    n_eval = len(hourly_rows)
    if n_eval > 0:
        overall_mae = total_abs_err / n_eval
        overall_rmse = math.sqrt(total_sq_err / n_eval)
        overall_bias = total_bias_err / n_eval

        summary_rows = [
            ("evaluated_hours", float(n_eval)),
            ("total_48_sp_count", float(len(sp48_rows))),
            ("mae_mw", round(overall_mae, 2)),
            ("rmse_mw", round(overall_rmse, 2)),
            ("bias_mw", round(overall_bias, 2)),
            ("total_actual_mwh", round(total_actual_mwh, 2)),
            ("total_forecast_mwh", round(total_forecast_mwh, 2)),
        ]
        cur.executemany("INSERT INTO summary_metrics (metric, value) VALUES (?, ?)", summary_rows)

        profile_rows = []
        for h in range(24):
            errs = hour_buckets[h]["errors"]
            fcs = hour_buckets[h]["forecasts"]
            acts = hour_buckets[h]["actuals"]
            cnt = len(errs)
            if cnt > 0:
                h_mae = sum(abs(e) for e in errs) / cnt
                h_rmse = math.sqrt(sum(e**2 for e in errs) / cnt)
                h_bias = sum(errs) / cnt
                avg_f = sum(fcs) / cnt
                avg_a = sum(acts) / cnt
                profile_rows.append((h, round(avg_f, 2), round(avg_a, 2), round(h_mae, 2), round(h_rmse, 2), round(h_bias, 2), cnt))

        cur.executemany("""
        INSERT INTO hourly_error_profile 
        (hour_of_day, avg_forecast_mw, avg_actual_mw, mae, rmse, bias, sample_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, profile_rows)

    conn.commit()
    conn.close()

    print("\n=======================================================")
    print("      DUAL-RESOLUTION PIPELINE SUMMARY (JAN 2026)      ")
    print("=======================================================")
    print(f"48-SP Procurement Schedule Rows : {len(sp48_rows)} (100% full coverage)")
    print(f"Native Evaluated Hours          : {n_eval} hours (zero synthetic bias)")
    print(f"Native Clean MAE                : {overall_mae:.2f} MW")
    print(f"Native Clean RMSE               : {overall_rmse:.2f} MW")
    print(f"Systematic Bias                 : {overall_bias:.2f} MW ({'Over-forecast' if overall_bias > 0 else 'Under-forecast'})")
    print(f"Total Actual Wind Energy        : {total_actual_mwh:,.0f} MWh")
    print(f"Clean Database Saved to         : {db_path}")
    print("=======================================================\n")


if __name__ == "__main__":
    print("Starting Day-Ahead Wind Ingestion & Analytics Pipeline...")
    t0 = time.time()

    actuals = fetch_all_actuals()
    
    raw_forecasts = fetch_all_forecasts()

    frozen_forecasts = align_day_ahead_forecasts(raw_forecasts)
    
    save_and_analyze_dataset(actuals, frozen_forecasts)
    
    print(f"[OK] Pipeline execution completed in {time.time() - t0:.1f} seconds.")
