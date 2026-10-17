# PostgreSQL Experiment Run Guide (Path B)

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Docker Desktop | 24+ |
| Python | 3.10+ |
| psycopg2-binary | 2.9+ |

Install Python deps (once):
```bash
cd catalog-dq
pip install -r requirements.txt
```

---

## Step 1 — Start PostgreSQL 16

```bash
cd catalog-dq
docker compose up -d
```

Wait ~10 seconds, then verify:
```bash
docker compose logs postgres | tail -5
# Expected: "database system is ready to accept connections"
```

---

## Step 2 — Copy datasets into place

The harness expects this layout (already set up via symlinks if you ran the
SQLite setup, otherwise copy manually):

```
catalog-dq/
└── datasets/
    ├── hospital/clean.csv
    ├── flights/clean.csv
    ├── beers/clean.csv
    ├── rayyan/clean.csv
    └── movies_1/clean.csv
```

---

## Step 3 — Run the full PostgreSQL experiment matrix

```bash
python run_experiments.py \
    --db postgres \
    --dsn "host=localhost port=5432 dbname=catalog_dq user=catalogdq password=catalogdq" \
    --dataset-dir datasets \
    --datasets hospital flights beers rayyan movies_1 \
    --reps 20 \
    --K 5 \
    --z 2.0 \
    --out-dir results
```

This runs **3,600 quality measurement runs** (5 datasets × 4 fault types ×
3 severities × 3 methods × 20 reps). Expected runtime: **2–4 hours**
depending on your machine. Safe to run overnight.

Progress is printed every 10 runs and each row is flushed to CSV immediately,
so you can inspect partial results if needed.

---

## Step 4 — Paste results back

When finished, you will have:

```
results/postgres/results_quality.csv   (~3,600 rows)
results/postgres/results_summary.csv   (~180 rows, one per cell)
```

These **two CSV files** are the inputs for the result tables and figures (mean F1 ± 95% bootstrap CI per cell, response-time comparison
bar chart, sensitivity analysis).

---

## Step 5 — Shut down PostgreSQL

```bash
docker compose down
```

To also delete the PostgreSQL data volume:
```bash
docker volume rm catalog-dq_pgdata
```

---

## DSN reference

| Parameter | Value |
|-----------|-------|
| host | localhost |
| port | 5432 |
| dbname | catalog_dq |
| user | catalogdq |
| password | catalogdq |

---

## Troubleshooting

**Port 5432 already in use**
```bash
# Find what is using it
lsof -i :5432   # macOS/Linux
netstat -ano | findstr :5432   # Windows
```
Either stop the existing process or change the port mapping in
`docker-compose.yml` (e.g. `"5433:5432"`) and update the DSN accordingly.

**psycopg2 import error**
```bash
pip install psycopg2-binary --break-system-packages
```

**Permission denied on Docker**
Add your user to the `docker` group (Linux):
```bash
sudo usermod -aG docker $USER
newgrp docker
```

**Very slow runs (>6 hours)**
Reduce `--reps 10` for a first pass; all analysis code works with any rep count
and the bootstrap CI will still be valid (wider interval at n=10 vs n=20).
