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

GRACE_RUNNING_HOURS = 1
GRACE_USED_HOURS = 48

def init_grace_table(db_path=DB):
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_cleanup_grace (
                code TEXT PRIMARY KEY,
                expired_at TEXT,
                first_detected_running TEXT,
                last_detected_running TEXT,
                used_at TEXT
            )
        """)
        con.commit()
        con.close()
    except Exception as e:
        print(f"Warning: Failed to init code_cleanup_grace table: {e}")

def get_grace_records(db_path=DB):
    init_grace_table(db_path)
    records = {}
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        for row in cur.execute("SELECT * FROM code_cleanup_grace").fetchall():
            records[row["code"]] = dict(row)
        con.close()
    except Exception as e:
        print(f"Warning: Failed to fetch code_cleanup_grace: {e}")
    return records

def update_grace_record(code, expired_at_str, is_running, db_path=DB, now_dt=None):
    now_dt = now_dt or datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT code, expired_at, first_detected_running, last_detected_running, used_at FROM code_cleanup_grace WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            if is_running:
                cur.execute(
                    "INSERT INTO code_cleanup_grace (code, expired_at, first_detected_running, last_detected_running, used_at) VALUES (?, ?, ?, ?, NULL)",
                    (code, expired_at_str, now_str, now_str)
                )
            else:
                cur.execute(
                    "INSERT INTO code_cleanup_grace (code, expired_at, first_detected_running, last_detected_running, used_at) VALUES (?, ?, NULL, NULL, ?)",
                    (code, expired_at_str, now_str)
                )
        else:
            c, exp, first_run, last_run, used = row
            if is_running:
                first_run_val = first_run if first_run else now_str
                cur.execute(
                    "UPDATE code_cleanup_grace SET expired_at=?, first_detected_running=?, last_detected_running=?, used_at=NULL WHERE code=?",
                    (expired_at_str or exp, first_run_val, now_str, code)
                )
            else:
                used_val = used if used else now_str
                cur.execute(
                    "UPDATE code_cleanup_grace SET expired_at=?, used_at=? WHERE code=?",
                    (expired_at_str or exp, used_val, code)
                )
        con.commit()
        con.close()
    except Exception as e:
        print(f"Warning: Failed to update grace record for {code}: {e}")

def delete_grace_record(code, db_path=DB):
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("DELETE FROM code_cleanup_grace WHERE code=?", (code,))
        con.commit()
        con.close()
    except Exception as e:
        pass

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
    "Mobile+Laptop": timedelta(days=30),
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
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M"
    ):
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
    # Align clock with Bangladesh / Dhaka Time (UTC+6) where routers and users reside
    from datetime import timezone
    dhaka_tz = timezone(timedelta(hours=6))
    now = datetime.now(timezone.utc).astimezone(dhaka_tz).replace(tzinfo=None)
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
        with urllib.request.urlopen(r, timeout=5, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

def rest_delete(url, auth):
    r = urllib.request.Request(url, method="DELETE")
    r.add_header("Authorization", f"Basic {base64.b64encode(auth.encode()).decode()}")
    try:
        with urllib.request.urlopen(r, timeout=5, context=ctx) as resp:
            return resp.status in (200, 204)
    except:
        return None

def _parse_terse_line(line):
    import re as _re
    line=line.strip()
    if not line or line.startswith("Columns:"):
        return None
    line=_re.sub(r"^\s*\d+\s+", "", line)
    disabled=None
    if line.startswith("X ") or line.startswith("! "):
        disabled="yes"
        line=line[2:].lstrip()
    line=_re.sub(r"(end-time|started)=(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", r'\1="\2 \3"', line)
    pat=_re.compile(r'(\S+?)="([^"]*)"|(\S+?)=([^\s"]+)')
    d={}
    for m in pat.finditer(line):
        if m.group(1):
            k=m.group(1); v=m.group(2)
        else:
            k=m.group(3); v=m.group(4)
        d[k]=v
    if disabled:
        d["disabled"]="yes"
    return d if d else None

def _ssh_fetch(router_cfg, ros_path):
    mapping={
        "/user-manager/user": "/user-manager user print terse without-paging",
        "/user-manager/user-profile": "/user-manager user-profile print terse without-paging",
        "/user-manager/session": "/user-manager session print terse without-paging",
        "/ip/hotspot/ip-binding": "/ip hotspot ip-binding print terse without-paging",
        "/ip/hotspot/active": "/ip hotspot active print terse without-paging",
    }
    cmd=mapping.get(ros_path)
    if not cmd:
        return None
    sshc=paramiko.SSHClient()
    sshc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        sshc.connect(router_cfg["ip"], port=router_cfg["port"], username=router_cfg["user"], password=router_cfg["pass"], look_for_keys=False, allow_agent=False, timeout=10)
        stdin, stdout, stderr = sshc.exec_command(cmd)
        out=stdout.read().decode("utf-8", errors="ignore")
        err=stderr.read().decode("utf-8", errors="ignore")
        if "bad command name" in out or "bad command name" in err:
            return []
        res=[]
        for raw in out.splitlines():
            if not raw.strip() or raw.strip().startswith("Flags:") or raw.strip().startswith("Columns:"):
                continue
            if "bad command name" in raw.lower():
                continue
            parsed=_parse_terse_line(raw)
            if parsed:
                res.append(parsed)
        return res
    except Exception as e:
        print(f"SSH fetch EXC {router_cfg.get('name','?')} {ros_path}: {e}")
        return None
    finally:
        try: sshc.close()
        except: pass

def _fetch_with_fallback_rest_or_ssh(url, auth, ros_path):
    # map url to router cfg
    cfg=None
    if url==R1_API:
        cfg={"name":"R1","ip":"192.168.88.2","port":22,"user":"admin","pass":"maruf123"}
    elif url==R2_API:
        cfg={"name":"R2","ip":"10.88.89.2","port":2225,"user":"chr","pass":"maruf123"}
    elif url==R2_1_API:
        cfg={"name":"R21","ip":"10.88.89.1","port":22,"user":"chr","pass":"maruf123"}
    data=rest_get(url+ros_path, auth) if ros_path.startswith("/") else rest_get(url, auth)
    # rest_get expects full url, handle
    if data is not None:
        return data
    if cfg:
        print(f"REST failed {cfg['name']} {ros_path}, trying SSH fallback")
        fb=_ssh_fetch(cfg, ros_path)
        if fb is not None:
            return fb
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
    # Align clock with Bangladesh / Dhaka Time (UTC+6) where routers and users reside
    from datetime import timezone
    dhaka_tz = timezone(timedelta(hours=6))
    now = datetime.now(timezone.utc).astimezone(dhaka_tz).replace(tzinfo=None)
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
        ORDER BY l.id ASC
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
    print("\nPre-fetching R2 (10.88.89.2:80) with SSH fallback...")
    r2_users = _fetch_with_fallback_rest_or_ssh(R2_API, R2_AUTH, "/user-manager/user") or []
    r2_profiles = _fetch_with_fallback_rest_or_ssh(R2_API, R2_AUTH, "/user-manager/user-profile") or []
    r2_um_sess = _fetch_with_fallback_rest_or_ssh(R2_API, R2_AUTH, "/user-manager/session") or []
    r2_hs_ipb = _fetch_with_fallback_rest_or_ssh(R2_API, R2_AUTH, "/ip/hotspot/ip-binding") or []
    r2_hs_sess = _fetch_with_fallback_rest_or_ssh(R2_API, R2_AUTH, "/ip/hotspot/active") or []

    map_r2_users = {u["name"]: u[".id"] for u in r2_users if "name" in u and ".id" in u}
    map_r2_profiles = {p["user"]: p[".id"] for p in r2_profiles if "user" in p and ".id" in p}
    map_r2_um_sess = {s["user"]: s[".id"] for s in r2_um_sess if "user" in s and ".id" in s}
    map_r2_hs_ipb = {b.get("comment",""): b[".id"] for b in r2_hs_ipb if "comment" in b and ".id" in b}
    map_r2_hs_sess = {s["user"]: s[".id"] for s in r2_hs_sess if "user" in s and ".id" in s}

    # 5. Pre-fetch R1
    print("Pre-fetching R1 (192.168.88.2) with SSH fallback...")
    r1_users = _fetch_with_fallback_rest_or_ssh(R1_API, R1_AUTH, "/user-manager/user") or []
    r1_profiles = _fetch_with_fallback_rest_or_ssh(R1_API, R1_AUTH, "/user-manager/user-profile") or []
    r1_hs_ipb = _fetch_with_fallback_rest_or_ssh(R1_API, R1_AUTH, "/ip/hotspot/ip-binding") or []
    r1_hs_sess = _fetch_with_fallback_rest_or_ssh(R1_API, R1_AUTH, "/ip/hotspot/active") or []

    map_r1_users = {u["name"]: u[".id"] for u in r1_users if "name" in u and ".id" in u}
    map_r1_profiles = {p["user"]: p[".id"] for p in r1_profiles if "user" in p and ".id" in p}
    map_r1_hs_ipb = {b.get("comment",""): b[".id"] for b in r1_hs_ipb if "comment" in b and ".id" in b}
    map_r1_hs_sess = {s["user"]: s[".id"] for s in r1_hs_sess if "user" in s and ".id" in s}

    # 5b. Pre-fetch R2_1 (10.88.89.1)
    print("Pre-fetching R2_1 (10.88.89.1) with SSH fallback...")
    r21_users = _fetch_with_fallback_rest_or_ssh(R2_1_API, R2_1_AUTH, "/user-manager/user") or []
    r21_profiles = _fetch_with_fallback_rest_or_ssh(R2_1_API, R2_1_AUTH, "/user-manager/user-profile") or []
    r21_hs_ipb = _fetch_with_fallback_rest_or_ssh(R2_1_API, R2_1_AUTH, "/ip/hotspot/ip-binding") or []
    r21_hs_sess = _fetch_with_fallback_rest_or_ssh(R2_1_API, R2_1_AUTH, "/ip/hotspot/active") or []
    map_r21_users = {u["name"]: u[".id"] for u in r21_users if "name" in u and ".id" in u}
    map_r21_profiles = {p["user"]: p[".id"] for p in r21_profiles if "user" in p and ".id" in p}
    map_r21_hs_ipb = {b.get("comment",""): b[".id"] for b in r21_hs_ipb if "comment" in b and ".id" in b}
    map_r21_hs_sess = {s["user"]: s[".id"] for s in r21_hs_sess if "user" in s and ".id" in s}

    # Match against user accounts, profiles, AND Hotspot IP bindings to catch orphaned bindings
    all_users = set(map_r2_users.keys()) | set(map_r1_users.keys()) | set(map_r21_users.keys()) |                 set(map_r2_hs_ipb.keys()) | set(map_r1_hs_ipb.keys()) | set(map_r21_hs_ipb.keys()) |                 set(map_r2_profiles.keys()) | set(map_r1_profiles.keys()) | set(map_r21_profiles.keys())
    matched = expired_codes & all_users
    print(f"Matched {len(matched)} expired codes on any router (R2:{len(expired_codes & set(map_r2_users.keys()))} R1:{len(expired_codes & set(map_r1_users.keys()))} R21:{len(expired_codes & set(map_r21_users.keys()))})")

    # Pre-calculate active running users across all hotspots and UM sessions
    running_users = set(map_r2_hs_sess.keys()) | set(map_r1_hs_sess.keys()) | set(map_r21_hs_sess.keys())
    for s in r2_um_sess:
        if s.get("active") in ("true", True) and "user" in s:
            running_users.add(s["user"])

    # Load persistent grace status
    grace_records = get_grace_records(DB)

    # 6. Evaluate Grace Periods and Execute Cleanups
    log_entries = []
    done_users = 0
    errors = 0

    print(f"\nProcessing {len(matched)} matched expired codes with grace rules (Running Grace: {GRACE_RUNNING_HOURS}h, Used UM Retention: {GRACE_USED_HOURS}h)...")

    for name in sorted(matched):
        info = codes_to_check.get(name, {})
        exp_dt_str = info.get("lite_expiry") or ""
        rec = grace_records.get(name, {})

        is_running = name in running_users

        # Update grace record in SQLite
        update_grace_record(name, exp_dt_str, is_running, DB, now_dt=now)

        # Re-read or determine timestamps
        first_detected_running_str = rec.get("first_detected_running") if rec else None
        used_at_str = rec.get("used_at") if rec else None

        if is_running and not first_detected_running_str:
            first_detected_running_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if not is_running and not used_at_str:
            used_at_str = now.strftime("%Y-%m-%d %H:%M:%S")

        first_run_dt = parse_dt(first_detected_running_str)
        used_dt = parse_dt(used_at_str)

        # Rule evaluation:
        # 1. If currently running: wait 1h. If running > 1h, delete completely (including UM).
        # 2. If not running (used): clean immediate places (bindings/sessions) right now; wait 48h before UM deletion.
        can_delete_um = False
        can_delete_others = True # clean IP-bindings, sessions, etc.

        if is_running:
            if first_run_dt and (now - first_run_dt) >= timedelta(hours=GRACE_RUNNING_HOURS):
                print(f"  [RUNNING EXPIRED > {GRACE_RUNNING_HOURS}h] {name}: terminating active and deleting from UM")
                can_delete_um = True
                can_delete_others = True
            else:
                elapsed_min = int((now - first_run_dt).total_seconds() / 60) if first_run_dt else 0
                print(f"  [RUNNING GRACE] {name}: currently active ({elapsed_min}m/{GRACE_RUNNING_HOURS*60}m) - skipping deletion")
                can_delete_um = False
                can_delete_others = False
        else:
            # Code is used / not running
            if used_dt and (now - used_dt) >= timedelta(hours=GRACE_USED_HOURS):
                print(f"  [USED > {GRACE_USED_HOURS}h] {name}: 48h elapsed since used - deleting from UM and all places")
                can_delete_um = True
                can_delete_others = True
            else:
                elapsed_h = ((now - used_dt).total_seconds() / 3600) if used_dt else 0.0
                print(f"  [USED GRACE] {name}: used {elapsed_h:.1f}h/{GRACE_USED_HOURS}h ago - cleaning IP-binding/sessions, retaining UM")
                can_delete_um = False
                can_delete_others = True

        entry = {"username": name, "is_running": is_running, "delete_um": can_delete_um, "delete_others": can_delete_others}

        for router in ["r2", "r1", "r21"]:
            if router == "r2":
                maps = {
                    "users": map_r2_users, "profiles": map_r2_profiles,
                    "um_sess": map_r2_um_sess,
                    "hs_ipb": map_r2_hs_ipb, "hs_sess": map_r2_hs_sess,
                }
                api, auth = R2_API, R2_AUTH
            elif router == "r1":
                maps = {
                    "users": map_r1_users, "profiles": map_r1_profiles,
                    "um_sess": {},
                    "hs_ipb": map_r1_hs_ipb, "hs_sess": map_r1_hs_sess,
                }
                api, auth = R1_API, R1_AUTH
            else:
                maps = {
                    "users": map_r21_users, "profiles": map_r21_profiles,
                    "um_sess": {},
                    "hs_ipb": map_r21_hs_ipb, "hs_sess": map_r21_hs_sess,
                }
                api, auth = R2_1_API, R2_1_AUTH

            keys_to_clean = []
            if can_delete_others:
                keys_to_clean.extend(["um_sess", "hs_sess", "hs_ipb"])
            if can_delete_um:
                keys_to_clean.extend(["profile", "user"])

            for key in keys_to_clean:
                m = maps[key]
                eid = m.get(name)
                if eid:
                    path = f"/user-manager/session/{eid}" if key == "um_sess" else \
                           f"/ip/hotspot/active/{eid}" if key == "hs_sess" else \
                           f"/ip/hotspot/ip-binding/{eid}" if key == "hs_ipb" else \
                           f"/user-manager/user-profile/{eid}" if key == "profile" else \
                           f"/user-manager/user/{eid}"
                    ok = rest_delete(f"{api}{path}", auth)
                    if not ok:
                        # SSH fallback for delete
                        try:
                            fallback_cmd = None
                            if key == "user":
                                fallback_cmd = f'/user-manager/user/remove [find name="{name}"]'
                            elif key == "profile":
                                fallback_cmd = f'/user-manager/user-profile/remove [find user="{name}"]'
                            elif key == "hs_ipb":
                                fallback_cmd = f'/ip hotspot ip-binding remove [find comment="{name}"]'
                            elif key == "hs_sess":
                                fallback_cmd = f'/ip hotspot active remove [find user="{name}"]'
                            if fallback_cmd:
                                host_map = {"r2": ("10.88.89.2",2225,"chr","maruf123"), "r1": ("192.168.88.2",22,"admin","maruf123"), "r21": ("10.88.89.1",22,"chr","maruf123")}
                                h,p,u,pw = host_map[router]
                                ssh_run(h,p,u,pw,[fallback_cmd])
                                ok = True
                        except: pass
                    entry[f"{router}_{key}"] = ok
                    if key == "user" and ok:
                        done_users += 1
                    elif key == "user" and not ok:
                        errors += 1

        if can_delete_um:
            delete_grace_record(name, DB)

        log_entries.append(entry)

    print(f"\nR2+R1: {done_users} users deleted from UM, {errors} errors")

    # 7. R3 SSH cleanup (immediate for IP-bindings and active sessions)
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
            # Only remove if not in running grace
            if c not in running_users or (grace_records.get(c, {}).get("first_detected_running") and (now - parse_dt(grace_records[c]["first_detected_running"])) >= timedelta(hours=GRACE_RUNNING_HOURS)):
                r3_cmds.append(f'/ip hotspot ip-binding remove [find comment="{c}"]')
        for c in set(expired_codes) & set(r3_sessions.keys()):
            if c in running_users and (grace_records.get(c, {}).get("first_detected_running") and (now - parse_dt(grace_records[c]["first_detected_running"])) >= timedelta(hours=GRACE_RUNNING_HOURS)):
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
        print(f"\nLogged {len(log_entries)} operations to {LOG}")
    print("\nDone.")

if __name__ == "__main__":
    main()
