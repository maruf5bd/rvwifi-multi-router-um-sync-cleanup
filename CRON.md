# Cron Schedule (on VPS as root)

| Interval | Script | Purpose |
|---|---|---|
| `*/5 * * * *` | `um_orchestrator.py` (legacy) | Ledger-guarded cleanup (kept for compatibility) |
| `*/15 * * * *` | `run_wifi_cleanup.sh` → sync_lite.py (quick) + delete_expired_codes.py | Incremental sync of latest 100 website records + expired code cleanup |
| `0 3 * * *` | `run_wifi_daily.sh` → sync_lite.py --full + delete_expired_codes.py | Daily full re-scan of all 1151+ website records + cleanup |

## Scripts

| File | Purpose |
|---|---|
| `sync_lite.py` | Syncs codes from `rvwifi.hostraj.com/public/l1` to local SQLite DB. `--full` flag re-processes all pages |
| `delete_expired_codes.py` | Multi-source expiry comparison (lite_codes → ledger R2 → derived → ledger R1). Deletes expired UM users from R1/R2/R3 via REST API or SSH |
| `sync_usermanager.py` | 3-way User Manager sync (R1 ↔ R2 ↔ R3). Add-only, no deletions. Pre-populates profiles, limitations, users, user-profiles |
| `um_ledger.py` | SQLite ledger module tracking UM user state across all routers. Used by sync and delete scripts for authority resolution |
| `scrape_lite.py` | Initial scraper (legacy). Parses HTML cards from the website |
| `cleanup_used_users.py` | Legacy ledger-guarded cleanup (72h cutoff) |
| `um_orchestrator.py` | Legacy cron launcher for cleanup_used_users.py |
