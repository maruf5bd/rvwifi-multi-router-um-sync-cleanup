# RV WiFi Multi-Router UM Sync & Cleanup

Automated User Manager sync and expired code cleanup for a 3-router MikroTik hotspot network.

## Infrastructure

| Router | Host | API Port | SSH Port | UM Users | Role |
|--------|------|----------|----------|----------|------|
| R1 (CHR) | 192.168.88.2 | 80 | 22 | 983 | Core VM, user sync target |
| R2_1 | 10.88.89.1 | 80 | 2225 | 983 | Client router (synced with R1) |
| R2 | 10.88.89.2 | 80 | 2225 | 695 | Client router, primary UM |
| R3 | 10.99.99.2 | 8080 | 22 | 695 | Client router, hotspot only |

## Scripts

### `sync_usermanager.py`
3-way add-only sync across R1, R2_1, and R3. Batches SSH commands 10-at-a-time for performance. Tracks synced state in `sync_store.json` to avoid duplicates.

**Pairs:** R1↔R2_1, R1↔R3. Syncs profiles → limitations → users → user-profiles in that order.

### `delete_expired_codes.py`
Multi-source expiry comparison: scraped website codes (`lite_codes`) vs ledger snapshots (`um_ledger.db`) vs package derivation. Deletes expired UM users from all routers via REST API (primary) or SSH (fallback).

**Expiry resolution (4 tiers):**
1. lite_codes.expiry_date (website)
2. um_ledger.db R2 snapshot
3. sent_at + package derivation
4. um_ledger.db R1 snapshot

**Comparison logic:** single source → accept; 2+ same → accept (consensus); all within 12h → accept most authoritative; spread >12h → skip unless ALL sources are in the past.

**Deletion order:** sessions → IP bindings → user-profile → user (per router).

### `sync_lite.py`
Syncs codes from the website's lite page (`https://rvwifi.hostraj.com/public/l1/`) into SQLite. Quick mode fetches latest 100; `--full` paginates through all records.

### `scrape_lite.py`
Original scraper for the lite page. Fetches all pages, parses HTML card data-* attributes.

### `um_ledger.py`
SQLite-based ledger that records snapshot data from all routers (users, profiles, user-profiles, verify status). Used as a secondary expiry source by the delete script.

### `cleanup_used_users.py`
Legacy cleanup script. Checks R1 user-profiles with state=used and end-time >72h ago, uses `um_ledger.is_deletable()` guard. Only deletes when the ledger's authoritative view confirms expiry.

### `um_orchestrator.py`
Cron entry point for the legacy 5-minute cleanup cycle.

### `run_wifi_cleanup.sh`
Cron entry point: `sync_lite.py` (quick 100) → `delete_expired_codes.py`.

### `run_wifi_daily.sh`
Cron entry point: `sync_lite.py --full` → `delete_expired_codes.py`.

## Cron Schedule

| Frequency | Script | Purpose |
|-----------|--------|---------|
| Every 5 min | `um_orchestrator.py` | Legacy ledger-guarded cleanup |
| Every 15 min | `run_wifi_cleanup.sh` | Incremental sync + expired deletion |
| 3:00 AM daily | `run_wifi_daily.sh` | Full re-sync + expired deletion |

All crons run on the Proxmox VPS host.

## Data Files

| File | Size | Contents |
|------|------|----------|
| `masterum.json` | 24K | Active UM user-profiles from R1 |
| `sync_store.json` | 1.9M | Sync tracking state (what's been synced between routers) |
| `sync_cache.json` | 445K | Cached API response snapshots |
| `cleanup_log.json` | 774K | Historical cleanup deletion log |
| `delete_expired_log.json` | 85K | Expired code deletion history |
| `cleanup_batch_log.json` | 244K | Batch deletion log |

## Key Bug Fixes

1. **`_fix_disabled()`** — REST API returns `"disabled": false` as JSON boolean. Python's `str(False)` is `"False"` (capital F). RouterOS SSH CLI only accepts `disabled=no` (lowercase). The fix converts Python `False` → `"no"`, `True` → `"yes"`.

2. **Syntax error detection** — `ssh_batch_cmd` now counts both `failure:` and `syntax error` lines as failures. RouterOS outputs all errors to stdout, not stderr.

3. **Blank expiry** — SQLite's `'' < datetime('now')` evaluates TRUE. Fixed with `AND expiry_date != '' AND expiry_date IS NOT NULL`.

4. **Dual has_db check** — After website resets `has_db` to 0 on all rows, the delete script checks BOTH `lite_codes.has_db` and `sent_codes.has_db` to avoid missing candidates.
