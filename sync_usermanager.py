import urllib.request
import json
import base64
import ssl
import sys
import os
import paramiko
from datetime import datetime
import um_ledger

R1_URL = "http://192.168.88.2:80/rest"
R1_SSH_IP = "192.168.88.2"
R1_SSH_PORT = 22
R1_SSH_USER = "admin"
R1_SSH_PASS = "maruf123"
R1_AUTH = "admin:maruf123"

R2_URL = "http://10.88.89.1:80/rest"
R2_SSH_IP = "10.88.89.1"
R2_SSH_PORT = 2225
R2_SSH_USER = "chr"
R2_SSH_PASS = "maruf123"
R2_AUTH = "chr:maruf123"

R3_URL = "http://10.88.89.2:80/rest"
R3_SSH_IP = "10.88.89.2"
R3_SSH_PORT = 2225
R3_SSH_USER = "chr"
R3_SSH_PASS = "maruf123"
R3_AUTH = "chr:maruf123"

STORE_FILE = "/root/sync_store.json"
LOCK_FILE = "/var/run/sync_usermanager.lock"
LOG_FILE = "/var/log/sync_usermanager.log"
MASTERUM_FILE = "/root/masterum.json"

try:
    import fcntl
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except:
    sys.exit(0)

def log_msg(msg):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except:
        pass

ROUTERS = {
    "r1": {"url": R1_URL, "auth": R1_AUTH, "ssh_ip": R1_SSH_IP, "ssh_port": R1_SSH_PORT, "ssh_user": R1_SSH_USER, "ssh_pass": R1_SSH_PASS},
    "r2": {"url": R2_URL, "auth": R2_AUTH, "ssh_ip": R2_SSH_IP, "ssh_port": R2_SSH_PORT, "ssh_user": R2_SSH_USER, "ssh_pass": R2_SSH_PASS},
    "r3": {"url": R3_URL, "auth": R3_AUTH, "ssh_ip": R3_SSH_IP, "ssh_port": R3_SSH_PORT, "ssh_user": R3_SSH_USER, "ssh_pass": R3_SSH_PASS},
}

def load_store():
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"last_run": None, "snapshots": {}, "synced": {}}
            data.setdefault("last_run", None)
            data.setdefault("snapshots", {})
            data.setdefault("synced", {})
            return data
        except:
            pass
    return {"last_run": None, "snapshots": {}, "synced": {}}

def save_store(store):
    store["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STORE_FILE, "w") as f:
        json.dump(store, f, indent=2)

def api_get(base_url, path, auth_str):
    url = f"{base_url}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(auth_str.encode()).decode())
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return json.loads(r.read().decode())
    except:
        return None

def ssh_cmd(ip, port, user, password, cmd):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, port=port, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=10)
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode('utf-8', errors='ignore').strip()
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        if err:
            log_msg(f"SSH STDERR: {err}")
            return None
        if out and (out.startswith("failure") or out.startswith("syntax error")):
            log_msg(f"SSH FAILED: {out}")
            return None
        return "OK"
    except Exception as e:
        log_msg(f"SSH EXCEPTION: {e}")
        return None
    finally:
        ssh.close()

def ssh_batch_cmd(ip, port, user, password, cmds):
    if not cmds:
        return 0, 0
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, port=port, username=user, password=password, look_for_keys=False, allow_agent=False, timeout=30)
        full_cmd = "\n".join(cmd.strip() for cmd in cmds)
        stdin, stdout, stderr = ssh.exec_command(full_cmd)
        out = stdout.read().decode('utf-8', errors='ignore').strip()
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        if err:
            log_msg(f"SSH BATCH STDERR: {err}")
        failures = 0
        if out:
            for line in out.split('\n'):
                if line.startswith("failure") or line.startswith("syntax error"):
                    failures += 1
        success = len(cmds) - failures
        return success, failures
    except Exception as e:
        log_msg(f"SSH BATCH EXCEPTION: {e}")
        return 0, len(cmds)
    finally:
        ssh.close()

def _fix_disabled(val):
    if val is False or val == "false":
        return "no"
    if val is True or val == "true":
        return "yes"
    return "no"

def ssh_add_user(target, username, password="maruf123", group="default", comment="", disabled="no"):
    r = ROUTERS[target]
    parts = [f'/user-manager user add name="{username}" password="{password}" group="{group}"']
    if comment:
        parts.append(f'comment="{comment}"')
    parts.append(f'disabled={_fix_disabled(disabled)}')
    cmd = ' '.join(parts)
    return ssh_cmd(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmd)

def ssh_add_profile(target, name, validity="unlimited", price="0", name_for_users="", starts_when="first-auth", override_shared="off"):
    r = ROUTERS[target]
    if not name_for_users:
        name_for_users = name
    cmd = f'/user-manager profile add name="{name}" validity="{validity}" price="{price}" name-for-users="{name_for_users}" starts-when="{starts_when}" override-shared-users="{override_shared}"'
    return ssh_cmd(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmd)

