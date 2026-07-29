#!/usr/bin/env python3
import urllib.request
import json
import base64
import ssl
import sys
import os
from datetime import datetime, timedelta
import paramiko
import um_ledger

# Configurations
R1_URL = "http://192.168.88.2/rest" # CHR VM
R1_SSH_IP = "192.168.88.2"
R1_SSH_PORT = 22
R1_SSH_USER = "admin"
R1_SSH_PASS = "maruf123"
R1_AUTH = "admin:maruf123"

# List of all client routers running Hotspot services
HOTSPOT_ROUTERS = [
    {
        "name": "Router 2 (10.88.89.2)",
        "url": "http://10.88.89.2:8080/rest",
        "auth": "chr:maruf123",
        "ssh_ip": "10.88.89.2",
        "ssh_port": 2225,
        "ssh_user": "chr",
        "ssh_pass": "maruf123",
        "has_user_manager": True
    },
    {
        "name": "Router 3 (10.99.99.2)",
        "url": "http://10.99.99.2:8080/rest",
        "auth": "chr:maruf123",
        "ssh_ip": "10.99.99.2",
        "ssh_port": 22,
        "ssh_user": "chr",
        "ssh_pass": "maruf123",
        "has_user_manager": False
    }
]

CUTOFF_HOURS = 72
LOG_FILE = "/root/cleanup_log.json"

# Whitelist of usernames that should NEVER be deleted by the cleanup script
SKIP_USERS = ["masterum"]

def make_request(base_url, path, auth_str, method="GET", payload=None):
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method=method)
    auth_encoded = base64.b64encode(auth_str.encode()).decode()
    req.add_header("Authorization", f"Basic {auth_encoded}")
    
    if payload:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(payload).encode()
    else:
        data = None
        
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, data=data, timeout=15, context=ctx) as response:
            if response.status in [200, 201]:
                return json.loads(response.read().decode())
            elif response.status == 204:
                return True
    except Exception as e:
        pass
    return None

def run_ssh_batch(ip, port, username, password, cmds):
    if not cmds:
        return True
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, port=port, username=username, password=password, look_for_keys=False, allow_agent=False, timeout=10)
        full_command = "\n".join(cmds)
        stdin, stdout, stderr = ssh.exec_command(full_command)
        out = stdout.read().decode('utf-8', errors='ignore').strip()
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        if err:
            print(f"SSH batch on {ip} returned errors:\n{err}")
        return True
    except Exception as e:
        print(f"SSH Batch connection to {ip}:{port} failed: {e}")
        return False
    finally:
        ssh.close()

def get_expired_users(url, auth_str):
    print(f"Fetching user-profiles from {url}...")
    up_data = make_request(url, "/user-manager/user-profile", auth_str)
    if not up_data:
        print(f"Could not retrieve user-profiles from {url}")
        return {}
        
    expired_users = {}
    cutoff_time = datetime.now() - timedelta(hours=CUTOFF_HOURS)
    
    for item in up_data:
        if item.get("state") == "used":
            end_time_str = item.get("end-time")
            if end_time_str and end_time_str != "not-yet-running":
                try:
                    end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
                    if end_time < cutoff_time:
                        user = item.get("user")
                        
                        # Skip whitelisted users
                        if user and user.lower() in SKIP_USERS:
                            print(f"Skipping whitelisted user: {user}")
                            continue
                            
                        expired_users[user] = {
                            "id": item.get(".id"),
                            "end-time": end_time_str
                        }
                except Exception as e:
                    print(f"Failed to parse end-time {end_time_str}: {e}")
                    
    return expired_users

def delete_user(url, auth_str, router_name, username, user_id):
    res = make_request(url, f"/user-manager/user/{user_id}", auth_str, method="DELETE")
    if res:
        print(f"Deleted user {username} on {router_name} via API")
        return True
    return False

