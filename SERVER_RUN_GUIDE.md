# SERVER RUN GUIDE — reproduce the experiments, start to finish

This is the exact recipe that produced the published results (July 2026), including every problem we hit and its fix. Follow it in order and a fresh Ubuntu server produces all four result sets in ~2–3 hours. Written so that future-you (or a reviewer's question) never has to reconstruct anything.

**You need:** a fresh Ubuntu server (20.04+; 4 vCPU / 8 GB was enough), SSH access (`ubuntu@SERVER_IP`), and on your PC: the `catalog-dq` repo folder + the `dataset` folder (hospital, flights, beers, rayyan, movies_1, toy — each with clean.csv + dirty.csv; from github.com/BigDaMa/raha).

---

## 1. Prepare and upload (PowerShell on your PC)

```powershell
$BASE = "C:\path\to\your\folder"     # contains catalog-dq and dataset
$SERVER = "ubuntu@SERVER_IP"

# datasets must live INSIDE the repo: catalog-dq\datasets\<name>\clean.csv
Remove-Item -Recurse -Force "$BASE\catalog-dq\datasets" -ErrorAction SilentlyContinue
Copy-Item -Recurse "$BASE\dataset" "$BASE\catalog-dq\datasets"

# stale caches cause weird bugs — always clear before upload
Get-ChildItem "$BASE\catalog-dq" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

ssh $SERVER "rm -rf ~/catalog-dq ~/results ~/*.log"     # clean slate
scp -r "$BASE\catalog-dq" ${SERVER}:~/
ssh $SERVER
```

## 2. Install PostgreSQL 16 + Python env (on the server, once)

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
sudo apt install -y postgresql-16

sudo -u postgres psql -c "CREATE USER catalogdq WITH PASSWORD 'catalogdq';"
sudo -u postgres psql -c "CREATE DATABASE catalog_dq OWNER catalogdq;"
# re-running later? reset instead:
# sudo -u postgres psql -c "DROP DATABASE IF EXISTS catalog_dq;"
# sudo -u postgres psql -c "CREATE DATABASE catalog_dq OWNER catalogdq;"

cd ~/catalog-dq
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 3. The check chain — never skip, never reorder

```bash
# [1] every file compiles (catches truncated/corrupted uploads)
python -m compileall -q catalog_dq run_experiments.py smoke_test.py test_fixes.py && echo "COMPILE OK"
# [2] unit tests for the three historical bug-fixes (tz, type-blind injector, F2 timing)
python test_fixes.py        # expect 8/8
# [3] end-to-end on the toy dataset
python smoke_test.py        # expect PASSED, F1=1.000 all three methods
```

## 4. Trial run — 10–15 min, catches everything the full run would waste a night on

```bash
DSN="host=localhost port=5432 dbname=catalog_dq user=catalogdq password=catalogdq"

python run_experiments.py --db postgres --dsn "$DSN" \
  --dataset-dir datasets --datasets hospital flights beers rayyan movies_1 \
  --reps 2 --K 5 --z 2.0 --out-dir results_trial 2>&1 | tee trial.log
```

**Acceptance — all four must pass before the real run:**

```bash
grep -c "ERROR" trial.log                # must print 0
awk -F, 'NR>1 {s += $NF} END {print "n_errors:", s}' results_trial/postgres/results_summary.csv   # must print 0
# the range-injection fix must be alive (beers/movies_1 were silently dead once):
python3 -c "
import csv
rows = [r for r in csv.DictReader(open('results_trial/postgres/results_quality.csv'))
        if r['fault_type']=='range' and r['dataset'] in ('beers','movies_1')]
bad = [r for r in rows if int(r['n_injected'])==0 or r['status']!='ok']
assert rows and not bad, 'RANGE INJECTION BROKEN'
print('RANGE FIX OK')"
head -3 results_trial/postgres/results_summary.csv    # eyeball: F1 neither all 0 nor all 1
```

## 5. The full runs — one chained job, then walk away (~2–3 h total)

```bash
nohup bash -c "
python run_experiments.py --db postgres --dsn '$DSN' --dataset-dir datasets --datasets hospital flights beers rayyan movies_1 --reps 20 --K 5 --z 2.0 --out-dir results > pg.log 2>&1 &&
python run_experiments.py --db sqlite --dataset-dir datasets --datasets hospital flights beers rayyan movies_1 --reps 20 --K 5 --z 2.0 --out-dir results > sqlite.log 2>&1 &&
python run_experiments.py --db postgres --dsn '$DSN' --dataset-dir datasets --datasets hospital flights beers rayyan movies_1 --reps 20 --K 5 --z 2.0 --statistics-target 10 --out-dir results/sensitivity_10 > sens10.log 2>&1 &&
python run_experiments.py --db postgres --dsn '$DSN' --dataset-dir datasets --datasets hospital flights beers rayyan movies_1 --reps 20 --K 5 --z 2.0 --statistics-target 1000 --out-dir results/sensitivity_1000 > sens1000.log 2>&1 &&
echo ALL_FOUR_DONE > ALL_DONE.txt
" > chain.log 2>&1 &
exit
```

The `&&` chain stops at the first failure — a broken run can never poison the ones after it.

## 6. Collect and verify (PowerShell, later)

```powershell
ssh $SERVER "ls ~/catalog-dq/ALL_DONE.txt && grep -c ERROR ~/catalog-dq/pg.log ~/catalog-dq/sqlite.log ~/catalog-dq/sens10.log ~/catalog-dq/sens1000.log"
scp -r ${SERVER}:~/catalog-dq/results "$BASE\results_$(Get-Date -Format yyyyMMdd)"
```

Expected shape: each `results_summary.csv` = 181 rows (180 cells + header), each `results_quality.csv` = 3,601 rows, `n_errors` column all zeros. Sanity in Excel: five datasets present, F1 with texture (swap and 1%-severity cells low), catalog latency ≪ scan latency.

## 7. Troubleshooting — every failure we actually hit, and the fix

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: catalog_dq.harness_pg` | incomplete/stale upload | re-upload the FULL repo; clear `__pycache__` both sides |
| `SyntaxError` mid-file at upload | file truncated in transfer/edit | `wc -l` against the source; re-copy the file whole, never patch in place |
| `can't subtract offset-naive and offset-aware datetimes` | old code (tz bug) | you have a pre-fix version — use the final repo; `test_fixes.py` guards this |
| `invalid input syntax for type double precision: "empty"` | type-blind range injector (pre-fix) | same — final repo only; the trial's RANGE FIX check guards it |
| `column "rating_value" does not exist` | column-name normalization mismatch (pre-fix) | same as above |
| Errors counted as F1=0 rows | ancient harness | final harness records status=error and excludes them; if you ever see mean_f1=0.000 with "N% cells done", stop — that's corrupted results, not data |
| Docker instead of native PG | no root, or preference | Docker Hub blocks Iranian IPs — set registry mirror `docker.arvancloud.ir` in Docker settings, or run from a EU server |

## The three rules this guide encodes

1. **Trial before full.** The 15-minute trial caught every disaster before it cost a night.
2. **Never trust a green run — grep it.** "Done." at the end of a log proves nothing; `grep -c ERROR` and the n_errors column do.
3. **Logs are evidence.** Keep pg.log/sqlite.log/sens*.log with the results; they are the answer to «این عدد از کجا آمد؟».
