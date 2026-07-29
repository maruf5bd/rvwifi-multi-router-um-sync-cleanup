#!/usr/bin/env python3
"""Single orchestrator: refresh User Manager data into the ledger (system of truth),
validate expiries, then run the ledger-guarded cleanup. Run from cron."""
import sys, subprocess, datetime

ROOT = "/root"
sys.path.insert(0, ROOT)

STEPS = [
    ("sync_usermanager.py",  "sync users R1<->R2"),
    ("um_ledger.py",         "rebuild ledger + verify expiry (SoT)"),
    ("cleanup_used_users.py","ledger-guarded cleanup"),
]

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open("/var/log/um_orchestrator.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def main():
    log("=== um_orchestrator START ===")
    for script, desc in STEPS:
        log(f"STEP: {desc} ({script})")
        try:
            r = subprocess.run([sys.executable, f"{ROOT}/{script}"],
                               capture_output=True, text=True, timeout=600)
            out = (r.stdout or "") + (r.stderr or "")
            for ln in out.strip().splitlines()[-15:]:
                log(f"  | {ln}")
            if r.returncode != 0:
                log(f"  !! {script} exited {r.returncode}")
        except Exception as e:
            log(f"  !! {script} ERROR: {e}")
    log("=== um_orchestrator DONE ===")

if __name__ == "__main__":
    main()