def main():
    # 1. Fetch expired users from CHR VM (R1)
    exp_r1 = get_expired_users(R1_URL, R1_AUTH)
    
    # 2. Fetch expired users from client routers running User Manager
    exp_clients = {}
    for r in HOTSPOT_ROUTERS:
        if r["has_user_manager"]:
            exp = get_expired_users(r["url"], r["auth"])
            exp_clients.update(exp)
            
    users_to_delete = set(exp_r1.keys()) | set(exp_clients.keys())
    print(f"Found {len(users_to_delete)} unique used users older than {CUTOFF_HOURS} hours to delete.")
    
    if not users_to_delete:
        print("No users to delete.")
        return
        
    # Get all users on R1 to map username -> .id
    users_r1_all = make_request(R1_URL, "/user-manager/user", R1_AUTH) or []
    map_users_r1 = {u["name"]: u[".id"] for u in users_r1_all if "name" in u and ".id" in u}
    
    # PRE-FETCH: Fetch all users, IP-bindings, and active sessions from client routers ONCE (O(1) lookups)
    map_users_clients = {}
    map_ip_bindings_clients = {}
    map_active_sessions_clients = {}
    
    for r in HOTSPOT_ROUTERS:
        # A. Pre-fetch User Manager users
        if r["has_user_manager"]:
            print(f"Pre-fetching User Manager users from {r['name']}...")
            users_all = make_request(r["url"], "/user-manager/user", r["auth"]) or []
            map_users_clients[r["name"]] = {u["name"]: u[".id"] for u in users_all if "name" in u and ".id" in u}
        else:
            map_users_clients[r["name"]] = {}
            
        # B. Pre-fetch Hotspot IP bindings
        print(f"Pre-fetching Hotspot IP bindings from {r['name']}...")
        ip_bindings = make_request(r["url"], "/ip/hotspot/ip-binding", r["auth"]) or []
        map_ip_bindings_clients[r["name"]] = {
            b["comment"].strip(): b[".id"] for b in ip_bindings 
            if "comment" in b and b["comment"] and ".id" in b
        }
        
        # C. Pre-fetch Hotspot active sessions
        print(f"Pre-fetching Hotspot active sessions from {r['name']}...")
        active_sessions = make_request(r["url"], "/ip/hotspot/active", r["auth"]) or []
        map_active_sessions_clients[r["name"]] = {s["user"]: s[".id"] for s in active_sessions if "user" in s and ".id" in s}
    
    ssh_cmds_r1 = []
    ssh_cmds_clients = {r["name"]: [] for r in HOTSPOT_ROUTERS}
    
    deleted_log = []
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 3. Perform deletions across all routers using pre-fetched maps
    for username in sorted(users_to_delete):
        # Double check whitelisting
        if username.lower() in SKIP_USERS:
            continue

        # LEDGER GUARD (system of truth): only delete if the trusted-source
        # record in um_ledger says the user is expired/used AND verify-valid.
        if not um_ledger.is_deletable(username, CUTOFF_HOURS):
            print(f"LEDGER GUARD: skip {username} (not expired per authoritative ledger)")
            continue

        log_entry = {
            "username": username,
            "deleted_at": timestamp_str,
            "r1_expired_end_time": exp_r1.get(username, {}).get("end-time", "unknown"),
            "deleted_from_r1": False,
            "routers_cleaned": []
        }
        
        # A. User Manager Deletion on Router 1 (CHR VM)
        if username in map_users_r1:
            u_id = map_users_r1[username]
            success = delete_user(R1_URL, R1_AUTH, "Router 1 (CHR)", username, u_id)
            if success:
                log_entry["deleted_from_r1"] = "API"
                um_ledger.mark_delete("r1", username)
            else:
                ssh_cmds_r1.append(f'/user-manager/user/remove [find name="{username}"]')
                log_entry["deleted_from_r1"] = "SSH Fallback"
                um_ledger.mark_delete("r1", username)
                
        # B. Hotspot / User Manager Deletion on all Client Routers
        for r in HOTSPOT_ROUTERS:
            cleaned_status = {
                "router_name": r["name"],
                "user_deleted": "n/a",
                "ip_binding_deleted": False,
                "hotspot_session_terminated": False
            }
            
            # If the router runs User Manager, delete the user account
            if r["has_user_manager"]:
                map_users = map_users_clients[r["name"]]
                if username in map_users:
                    u_id = map_users[username]
                    success = delete_user(r["url"], r["auth"], r["name"], username, u_id)
                    if success:
                        cleaned_status["user_deleted"] = "API"
                        um_ledger.mark_delete("r2", username)
                    else:
                        ssh_cmds_clients[r["name"]].append(f'/user-manager/user/remove [find name="{username}"]')
                        cleaned_status["user_deleted"] = "SSH Fallback"
                        um_ledger.mark_delete("r2", username)
                        
            # Hotspot IP Binding Deletion
            map_ip_bindings = map_ip_bindings_clients[r["name"]]
            if username in map_ip_bindings:
                b_id = map_ip_bindings[username]
                res = make_request(r["url"], f"/ip/hotspot/ip-binding/{b_id}", r["auth"], method="DELETE")
                if res:
                    print(f"Deleted IP Binding for {username} on {r['name']} via API")
                    cleaned_status["ip_binding_deleted"] = "API"
                else:
                    ssh_cmds_clients[r["name"]].append(f'/ip hotspot ip-binding remove [find comment="{username}"]')
                    cleaned_status["ip_binding_deleted"] = "SSH Fallback"
                    
            # Hotspot Active Session Terminate
            map_active_sessions = map_active_sessions_clients[r["name"]]
            if username in map_active_sessions:
                s_id = map_active_sessions[username]
                res = make_request(r["url"], f"/ip/hotspot/active/{s_id}", r["auth"], method="DELETE")
                if res:
                    print(f"Terminated active session for {username} on {r['name']} via API")
                    cleaned_status["hotspot_session_terminated"] = "API"
                else:
                    ssh_cmds_clients[r["name"]].append(f'/ip hotspot active remove [find user="{username}"]')
                    cleaned_status["hotspot_session_terminated"] = "SSH Fallback"
                    
            if cleaned_status["user_deleted"] != "n/a" or cleaned_status["ip_binding_deleted"] or cleaned_status["hotspot_session_terminated"]:
                log_entry["routers_cleaned"].append(cleaned_status)
                
        deleted_log.append(log_entry)
                
    # 4. Fallback to SSH for any failed API deletions
    if ssh_cmds_r1:
        print(f"Running {len(ssh_cmds_r1)} fallbacks on Router 1 over SSH...")
        run_ssh_batch(R1_SSH_IP, R1_SSH_PORT, R1_SSH_USER, R1_SSH_PASS, ssh_cmds_r1)
        
    for r in HOTSPOT_ROUTERS:
        cmds = ssh_cmds_clients[r["name"]]
        if cmds:
            print(f"Running {len(cmds)} fallbacks on {r['name']} over SSH...")
            run_ssh_batch(r["ssh_ip"], r["ssh_port"], r["ssh_user"], r["ssh_pass"], cmds)
            
    # 5. Append to JSON Log File
    existing_log = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                existing_log = json.load(f)
        except:
            existing_log = []
            
    existing_log.extend(deleted_log)
    
    try:
        with open(LOG_FILE, "w") as f:
            json.dump(existing_log, f, indent=2)
        print(f"Logged deletions in {LOG_FILE}")
    except Exception as e:
        print(f"Failed to write log file: {e}")
        
    print("Cleanup run complete.")

if __name__ == '__main__':
    main()
