# AeroGrid • UK Day-Ahead Wind Procurement & Quantitative Risk Engine

> **Live Production Deployment**: [https://vdsdgfsrf.vercel.app/](https://vdsdgfsrf.vercel.app/)  
> **Evaluation Horizon**: January 2026 Day-Ahead Procurement Decisions (Delivery: Jan 02 – Feb 01, 2026)  
> **Data Stream**: National Level Wind Outturn (`FUELHH`) & Day-Ahead Forecasts (`WINDFOR`) via Elexon BMRS  
> **Stack**: React 19 + Recharts + Python FastAPI + SQLite + Docker

---

## 1. Executive Summary & Problem Setting

### 1.1 The Client's Challenge
The client is a UK electricity utility responsible for procuring wholesale power to meet consumer demand. While wind generation constitutes an increasingly large share of the UK grid, it is non-dispatchable and weather-dependent. 

To ensure continuous grid balance without incurring severe financial imbalance penalties, the day-ahead procurement desk must determine:
$$\text{Thermal Procurement Required (MW)} = \text{Forecasted Consumer Demand (MW)} - \text{Expected Wind Generation (MW)}$$

All wholesale power purchase decisions for delivery day $D$ must be submitted into the day-ahead wholesale auctions (e.g., EPEX SPOT / Nord Pool) prior to the **11:00 AM UTC Day-Ahead Gate Closure on $D-1$**.

### 1.2 The Financial Risk of Forecast Error
In the UK balancing mechanism governed by the Balancing and Settlement Code (BSC):
- **Over-Forecasting (Positive Bias)**: The utility assumes high wind generation and under-buys thermal power. When physical wind outturn falls short, the utility is "short" in the balancing market and must cash out the deficit at the punitive **System Buy Price (SBP)** during peak hours.
- **Under-Forecasting (Negative Bias)**: The utility over-procures thermal power and must sell excess power back at the lower **System Sell Price (SSP)**.

This software product provides the desk with:
1. **Operational 48-Settlement-Period Order Schedules** reflecting the latest legitimate forecast available strictly before 11:00 AM UTC on $D-1$ (zero lookahead bias).
2. **Quantitative Accuracy Analytics** that size the necessary thermal buffer and identify diurnal risk patterns across the 24 hours of the delivery day.

---

## 2. January 2026 Quantitative Outturn & Key Findings

Evaluating national UK wind outturn across **1,488 Settlement Periods (744 Hours)** between **02 January 2026 00:00 UTC and 01 February 2026 23:30 UTC** yields critical quantitative insights for power traders:

| Macro KPI | Value | Operational Implication for Desk |
| :--- | :---: | :--- |
| **Native MAE** | **2,013.01 MW** | On average, day-ahead wind forecasts deviated by **~2.01 GW** from physical outturn. |
| **Root Mean Square Error (RMSE)** | **2,505.94 MW** | Large outlier forecast errors (storm fronts arriving faster/slower than model runs) amplify volatile variance. |
| **Systematic Directional Bias** | **+1,729.93 MW** | **Severe Over-Forecasting**. Forecast models persistently overestimated wind outturn across **86.4%** of January hours. |
| **Delivered Wind Energy Outturn** | **4.20 TWh** | Actual energy produced across 1,488 half-hours ($4,198,208\text{ MWh}$). |

### Key Behavioral Discoveries:
1. **The Diurnal Evening Peak Penalty**: Error and systematic bias peak between **17:00 and 20:00 UTC (SPs 35–40)**, averaging over **+2,200 MW** of over-forecast. Because this coincides with the national peak electricity demand ramp and the highest daily wholesale prices, an unhedged desk faces maximum financial exposure during these specific hours.
2. **The "Storm Shift" Phenomenon (Jan 22, 2026)**: During severe atmospheric transitions (e.g., high-wind stormfronts moving across the North Sea), the day-ahead forecast ramp misjudged wind timing by 3–4 hours, causing errors exceeding **5,000 MW**.

---

## 3. Methodological Rigor & Technical Design

### 3.1 Strict Gate Closure Enforcement (Zero Lookahead Bias)
Trading desks cannot use forecasts generated after the auction has cleared.
- For delivery date $D$, only forecasts with `publishTime <= (D-1) 11:00:00 UTC` are admitted.
- If multiple forecast cycles were published prior to 11:00 AM on $D-1$ (e.g. 06:00 UTC and 10:30 UTC), the pipeline selects the **latest valid run** (10:30 UTC).
- All intra-day or post-11:00 updates are strictly discarded.

### 3.2 Dual-Resolution Architecture (Solving Resolution Mismatch)
Elexon BMRS exposes data at two different temporal resolutions:
- **`WINDFOR` (Forecast)**: Hourly resolution (**24 forecast points per day**).
- **`FUELHH` (Actual Generation)**: Half-hourly settlement resolution (**48 points per day**).

To prevent mathematical distortion, we engineered a **Dual-Resolution Architecture**:
1. **Operational Desk View (48 Settlement Periods)**:
   - Built to match the UK market's half-hourly settlement periods (SP 1 to SP 48).
   - Half-hourly steps are linearly derived from the 24 hourly anchors.
   - Every row in the matrix carries an immutable provenance indicator: `Native Point Forecast` vs `Derived Half-Hour*`, ensuring full auditability.
2. **Quantitative Accuracy Lab (24 Native Hours)**:
   - For statistical rigor (MAE, RMSE, Bias), actual half-hourly periods are aggregated into native hourly blocks:
     $$\text{Actual}_H = \frac{\text{Actual}_{\text{SP } 2H+1} + \text{Actual}_{\text{SP } 2H+2}}{2}$$
   - This ensures meteorological models are graded **strictly against native coordinates**, eliminating synthetic interpolation bias.

### 3.3 Empirical MAE Uncertainty Bands vs. The Synthetic P10/P90 Trap
Elexon `WINDFOR` is a deterministic forecast. Fabricating artificial Gaussian confidence intervals (e.g. $\pm 1.645 \sigma$) would misrepresent real meteorological asymmetry. Instead, our trading dashboard projects an **Empirical Risk Envelope**:
$$\text{Risk Band} = \left[ \max(0, \text{Forecast}_t - \text{MAE}), \; \text{Forecast}_t + \text{MAE} \right]$$
Traders use this envelope as a rule-of-thumb margin to size thermal backup contracts.

### 3.4 Cold-Start Resilient Cloud Architecture
When running on cloud microservices (e.g. Render free tier), containers spin down after 15 minutes of inactivity. 
- The React application includes an institutional **Waking Up Gateway**.
- When a reviewer opens the link while the backend is dormant, rather than showing blank tables or broken errors, the UI displays a clean diagnostic modal with live elapsed seconds, retry heartbeat pings to `/api/v1/health`, and status indicators.
- As soon as the backend responds, the screen smoothly transitions into the populated trading desk.

---

## 4. Repository Structure & File Manifest

```text
├── Dockerfile                   # Multi-stage production container (Node 20 + Python 3.11)
├── docker-compose.yml           # One-command local orchestration (port 8000)
├── .dockerignore                # Build context exclusion rules
├── requirements.txt             # Pinned backend Python dependencies
├── main.py                      # FastAPI microservice with CORS & dual-resolution endpoints
├── test_cutoff.py               # Automated pytest suite verifying gate closure & API contracts
├── etl_pipeline.py              # Elexon BMRS streaming ingestion, chunking & DB builder
├── data/
│   ├── market_data.db           # SQLite database containing 1,488 SPs and 744 hourly rows
│   └── raw_cache/               # Local cache for raw Elexon JSON payloads (gitignored)
└── frontend/
    ├── index.html               # Institutional title & metadata configuration
    ├── package.json             # React 19, Recharts, Lucide-react
    ├── vite.config.js           # Vite configuration with local proxy to port 8000
    └── src/
        ├── App.jsx              # Trading Operations Desk & Quantitative Accuracy Lab UI
        ├── index.css            # Custom utility-desk dark-mode design system
        └── main.jsx             # React DOM entry point
```

---

## 5. Quickstart & Local Setup

### Option A: One-Command Docker Setup (Recommended)
You can run the entire decoupled stack inside a self-contained container:
```bash
# Clone the repository
git clone <REPO_URL>
cd <REPO_FOLDER>

# Build and run with Docker Compose
docker compose up --build
```
Once started, open **[http://localhost:8000](http://localhost:8000)** in your browser.

---

### Option B: Manual Local Development Setup

#### 1. Backend (FastAPI + SQLite)
Requires Python 3.10+:
```bash
# Install Python dependencies
pip install -r requirements.txt

# Run the automated test suite
python -m pytest test_cutoff.py -v

# Start FastAPI server on port 8000
python -m uvicorn main:app --reload --port 8000
```
FastAPI interactive Swagger docs will be live at: `http://localhost:8000/docs`

#### 2. Frontend (React 19 + Vite)
Requires Node.js 18+:
```bash
cd frontend

# Install npm dependencies
npm install

# Start Vite dev server on port 5173
npm run dev
```
Open **[http://localhost:5173](http://localhost:5173)** in your browser.

---

## 6. Cloud Deployment Architecture

- **Frontend**: Hosted on [Vercel](https://vercel.com/) at [https://vdsdgfsrf.vercel.app/](https://vdsdgfsrf.vercel.app/).
  - Connects to backend via environment variable: `VITE_API_BASE_URL`.
- **Backend**: Hosted on [Render](https://render.com/) as a Python Web Service.
  - Serves REST endpoints (`/api/v1/summary`, `/api/v1/settlement-periods`, `/api/v1/error-profile`, `/api/v1/health`).
  - Pre-populated with verified January 2026 Elexon data stored in `market_data.db`.

---

## 7. Video Demonstration Outline (Unlisted YouTube)

The accompanying 10-minute video presentation is divided into two 5-minute segments:

### Part 1: Client Pitch (< 5 mins) — *Presenting to the Utility Trading Desk*
1. **The Executive Problem**: Why the January 2026 UK wind fleet posed an extreme financial risk (+1,729 MW systematic over-forecasting).
2. **Operational Desk Walkthrough**:
   - 48-SP Order Matrix for physical scheduling.
   - Quick jumps between typical days (Jan 15) and dynamic storm volatility (Jan 22).
   - How the **MAE Uncertainty Band ($\pm 2,013\text{ MW}$)** sizes the necessary thermal reserve.
3. **Accuracy Lab & Diurnal Profile**: Explaining why peak evening hours (17:00–20:00 UTC) require a heightened thermal buffer to protect cash-out margins.
4. **Data Export**: Demonstrating the instant 48-SP CSV export for direct upload into trading execution systems.

### Part 2: Technical Walkthrough (< 5 mins) — *Review with Tech Lead*
1. **Ingestion & Lookahead Protection**: Deep dive into `etl_pipeline.py` and the strict enforcement of `publishTime <= 11:00 AM UTC (D-1)`.
2. **Dual-Resolution Architecture**: Why 24 native hours was chosen for statistical evaluation versus 48 half-hours for dispatch.
3. **Automated Test Suite**: Running `pytest test_cutoff.py` to prove mathematically that late forecasts are rejected.
4. **Containerization & Deployment**: Walkthrough of the multi-stage `Dockerfile`, Docker Compose setup, and Vercel/Render production deployment.

*(Link to unlisted YouTube video will be provided in the official submission form).*

---

## 8. AI Tool Attribution Statement

In accordance with the assignment guidelines, this application was developed with the assistance of AI tools (Google Antigravity & LLM coding assistants). AI tools were utilized for:
- Accelerating boilerplate generation for the FastAPI backend and React frontend.
- Refactoring the diurnal chart to cleanly accommodate dual-resolution toggles.
- Structuring the multi-stage Docker build and verifying edge cases in Elexon API timestamp parsing.

All domain calculations, gate closure cutoff rules, mathematical metrics, and architectural decisions were independently validated against Elexon BMRS market documentation and Python unit tests.