def ssh_add_limitation(target, name, download="0", upload="0", uptime="0", rate="0"):
    r = ROUTERS[target]
    cmd = f'/user-manager limitation add name="{name}" download-limit="{download}" upload-limit="{upload}" uptime-limit="{uptime}" rate-limit="{rate}"'
    return ssh_cmd(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmd)

def ssh_add_user_profile(target, user_name, profile_name, started=""):
    r = ROUTERS[target]
    cmd = f'/user-manager user-profile add user="{user_name}" profile="{profile_name}"'
    return ssh_cmd(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmd)

SSH_ADDERS = {
    "profiles": lambda target, item: ssh_add_profile(target, item.get("name",""), item.get("validity","unlimited"), item.get("price","0"), item.get("name-for-users",""), item.get("starts-when","first-auth"), item.get("override-shared-users","off")),
    "limitations": lambda target, item: ssh_add_limitation(target, item.get("name",""), item.get("download-limit","0"), item.get("upload-limit","0"), item.get("uptime-limit","0"), item.get("rate-limit","0")),
    "users": lambda target, item: ssh_add_user(target, item.get("name",""), item.get("password","maruf123"), item.get("group","default"), item.get("comment",""), _fix_disabled(item.get("disabled","no"))),
    "user_profiles": lambda target, item: ssh_add_user_profile(target, item.get("user",""), item.get("profile","")),
}

SSH_BUILDERS = {
    "profiles": lambda target, item: f'/user-manager profile add name="{item.get("name","")}" validity="{item.get("validity","unlimited")}" price="{item.get("price","0")}" name-for-users="{item.get("name-for-users",item.get("name",""))}" starts-when="{item.get("starts-when","first-auth")}" override-shared-users="{item.get("override-shared-users","off")}"',
    "limitations": lambda target, item: f'/user-manager limitation add name="{item.get("name","")}" download-limit="{item.get("download-limit","0")}" upload-limit="{item.get("upload-limit","0")}" uptime-limit="{item.get("uptime-limit","0")}" rate-limit="{item.get("rate-limit","0")}"',
    "users": lambda target, item: _build_user_cmd(item),
    "user_profiles": lambda target, item: f'/user-manager user-profile add user="{item.get("user","")}" profile="{item.get("profile","")}"',
}

def _build_user_cmd(item):
    parts = [f'/user-manager user add name="{item.get("name","")}" password="{item.get("password","maruf123")}" group="{item.get("group","default")}"']
    if item.get("comment"):
        parts.append(f'comment="{item["comment"]}"')
    parts.append(f'disabled={_fix_disabled(item.get("disabled","no"))}')
    return ' '.join(parts)

def _record_ledger(store_key, dst_name, item):
    if store_key == "users":
        um_ledger.mark_add(dst_name, item.get("name",""), item.get("group","default"), item.get("comment",""), _fix_disabled(item.get("disabled","no")))
    elif store_key == "profiles":
        um_ledger.mark_add_profile(dst_name, item.get("name",""), item.get("validity","unlimited"), item.get("price","0"), item.get("name-for-users",""), item.get("starts-when","first-auth"))
    elif store_key == "user_profiles":
        um_ledger.mark_add_user_profile(dst_name, item.get("user",""), item.get("profile",""))

def flush_batch(dst_name, r, batch, builder, store_key, synced, pair_key):
    cmds = [builder(r, item) for _, item, _ in batch]
    success, failures = ssh_batch_cmd(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmds)

    if success == 0 and failures == len(batch):
        log_msg(f"BATCH FAILED: {store_key} -> {dst_name}: {len(batch)} all failed (SSH/target issue)")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for key, item, sync_id in batch:
        synced[sync_id] = now
    if failures > 0:
        log_msg(f"BATCH {store_key} -> {dst_name}: {len(batch)} sent, {success} ok, {failures} fail")
    for key, item, sync_id in batch:
        log_msg(f"ADD OK: {store_key} '{key}' -> {dst_name}")
        _record_ledger(store_key, dst_name, item)
    return success

