import re
import sqlite3
import urllib.request
import urllib.parse
import sys
from datetime import datetime

BASE = "https://rvwifi.hostraj.com/public/l1/"
DB_PATH = "/root/sent_codes.db"
LOG_FILE = "/root/sync_lite.log"
LIMIT = 100

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch_page(limit, page):
    params = urllib.parse.urlencode({"q": "", "limit": limit, "page": page, "lite": 1})
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")

def parse_cards(html):
    cards = []
    parts = html.split('<div class=\\"card\\">')
    for part in parts[1:]:
        end_idx = len(part)
        for marker in ['<div class=\\"card\\">', '<div class=\\"actions\\"']:
            idx = part.find(marker)
            if idx != -1 and idx < end_idx:
                end_idx = idx
        for marker in ['</div></div><script>', '<div class="actions"']:
            idx = part.find(marker)
            if idx != -1 and idx < end_idx:
                end_idx = idx
        block = part[:end_idx]

        ts_m = re.search(r'<div class=\\"muted\\">([^<]+)</div>', block)
        sent_at = ts_m.group(1).strip() if ts_m else ""

        has_db = 1 if re.search(r'<span class=\\"pill\\">DB</span>\\s*<span class=\\"pill ok\\">', block) else 0
        has_json = 1 if re.search(r'<span class=\\"pill ok\\">JSON</span>', block) else 0

        btn_m = re.search(
            r'data-phone=\\"([^"]*)\\"'
            r'.*?data-code=\\"([^"]*)\\"'
            r'.*?data-invoice=\\"([^"]*)\\"'
            r'.*?data-transaction=\\"([^"]*)\\"'
            r'.*?data-name=\\"([^"]*)\\"'
            r'.*?data-room=\\"([^"]*)\\"'
            r'.*?data-amount=\\"([^"]*)\\"'
            r'.*?data-package=\\"([^"]*)\\"'
            r'.*?data-expiry=\\"([^"]*)\\"',
            block, re.DOTALL,
        )
        if not btn_m:
            continue

        cards.append({
            "phone": btn_m.group(1),
            "code": btn_m.group(2),
            "invoice_id": btn_m.group(3),
            "transaction_id": btn_m.group(4),
            "name": btn_m.group(5),
            "room_no": btn_m.group(6),
            "amount": btn_m.group(7),
            "package": btn_m.group(8),
            "expiry_date": btn_m.group(9),
            "sent_at": sent_at,
            "has_db": has_db,
            "has_json": has_json,
        })
    return cards

def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lite_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT,
            invoice_id TEXT,
            transaction_id TEXT,
            phone TEXT,
            code TEXT,
            name TEXT,
            room_no TEXT,
            amount TEXT,
            package TEXT,
            expiry_date TEXT,
            has_db INTEGER DEFAULT 0,
            has_json INTEGER DEFAULT 0,
            UNIQUE(invoice_id, code)
        )
    """)
    conn.commit()

def sync_cards(conn, cards):
    inserted = 0
    updated = 0
    for c in cards:
        cur = conn.execute(
            "SELECT id, has_db, has_json, expiry_date FROM lite_codes WHERE invoice_id=? AND code=?",
            (c["invoice_id"], c["code"])
        )
        existing = cur.fetchone()
        if existing:
            new_db, new_json, new_expiry = c["has_db"], c["has_json"], c["expiry_date"]
            if new_db != existing[1] or new_json != existing[2] or (new_expiry and new_expiry != existing[3]):
                conn.execute("UPDATE lite_codes SET has_db=?, has_json=?, expiry_date=? WHERE id=?",
                             (new_db, new_json, new_expiry, existing[0]))
                updated += 1
        else:
            conn.execute("""
                INSERT INTO lite_codes
                (sent_at, invoice_id, transaction_id, phone, code, name, room_no, amount, package, expiry_date, has_db, has_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (c["sent_at"], c["invoice_id"], c["transaction_id"], c["phone"],
                  c["code"], c["name"], c["room_no"], c["amount"], c["package"],
                  c["expiry_date"], c["has_db"], c["has_json"]))
            inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM lite_codes").fetchone()[0]
    return inserted, updated, total

def run_quick():
    log("Quick sync (latest 100)")
    html = fetch_page(LIMIT, 1)
    cards = parse_cards(html)
    log(f"Fetched {len(cards)} cards")
    if not cards:
        log("ERROR: No cards parsed")
        return
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    inserted, updated, total = sync_cards(conn, cards)
    conn.close()
    log(f"Inserted: {inserted}, Updated: {updated}, Total: {total}")
    log("Quick sync finished")

def run_full():
    log("Full sync (all pages)")
    PAGE_LIMIT = 200
    page = 1
    total_inserted = 0
    total_updated = 0
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    while True:
        html = fetch_page(PAGE_LIMIT, page)
        cards = parse_cards(html)
        if not cards:
            break
        inserted, updated, total = sync_cards(conn, cards)
        total_inserted += inserted
        total_updated += updated
        log(f"  Page {page}: {len(cards)} cards, inserted={inserted}, updated={updated}")
        if len(cards) < PAGE_LIMIT:
            break
        page += 1
    total = conn.execute("SELECT COUNT(*) FROM lite_codes").fetchone()[0]
    conn.close()
    log(f"Full sync done: {page} pages, {total_inserted} new, {total_updated} updated, {total} total")

if __name__ == "__main__":
    flag = sys.argv[1] if len(sys.argv) > 1 else "quick"
    if flag == "--full":
        run_full()
    else:
        run_quick()
