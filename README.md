# socso-radar

> Automated Malaysian IC → PERKESO SOCSO lookup. Runs on GitHub Actions every 30 minutes and saves results to sharded JSON files that never outgrow the repo.

**Live dashboard →** `https://YOUR-USERNAME.github.io/socso-radar/`

---

## What it does

1. Generates valid 12-digit Malaysian IC numbers using the real checksum algorithm (ISO 7064 Mod 11,2), weighted toward high-population states (Selangor, KL, Johor, Penang)
2. Checks each IC against the PERKESO API — 8 parallel workers, adaptive backoff, retries on 5xx/timeouts
3. Saves confirmed records permanently, sharded by birth year
4. Caches invalid results with a 30-day expiry (re-checks automatically, since people register with PERKESO when they start new jobs)
5. Logs every run, keeps hit statistics and resumes from a saved cursor

---

## How it keeps going

**Nothing is ever allowed to grow past a size cap.** All results are written in
shards of at most 25 MB (GitHub rejects single blobs over 100 MB):

```
ic_results/valid_1990.json      # one file per birth year
ic_results/valid_1996_a.json    # auto-split further if a year gets big
ic_results/manifest.json        # index the dashboard reads
ic_results/master_invalid_a.json
ic_results/invalid_manifest.json
```

An oversized shard is split, never fatal. Records are stored as arrays
(`["900830075700","2026-07-15","NAME",3]`) — about a third of the bytes of the
old key-per-record JSON, and the dashboard unpacks them on load.

**Coverage moves forward.** Each year-range job keeps a cursor in
`ic_results/cursors/`, so a run picks up where the last one stopped instead of
re-checking a random slice. Hits are exploited immediately (nearby sequences of
the same DOB/state), and states/years that produced hits get a bigger share of
future probes via `ic_results/stats.json`.

**Every run is logged** to `logs/run-log.ndjson` (checked / valid / invalid /
rate / cursor) and to the Actions job summary; merged data is also uploaded as
the `merged-results` artifact so a failed push never loses a scan.

---

## Files in this repo

```
socso-radar/
├── ic_checker.py                    main script
├── requirements.txt                 pip install requests
├── index.html                       GitHub Pages dashboard
├── logs/run-log.ndjson              automatic per-run log
├── .github/workflows/
│   └── run_checker.yml              runs every 30 min
└── ic_results/
    ├── valid_<year>[_x].json        valid hits, sharded by birth year
    ├── manifest.json                shard index (counts, years)
    ├── master_invalid[_x].json      invalid cache, 30-day expiry
    ├── invalid_manifest.json        invalid shard index
    ├── cursors/cursor_*.json        sweep position per year range
    └── stats.json                   hit counts per state/year/sequence
```

`master_valid.json` / `master_invalid.json` are the pre-sharding names; the
script still reads them, and the merge replaces them with shards.

---

## Setup (5 minutes)

### 1 — Create repo

Create a new GitHub repo named `socso-radar` and push all these files.

### 2 — Allow Actions to write to the repo

**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**

Without this the workflow can commit new results to the JSON files.

### 3 — Enable GitHub Pages

**Settings → Pages → Source: Deploy from branch → Branch: main → Folder: / (root) → Save**

Your dashboard will be live at `https://YOUR-USERNAME.github.io/socso-radar/`

### 4 — Done

The first scheduled run happens at 02:00 UTC. You can also trigger it immediately:
**Actions → PERKESO IC Checker → Run workflow**

---

## Running locally

```bash
pip install -r requirements.txt

# Interactive
python ic_checker.py

# Non-interactive
python ic_checker.py --mode 1 --max-checks 200 --workers 3
python ic_checker.py --mode 2 --date 1985-04-27 --pb 10
python ic_checker.py --mode 3 --ics 610212075425,920913235001
```

| Flag | Default | Description |
|---|---|---|
| `--mode` | interactive | 1 = smart random, 2 = full DOB sweep, 3 = manual IC list |
| `--workers` | 3 | Concurrent threads (1–5) |
| `--from-year` | 1965 | Earliest birth year to generate |
| `--to-year` | 2000 | Latest birth year to generate |
| `--max-checks` | unlimited | Stop after N checks |
| `--max-runtime` | unlimited | Stop after N minutes |
| `--seq-band` | 500-650 / 0-500 | Sequence range to explore, e.g. `0-999` to widen coverage |
| `--no-cursor` | off | Ignore the saved cursor and stay purely random |
| `--date` | random | YYYY-MM-DD for mode 2 |
| `--pb` | 10 | State code for mode 2 (10 = Selangor) |
| `--ics` | — | Comma-separated ICs for mode 3 |
| `--ttl` | 30 | Days before invalid records expire and get re-checked |

---

## IC number structure

```
6 1 0 2 1 2   0 7   5 4 2   5
└───────────┘ └──┘ └─────┘ └─┘
   YYMMDD      PB    SEQ   CHK
 date of birth  ↑   unique  gender + ISO 7064 checksum
            state code
```

Each 12-digit number is a unique identifier. `610212-07-5425` and `850427-07-5425` are completely different ICs — marking one invalid never affects the other.

---

## Why invalids expire after 30 days

A person registers with PERKESO when their employer signs them up — typically when they start a new job. An IC that was invalid today could be valid next month. Expiring invalid cache entries ensures the tool re-checks them periodically and picks up newly registered workers.

Valid records are permanent — a person's name and DOB don't change once found.
