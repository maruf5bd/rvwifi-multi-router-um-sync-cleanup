#!/usr/bin/env python3
"""Delete expired WiFi codes from RouterOS routers.
Authority: lite_codes table, with multi-source fallback for blank expiries."""

import sqlite3, urllib.request, json, base64, ssl, paramiko, os
from datetime import datetime, timedelta
import re

R2_API = "http://10.88.89.2:80/rest"
R2_AUTH = "chr:maruf123"
R1_API = "http://192.168.88.2/rest"
R1_AUTH = "admin:maruf123"
R2_1_API = "http://10.88.89.1:80/rest"
R2_1_AUTH = "chr:maruf123"
R3_SSH = {"ip": "10.99.99.2", "port": 22, "user": "chr", "pass": "maruf123"}

DB = "/root/sent_codes.db"
LEDGER_DB = "/root/um_ledger.db"
LOG = "/root/delete_expired_log.json"
ctx = ssl._create_unverified_context()

PACKAGE_DURATIONS = {
    "30days WIFI": timedelta(days=30),
    "20 Days": timedelta(days=20),
    "20 Days WIFI": timedelta(days=20),
    "20days WIFI": timedelta(days=20),
    "15days WIFI": timedelta(days=15),
    "7 Days WIFI": timedelta(days=7),
    "7days WIFI": timedelta(days=7),
    "3days WIFI": timedelta(days=3),
    "24h WIFI": timedelta(days=1),
    "Mobile+Laptop": None,
}

def parse_package_duration(pkg):
    if not pkg:
        return None
    p = pkg.strip()
    if p in PACKAGE_DURATIONS:
        return PACKAGE_DURATIONS[p]
    m = re.search(r'(\d+)\s*(days?|d)', p, re.IGNORECASE)
    if m:
        return timedelta(days=int(m.group(1)))
    m = re.search(r'(\d+)\s*(hours?|h)', p, re.IGNORECASE)
    if m:
        return timedelta(hours=int(m.group(1)))
    return None

def parse_dt(s):
    if not s or s.strip() == "" or s == "not-yet-running":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo:
                d = d.replace(tzinfo=None)
            return d
        except:
            continue
    return None

def resolve_expiry(code, lite_expiry, lite_sent_at, lite_pkg, sent_pa, sent_pkg, ledger_db):
    """Returns (expiry_datetime, source) or (None, reason).
    Collects ALL sources, then compares:
    - 2+ sources same date → use that
    - All within 12h → use most authoritative
    - Otherwise → skip (ambiguous)
    """
    sources = []

    # 1. lite_codes expiry_date (most authoritative)
    d = parse_dt(lite_expiry)
    if d:
        sources.append((d, "lite_codes"))

    # 2. Ledger R2 end_time
    if ledger_db:
        try:
            row = ledger_db.execute(
                "SELECT end_time FROM um_user_profiles WHERE user=? AND source=? AND op!=? ORDER BY id DESC LIMIT 1",
                (code, "r2", "delete")
            ).fetchone()
            if row and row[0]:
                d = parse_dt(row[0])
                if d:
                    sources.append((d, "ledger_r2"))
        except:
            pass

    # 3. Derive from sent_at + package
    for ts, pkg in [(lite_sent_at, lite_pkg), (sent_pa, sent_pkg)]:
        if ts and pkg:
            ts_d = parse_dt(ts)
            dur = parse_package_duration(pkg)
            if ts_d and dur:
                sources.append((ts_d + dur, f"derived({pkg})"))

    # 4. Ledger R1 end_time
    if ledger_db:
        try:
            row = ledger_db.execute(
                "SELECT end_time FROM um_user_profiles WHERE user=? AND source=? AND op!=? ORDER BY id DESC LIMIT 1",
                (code, "r1", "delete")
            ).fetchone()
            if row and row[0]:
                d = parse_dt(row[0])
                if d:
                    sources.append((d, "ledger_r1"))
        except:
            pass

    if not sources:
        return (None, "no_expiry_found")

    # Check for consensus on FULL (pre-dedup) sources:
    # if 2+ different sources have the exact same timestamp, that's consensus
    from collections import Counter
    full_counts = Counter(dt.strftime("%Y-%m-%d %H:%M:%S") for dt, _ in sources)
    top_date_str, top_count = full_counts.most_common(1)[0]
    if top_count >= 2:
        consensus_dt = datetime.strptime(top_date_str, "%Y-%m-%d %H:%M:%S")
        return (consensus_dt, f"consensus({top_count}sources)")

    # Dedup same-minute dates for spread comparison (e.g. derived from two cols)
    seen = set()
    deduped = []
    for dt, src in sources:
        key = dt.strftime("%Y-%m-%d %H:%M")
        if key not in seen:
            seen.add(key)
            deduped.append((dt, src))
    sources = deduped

    if len(sources) == 1:
        return sources[0]

    # If ALL sources show past dates, accept regardless of spread
    now = datetime.now()
    if all(dt < now for dt, _ in sources):
        return sources[0]

    # All within 12h → use most authoritative (first = lite_codes)
    min_dt = min(dt for dt, _ in sources)
    max_dt = max(dt for dt, _ in sources)
    spread = abs((max_dt - min_dt).total_seconds()) / 3600
    if spread <= 12:
        return sources[0]

    print(f"  AMBIGUOUS {code}: {[(dt.strftime('%Y-%m-%d %H:%M'), src) for dt, src in sources]} (spread={spread:.1f}h)")
    return (None, f"ambiguous(spread={spread:.1f}h)")

