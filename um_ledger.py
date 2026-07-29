import urllib.request, json, base64, ssl, sqlite3
from datetime import datetime, timedelta

DB = "/root/um_ledger.db"
ctx = ssl._create_unverified_context()

# SOURCE OF TRUTH CONFIG: which router's expire/state wins on conflict
TRUST_EXPIRE = "r2"   # r1 or r2  -> checkpoint for trusting expire date
TRUST_STATE  = "r2"

ROUTERS = {
    "r1": {"url": "http://192.168.88.2/rest", "auth": "admin:maruf123"},
    "r2": {"url": "http://10.88.89.2:8080/rest", "auth": "chr:maruf123"},
}

def api_get(base_url, path, auth):
    req = urllib.request.Request(base_url + path)
    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS um_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, source TEXT, um_id TEXT,
        comment TEXT, disabled TEXT, grp TEXT, shared_users TEXT,
        recorded_at TEXT, op TEXT DEFAULT 'upsert'
    );
    CREATE TABLE IF NOT EXISTS um_user_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT, source TEXT, um_id TEXT, profile TEXT,
        state TEXT, end_time TEXT, started TEXT,
        recorded_at TEXT, op TEXT DEFAULT 'upsert'
    );
    CREATE TABLE IF NOT EXISTS um_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, source TEXT, validity TEXT, price TEXT,
        starts_when TEXT, name_for_users TEXT, recorded_at TEXT
    );
    CREATE TABLE IF NOT EXISTS um_trust (
        key TEXT PRIMARY KEY,
        trust_expire TEXT,
        trust_state TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS um_verify (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT, source TEXT, profile TEXT, state TEXT,
        end_time TEXT, now TEXT,
        expected_max_end TEXT, valid INTEGER,
        reason TEXT, checked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_users_name_src ON um_users(name, source);
    CREATE INDEX IF NOT EXISTS idx_up_user_src ON um_user_profiles(user, source);
    CREATE INDEX IF NOT EXISTS idx_prof_name_src ON um_profiles(name, source);
    CREATE INDEX IF NOT EXISTS idx_verify_invalid ON um_verify(valid);
    """)
    conn.commit()

def set_trust(conn):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO um_trust(key,trust_expire,trust_state,updated_at)
        VALUES('global',?,?,?)
        ON CONFLICT(key) DO UPDATE SET trust_expire=excluded.trust_expire,
            trust_state=excluded.trust_state, updated_at=excluded.updated_at""",
        (TRUST_EXPIRE, TRUST_STATE, now))
    conn.commit()

def parse_duration(s):
    if not s or s == "unlimited":
        return None
    total = timedelta()
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch in units:
            if num == "":
                num = "1"
            total += timedelta(seconds=int(num) * units[ch])
            num = ""
        else:
            num = ""
    return total if total else None

def parse_dt(s):
    if not s or s in ("not-yet-running", ""):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def record(conn, source, users, profiles, profile_defs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ru = [(u.get("name"), source, u.get(".id"), u.get("comment",""),
           u.get("disabled",""), u.get("group",""), u.get("shared-users",""), now, "upsert")
          for u in (users or []) if isinstance(u, dict) and "name" in u]
    rp = [(p.get("user"), source, p.get(".id"), p.get("profile",""),
           p.get("state",""), p.get("end-time",""), p.get("started") or "", now, "upsert")
          for p in (profiles or []) if isinstance(p, dict) and "user" in p]
    rpr = [(p.get("name"), source, p.get("validity",""), p.get("price",""),
            p.get("starts-when",""), p.get("name-for-users",""), now)
           for p in (profile_defs or []) if isinstance(p, dict) and "name" in p]
    conn.executemany("""INSERT INTO um_users(name,source,um_id,comment,disabled,grp,shared_users,recorded_at,op)
        VALUES (?,?,?,?,?,?,?,?,?)""", ru)
    conn.executemany("""INSERT INTO um_user_profiles(user,source,um_id,profile,state,end_time,started,recorded_at,op)
        VALUES (?,?,?,?,?,?,?,?,?)""", rp)
    conn.executemany("""INSERT INTO um_profiles(name,source,validity,price,starts_when,name_for_users,recorded_at)
        VALUES (?,?,?,?,?,?,?)""", rpr)
    conn.commit()

def record_delete(conn, source, username):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO um_users(name,source,recorded_at,op) VALUES(?,?,?, 'delete')",(username,source,now))
    conn.execute("INSERT INTO um_user_profiles(user,source,recorded_at,op) VALUES(?,?,?, 'delete')",(username,source,now))
    conn.commit()

def mark_delete(source, username):
    conn = sqlite3.connect(DB)
    record_delete(conn, source, username)
    conn.close()

def mark_add(source, username, group="default", comment="", disabled="no"):
    conn = sqlite3.connect(DB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO um_users(name,source,um_id,comment,disabled,grp,shared_users,recorded_at,op) VALUES (?,?,?,?,?,?,?,?,?)",
        (username, source, "", comment, disabled, group, "", now, "upsert")
    )
    conn.commit()
    conn.close()

def mark_add_profile(source, name, validity="unlimited", price="0", name_for_users="", starts_when="first-auth"):
    conn = sqlite3.connect(DB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO um_profiles(name,source,validity,price,starts_when,name_for_users,recorded_at) VALUES (?,?,?,?,?,?,?)",
        (name, source, validity, price, starts_when, name_for_users or name, now)
    )
    conn.commit()
    conn.close()

def mark_add_user_profile(source, user_name, profile_name, state="waiting"):
    conn = sqlite3.connect(DB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO um_user_profiles(user,source,um_id,profile,state,end_time,started,recorded_at,op) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_name, source, "", profile_name, state, "not-yet-running", "", now, "upsert")
    )
    conn.commit()
    conn.close()

def verify_expiry(conn, grace_hours=12):
    """Validate each user-profile record:
       - running-active/waiting -> end_time must be in the FUTURE
       - used -> end_time should be in the PAST
       - end_time must not exceed now + profile validity (+ grace buffer)
       valid=1 means logically consistent, valid=0 means mismatch.
       grace_hours: tolerance for clock/timezone skew on the upper bound.
    """
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    checked_at = now_str
    cur = conn.cursor()
    cur.execute("SELECT source,name,validity FROM um_profiles WHERE id IN (SELECT MAX(id) FROM um_profiles GROUP BY source,name)")
    prof = {(r[0], r[1]): parse_duration(r[2]) for r in cur.fetchall()}
    cur.execute("""SELECT user,source,profile,state,end_time FROM um_user_profiles
        WHERE op<>'delete' AND id IN (SELECT MAX(id) FROM um_user_profiles GROUP BY user, source)""")
    rows = cur.fetchall()
    out = []
    for user, source, profile, state, end_time in rows:
        end_dt = parse_dt(end_time)
        dur = prof.get((source, profile))
        valid = 1
        reason = "ok"
        if end_time == "unlimited" or (dur is None and (end_time == "" or end_dt is None)):
            # unlimited / no-validity profiles: no expiry to validate
            valid = 1
            reason = "unlimited/no-expiry"
        elif end_time == "not-yet-running" or state == "waiting":
            # waiting profile not activated yet -> no expiry to validate
            valid = 1
            reason = "waiting/not-yet-running"
        elif end_dt is None:
            valid = 0
            reason = "unparseable end-time"
        else:
            if state in ("running-active", "waiting"):
                if end_dt <= now:
                    valid = 0
                    reason = f"{state} but end-time in past ({end_time})"
            elif state == "used":
                if end_dt > now:
                    valid = 0
                    reason = f"used but end-time in future ({end_time})"
            # sanity: end_time should not exceed now + validity (+ grace buffer)
            if valid == 1 and dur is not None:
                max_end = now + dur + timedelta(hours=grace_hours)
                if end_dt > max_end:
                    valid = 0
                    reason = f"end-time exceeds profile validity {dur} + {grace_hours}h grace (max {max_end})"
        out.append((user, source, profile, state, end_time or "", now_str,
                    (now + dur).strftime("%Y-%m-%d %H:%M:%S") if dur else "", valid, reason, checked_at))
    cur.executemany("""INSERT INTO um_verify(user,source,profile,state,end_time,now,expected_max_end,valid,reason,checked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", out)
    conn.commit()
    bad = [r for r in out if r[7] == 0]
    return len(out), len(bad), bad

def current_view(conn):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT trust_expire, trust_state FROM um_trust WHERE key='global'")
    t = cur.fetchone()
    te, ts = (t["trust_expire"], t["trust_state"]) if t else ("r2","r2")
    cur.execute("""SELECT * FROM um_users WHERE id IN (
        SELECT MAX(id) FROM um_users GROUP BY name, source)""")
    users = {}
    for r in cur.fetchall():
        users.setdefault(r["name"], {})[r["source"]] = dict(r)
    cur.execute("""SELECT * FROM um_user_profiles WHERE id IN (
        SELECT MAX(id) FROM um_user_profiles GROUP BY user, source)""")
    profs = {}
    for r in cur.fetchall():
        profs.setdefault(r["user"], {})[r["source"]] = dict(r)
    return te, ts, users, profs

def build_authoritative_view(conn):
    """Create v_authoritative: one row per user, resolved to the trusted source,
    with expiry validation status. Rebuild each run so it reflects latest data."""
    conn.executescript("""
    DROP VIEW IF EXISTS v_authoritative;
    CREATE VIEW v_authoritative AS
    WITH latest_up AS (
        SELECT * FROM um_user_profiles
        WHERE op<>'delete' AND id IN (SELECT MAX(id) FROM um_user_profiles GROUP BY user, source)
    ),
    latest_verify AS (
        SELECT * FROM um_verify
        WHERE id IN (SELECT MAX(id) FROM um_verify GROUP BY user, source)
    ),
    trust AS (SELECT trust_expire, trust_state FROM um_trust WHERE key='global')
    SELECT
        COALESCE(a.user, b.user) AS user,
        (SELECT trust_expire FROM trust) AS trust_source,
        CASE WHEN (SELECT trust_expire FROM trust)='r1' THEN a.user ELSE b.user END AS authoritative_user,
        a.source AS r1_source, a.profile AS r1_profile, a.state AS r1_state, a.end_time AS r1_end_time,
        b.source AS r2_source, b.profile AS r2_profile, b.state AS r2_state, b.end_time AS r2_end_time,
        COALESCE(v.valid, 1) AS authoritative_valid,
        v.reason AS verify_reason
    FROM latest_up a
    LEFT JOIN latest_up b ON a.user=b.user AND b.source='r2'
    LEFT JOIN latest_verify v ON v.user=COALESCE(a.user,b.user) AND v.source=(SELECT trust_expire FROM trust)
    WHERE a.source='r1'
    UNION ALL
    SELECT
        b.user AS user,
        (SELECT trust_expire FROM trust) AS trust_source,
        b.user AS authoritative_user,
        a.source AS r1_source, a.profile AS r1_profile, a.state AS r1_state, a.end_time AS r1_end_time,
        b.source AS r2_source, b.profile AS r2_profile, b.state AS r2_state, b.end_time AS r2_end_time,
        COALESCE(v.valid, 1) AS authoritative_valid,
        v.reason AS verify_reason
    FROM latest_up b
    LEFT JOIN latest_up a ON b.user=a.user AND a.source='r1'
    LEFT JOIN latest_verify v ON v.user=b.user AND v.source=(SELECT trust_expire FROM trust)
    WHERE b.source='r2' AND a.user IS NULL;
    """)
    conn.commit()

def authoritative_resolve(conn, username):
    """Return the trusted-source record for a user, or None."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT trust_expire FROM um_trust WHERE key='global'")
    t = cur.fetchone()
    te = t["trust_expire"] if t else "r2"
    cur.execute("""SELECT * FROM um_user_profiles
        WHERE user=? AND source=? AND op<>'delete'
        AND id IN (SELECT MAX(id) FROM um_user_profiles WHERE user=? AND source=?)
        LIMIT 1""", (username, te, username, te))
    r = cur.fetchone()
    return dict(r) if r else None

def is_deletable(username, cutoff_hours=72):
    """Consult the ledger (system of truth). Return True only if the authoritative
    record is state='used' AND end_time older than cutoff_hours AND verify valid.
    Otherwise the user must NOT be deleted."""
    from datetime import datetime as _dt
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rec = authoritative_resolve(conn, username)
    if rec is None:
        conn.close()
        return False  # not in trusted source -> do not delete
    # verify status from trusted source
    cur = conn.cursor()
    cur.execute("SELECT valid FROM um_verify WHERE user=? AND source=? AND id IN (SELECT MAX(id) FROM um_verify WHERE user=? AND source=?)", (username, rec["source"], username, rec["source"]))
    vr = cur.fetchone()
    valid = vr["valid"] if vr else 1
    conn.close()
    if valid != 1:
        return False  # ledger says record is inconsistent -> never auto-delete
    if rec["state"] != "used":
        return False  # still active/waiting on trusted source -> never delete
    end = parse_dt(rec["end_time"])
    if end is None or end >= (_dt.now() - timedelta(hours=cutoff_hours)):
        return False
    return True

def main():
    conn = sqlite3.connect(DB)
    init_db(conn); set_trust(conn)
    for source, cfg in ROUTERS.items():
        users = api_get(cfg["url"], "/user-manager/user", cfg["auth"])
        profiles = api_get(cfg["url"], "/user-manager/user-profile", cfg["auth"])
        pdefs = api_get(cfg["url"], "/user-manager/profile", cfg["auth"])
        nu = len([x for x in (users or []) if isinstance(x, dict) and "name" in x])
        np_ = len([x for x in (profiles or []) if isinstance(x, dict) and "user" in x])
        record(conn, source, users, profiles, pdefs)
        print(f"{source}: recorded {nu} users, {np_} profiles, {len([x for x in (pdefs or []) if isinstance(x,dict) and 'name' in x])} profile-defs")
    total, bad, badrows = verify_expiry(conn, grace_hours=12)
    print(f"VERIFY: {total} user-profiles checked, {bad} INVALID expiry")
    for b in badrows[:25]:
        print("  INVALID:", b)
    build_authoritative_view(conn)
    print("AUTHORITATIVE VIEW built (v_authoritative)")
    te, ts, users, profs = current_view(conn)
    print(f"TRUST CHECKPOINT: expire->{te}  state->{ts}")
    conn.close()

if __name__ == "__main__":
    main()