def sync_pair(name_a, name_b, path, store_key, unique_key):
    data_a = api_get(ROUTERS[name_a]["url"], path, ROUTERS[name_a]["auth"])
    data_b = api_get(ROUTERS[name_b]["url"], path, ROUTERS[name_b]["auth"])
    pair_key = f"{name_a}_{name_b}"

    if data_a is None:
        log_msg(f"SKIP {pair_key}: {name_a} API FAIL: {path}")
        return
    if data_b is None:
        log_msg(f"SKIP {pair_key}: {name_b} API FAIL: {path}")
        return

    store = load_store()
    store.setdefault("snapshots", {})
    store.setdefault("synced", {})
    store["snapshots"].setdefault(pair_key, {})
    store["synced"].setdefault(pair_key, {})

    store["snapshots"][pair_key][store_key] = {
        name_a: data_a,
        name_b: data_b,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    synced = store["synced"][pair_key].setdefault(store_key, {})
    added_count = 0

    map_a = {item[unique_key]: item for item in data_a if unique_key in item}
    map_b = {item[unique_key]: item for item in data_b if unique_key in item}

    def sync_dir(src_name, dst_name, src_map, dst_map):
        nonlocal added_count
        r = ROUTERS[dst_name]
        builder = SSH_BUILDERS.get(store_key)
        batch = []
        for key, item in src_map.items():
            if key not in dst_map:
                sync_id = f"{dst_name}_{key}"
                if synced.get(sync_id):
                    continue
                batch.append((key, item, sync_id))
                if len(batch) >= 10:
                    added_count += flush_batch(dst_name, r, batch, builder, store_key, synced, pair_key)
                    batch = []
        if batch:
            added_count += flush_batch(dst_name, r, batch, builder, store_key, synced, pair_key)

    sync_dir(name_a, name_b, map_a, map_b)
    sync_dir(name_b, name_a, map_b, map_a)

    save_store(store)
    log_msg(f"{pair_key} {path}: added={added_count} A={len(map_a)} B={len(map_b)}")

def sync_user_profiles_pair(name_a, name_b):
    data_a = api_get(ROUTERS[name_a]["url"], "/user-manager/user-profile", ROUTERS[name_a]["auth"])
    data_b = api_get(ROUTERS[name_b]["url"], "/user-manager/user-profile", ROUTERS[name_b]["auth"])
    pair_key = f"{name_a}_{name_b}"
    store_key = "user_profiles"

    if data_a is None or data_b is None:
        log_msg(f"SKIP {pair_key}: UP API failed")
        return

    store = load_store()
    store.setdefault("snapshots", {})
    store.setdefault("synced", {})
    store["snapshots"].setdefault(pair_key, {})
    store["synced"].setdefault(pair_key, {})
    store["snapshots"][pair_key][store_key] = {
        name_a: data_a,
        name_b: data_b,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    synced = store["synced"][pair_key].setdefault(store_key, {})
    added_count = 0

    def get_key(item):
        started = item.get("started", "waiting")
        started_clean = started.replace(" ", "_").replace(":", "-").replace("/", "-")
        return f"{item.get('user','')}_{item.get('profile','')}_{started_clean}"

    map_a = {get_key(item): item for item in data_a if 'user' in item and 'profile' in item}
    map_b = {get_key(item): item for item in data_b if 'user' in item and 'profile' in item}
    builder = SSH_BUILDERS[store_key]

    def sync_dir(src_name, dst_name, src_map, dst_map):
        nonlocal added_count
        r = ROUTERS[dst_name]
        batch = []
        for key, item in src_map.items():
            if key not in dst_map:
                sync_id = f"{dst_name}_{key}"
                if synced.get(sync_id):
                    continue
                batch.append((key, item, sync_id))
                if len(batch) >= 10:
                    added_count += flush_batch(dst_name, r, batch, builder, store_key, synced, pair_key)
                    batch = []
        if batch:
            added_count += flush_batch(dst_name, r, batch, builder, store_key, synced, pair_key)

    sync_dir(name_a, name_b, map_a, map_b)
    sync_dir(name_b, name_a, map_b, map_a)

    save_store(store)
    log_msg(f"{pair_key} UP: added={added_count} A={len(map_a)} B={len(map_b)}")

def generate_masterum_json():
    up_r1 = api_get(R1_URL, "/user-manager/user-profile", R1_AUTH)
    if not up_r1:
        return
    dedup = {}
    for item in up_r1:
        user = item.get("user")
        if not user:
            continue
        state = item.get("state", "waiting")
        if state == "waiting":
            continue
        profile = item.get("profile")
        end_time = item.get("end-time", "not-yet-running")
        started = item.get("started")
        current = dedup.get(user)
        if not current:
            dedup[user] = {"user": user, "profile": profile, "state": state, "end-time": end_time, "started": started}
        else:
            current_state = current.get("state", "waiting")
            overwrite = False
            if state == "running":
                overwrite = True
            elif state == "used" and current_state == "used":
                if end_time > current.get("end-time", ""):
                    overwrite = True
            if overwrite:
                dedup[user] = {"user": user, "profile": profile, "state": state, "end-time": end_time, "started": started}
    try:
        with open(MASTERUM_FILE, "w") as f:
            json.dump(list(dedup.values()), f, indent=2)
    except:
        pass

def sync_all():
    pairs = [("r1", "r2"), ("r1", "r3")]
    for a, b in pairs:
        sync_pair(a, b, "/user-manager/profile", "profiles", "name")
        sync_pair(a, b, "/user-manager/limitation", "limitations", "name")
        sync_pair(a, b, "/user-manager/user", "users", "name")
        sync_user_profiles_pair(a, b)
    generate_masterum_json()

if __name__ == '__main__':
    sync_all()