def rest_get(url, auth):
    r = urllib.request.Request(url)
    r.add_header("Authorization", f"Basic {base64.b64encode(auth.encode()).decode()}")
    try:
        with urllib.request.urlopen(r, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

def rest_delete(url, auth):
    r = urllib.request.Request(url, method="DELETE")
    r.add_header("Authorization", f"Basic {base64.b64encode(auth.encode()).decode()}")
    try:
        with urllib.request.urlopen(r, timeout=15, context=ctx) as resp:
            return resp.status in (200, 204)
    except:
        return None

def ssh_run(host, port, user, pw, cmds):
    if not cmds: return
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=port, username=user, password=pw, look_for_keys=False, allow_agent=False, timeout=10)
        stdin, stdout, stderr = ssh.exec_command("\n".join(cmds))
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        if err: print(f"  SSH err on {host}: {err[:200]}")
        ssh.close()
    except Exception as e:
        print(f"  SSH failed {host}:{port}: {e}")

def main():
    now = datetime.now()
    db = sqlite3.connect(DB)
    ledger_db = None
    if os.path.exists(LEDGER_DB):
        try:
            ledger_db = sqlite3.connect(LEDGER_DB)
        except:
            pass

    # Get codes WHERE lite_codes.has_db='1' OR sent_codes.has_db='1'
    # LEFT JOIN sent_codes to fill in expiry info for blank-expiry cases
    codes_to_check = {}
    rows = db.execute("""
        SELECT l.code, l.expiry_date, l.sent_at, l.package,
               s.processed_at, s.package
        FROM lite_codes l
        LEFT JOIN sent_codes s ON s.code_db = l.code OR s.code_json = l.code
        WHERE l.has_db = '1' OR s.has_db = '1'
    """).fetchall()
    for r in rows:
        code = r[0].strip()
        info = {"lite_expiry": r[1], "lite_sent_at": r[2], "lite_pkg": r[3]}
        if r[4]:
            info["sent_pa"] = r[4]
        if r[5]:
            info["sent_pkg"] = r[5]
        codes_to_check[code] = info
    db.close()

    # 3. Resolve expiry for each code
    expired_codes = set()
    skipped = 0
    for code, info in codes_to_check.items():
        expiry_dt, source = resolve_expiry(
            code,
            info.get("lite_expiry"),
            info.get("lite_sent_at"),
            info.get("lite_pkg"),
            info.get("sent_pa"),
            info.get("sent_pkg"),
            ledger_db
        )
        if expiry_dt:
            if expiry_dt < now:
                expired_codes.add(code)
        else:
            skipped += 1

    if ledger_db:
        ledger_db.close()

    print(f"Total codes with has_db=1: {len(codes_to_check)}")
    print(f"Expired (resolved): {len(expired_codes)}")
    print(f"Skipped (no expiry): {skipped}")

    if not expired_codes:
        print("Nothing to delete.")
        return

    # 4. Pre-fetch R2 entities
    print("\nPre-fetching R2 (10.88.89.2:80)...")
    r2_users = rest_get(f"{R2_API}/user-manager/user", R2_AUTH) or []
    r2_profiles = rest_get(f"{R2_API}/user-manager/user-profile", R2_AUTH) or []
    r2_um_ipb = rest_get(f"{R2_API}/user-manager/ip-binding", R2_AUTH) or []
    r2_um_sess = rest_get(f"{R2_API}/user-manager/session", R2_AUTH) or []
    r2_hs_ipb = rest_get(f"{R2_API}/ip/hotspot/ip-binding", R2_AUTH) or []
    r2_hs_sess = rest_get(f"{R2_API}/ip/hotspot/active", R2_AUTH) or []

    map_r2_users = {u["name"]: u[".id"] for u in r2_users if "name" in u and ".id" in u}
    map_r2_profiles = {p["user"]: p[".id"] for p in r2_profiles if "user" in p and ".id" in p}
    map_r2_um_ipb = {b["user"]: b[".id"] for b in r2_um_ipb if "user" in b and ".id" in b}
    map_r2_um_sess = {s["user"]: s[".id"] for s in r2_um_sess if "user" in s and ".id" in s}
    map_r2_hs_ipb = {b.get("comment",""): b[".id"] for b in r2_hs_ipb if "comment" in b and ".id" in b}
    map_r2_hs_sess = {s["user"]: s[".id"] for s in r2_hs_sess if "user" in s and ".id" in s}

    # 5. Pre-fetch R1
    print("Pre-fetching R1 (192.168.88.2)...")
    r1_users = rest_get(f"{R1_API}/user-manager/user", R1_AUTH) or []
    r1_profiles = rest_get(f"{R1_API}/user-manager/user-profile", R1_AUTH) or []
    r1_hs_ipb = rest_get(f"{R1_API}/ip/hotspot/ip-binding", R1_AUTH) or []
    r1_hs_sess = rest_get(f"{R1_API}/ip/hotspot/active", R1_AUTH) or []

    map_r1_users = {u["name"]: u[".id"] for u in r1_users if "name" in u and ".id" in u}
    map_r1_profiles = {p["user"]: p[".id"] for p in r1_profiles if "user" in p and ".id" in p}
    map_r1_hs_ipb = {b.get("comment",""): b[".id"] for b in r1_hs_ipb if "comment" in b and ".id" in b}
    map_r1_hs_sess = {s["user"]: s[".id"] for s in r1_hs_sess if "user" in s and ".id" in s}

    # 5b. Pre-fetch R2_1 (10.88.89.1)
    print("Pre-fetching R2_1 (10.88.89.1)...")
    r21_users = rest_get(f"{R2_1_API}/user-manager/user", R2_1_AUTH) or []
    r21_profiles = rest_get(f"{R2_1_API}/user-manager/user-profile", R2_1_AUTH) or []
    r21_hs_ipb = rest_get(f"{R2_1_API}/ip/hotspot/ip-binding", R2_1_AUTH) or []
    r21_hs_sess = rest_get(f"{R2_1_API}/ip/hotspot/active", R2_1_AUTH) or []
    map_r21_users = {u["name"]: u[".id"] for u in r21_users if "name" in u and ".id" in u}
    map_r21_profiles = {p["user"]: p[".id"] for p in r21_profiles if "user" in p and ".id" in p}
    map_r21_hs_ipb = {b.get("comment",""): b[".id"] for b in r21_hs_ipb if "comment" in b and ".id" in b}
    map_r21_hs_sess = {s["user"]: s[".id"] for s in r21_hs_sess if "user" in s and ".id" in s}

    matched = expired_codes & set(map_r2_users.keys())
    print(f"Matched {len(matched)} expired codes on R2")

    # 6. Delete from R2 + R1
    log_entries = []
    done = 0
    errors = 0
    for name in sorted(matched):
        entry = {"username": name}
        for router in ["r2", "r1", "r21"]:
            if router == "r2":
                maps = {
                    "users": map_r2_users, "profiles": map_r2_profiles,
                    "um_ipb": map_r2_um_ipb, "um_sess": map_r2_um_sess,
                    "hs_ipb": map_r2_hs_ipb, "hs_sess": map_r2_hs_sess,
                }
                api, auth = R2_API, R2_AUTH
            elif router == "r1":
                maps = {
                    "users": map_r1_users, "profiles": map_r1_profiles,
                    "um_ipb": {}, "um_sess": {},
                    "hs_ipb": map_r1_hs_ipb, "hs_sess": map_r1_hs_sess,
                }
                api, auth = R1_API, R1_AUTH
            else:
                maps = {
                    "users": map_r21_users, "profiles": map_r21_profiles,
                    "um_ipb": {}, "um_sess": {},
                    "hs_ipb": map_r21_hs_ipb, "hs_sess": map_r21_hs_sess,
                }
                api, auth = R2_1_API, R2_1_AUTH
            for key, m in [("um_sess", maps["um_sess"]), ("hs_sess", maps["hs_sess"]),
                           ("um_ipb", maps["um_ipb"]), ("hs_ipb", maps["hs_ipb"]),
                           ("profile", maps["profiles"]), ("user", maps["users"])]:
                eid = m.get(name)
                if eid:
                    path = f"/user-manager/session/{eid}" if key == "um_sess" else \
                           f"/ip/hotspot/active/{eid}" if key == "hs_sess" else \
                           f"/user-manager/ip-binding/{eid}" if key == "um_ipb" else \
                           f"/ip/hotspot/ip-binding/{eid}" if key == "hs_ipb" else \
                           f"/user-manager/user-profile/{eid}" if key == "profile" else \
                           f"/user-manager/user/{eid}"
                    ok = rest_delete(f"{api}{path}", auth)
                    entry[f"{router}_{key}"] = ok
                    if key == "user" and ok:
                        done += 1
                    elif key == "user" and not ok:
                        errors += 1
        log_entries.append(entry)
        if done % 50 == 0 and done > 0:
            print(f"  Deleted {done}/{len(matched)}...")

    print(f"\nR2+R1: {done} users deleted, {errors} errors")

    # 7. R3 SSH cleanup
    print("\nChecking R3 (10.99.99.2)...")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(R3_SSH["ip"], port=R3_SSH["port"], username=R3_SSH["user"],
                    password=R3_SSH["pass"], look_for_keys=False, allow_agent=False, timeout=10)
        stdin, stdout, stderr = ssh.exec_command('/ip hotspot ip-binding print terse\n/ip hotspot active print terse')
        out = stdout.read().decode('utf-8', errors='ignore')
        r3_bindings = {}
        r3_sessions = {}
        for line in out.split("\n"):
            parts = line.split()
            id_part = parts[0] if parts else ""
            for p in parts:
                if p.startswith("comment="):
                    c = p.split("=", 1)[1].strip('"')
                    r3_bindings[c] = id_part
                if p.startswith("user="):
                    u = p.split("=", 1)[1].strip('"')
                    r3_sessions[u] = id_part
        ssh.close()
        r3_cmds = []
        for c in set(expired_codes) & set(r3_bindings.keys()):
            r3_cmds.append(f'/ip hotspot ip-binding remove [find comment="{c}"]')
        for c in set(expired_codes) & set(r3_sessions.keys()):
            r3_cmds.append(f'/ip hotspot active remove [find user="{c}"]')
        if r3_cmds:
            print(f"  R3: {len(r3_cmds)} commands")
            ssh_run(R3_SSH["ip"], R3_SSH["port"], R3_SSH["user"], R3_SSH["pass"], r3_cmds)
        else:
            print("  R3: no matches")
    except Exception as e:
        print(f"  R3 SSH failed: {e}")

    if log_entries:
        existing = []
        if os.path.exists(LOG):
            try:
                with open(LOG) as f: existing = json.load(f)
            except: pass
        existing.extend(log_entries)
        with open(LOG, "w") as f: json.dump(existing, f, indent=2)
        print(f"\nLogged {len(log_entries)} deletions to {LOG}")
    print("\nDone.")

if __name__ == "__main__":
    main()
