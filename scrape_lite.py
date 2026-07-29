import re
import sqlite3
import urllib.request
import urllib.parse

BASE = "https://rvwifi.hostraj.com/public/l1/"
DB_PATH = "/root/sent_codes.db"

def fetch_page(page=1, limit=1000):
    params = urllib.parse.urlencode({"q": "", "limit": limit, "page": page, "lite": 1})
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as r:
        return r.read().decode("utf-8")

def parse_cards(html):
    cards = []
    # Split on card boundaries (escaped quotes in response)
    parts = html.split('<div class=\\"card\\">')
    for part in parts[1:]:
        # Find where this card ends
        end_idx = len(part)
        for marker in ['<div class=\\"card\\">', '<div class=\\"actions\\"']:
            idx = part.find(marker)
            if idx != -1 and idx < end_idx:
                end_idx = idx
        # Fallback: also check for unescaped markers near end
        for marker in ['</div></div><script>', '<div class="actions"']:
            idx = part.find(marker)
            if idx != -1 and idx < end_idx:
                end_idx = idx
        block = part[:end_idx]

        # Timestamp from muted div
        ts_m = re.search(r'<div class=\\"muted\\">([^<]+)</div>', block)
        sent_at = ts_m.group(1).strip() if ts_m else ""

        # DB/JSON pills
        has_db = 1 if re.search(r'<span class=\\"pill\\">DB</span>\\s*<span class=\\"pill ok\\">', block) else 0
        has_json = 1 if re.search(r'<span class=\\"pill ok\\">JSON</span>', block) else 0

        # Resend button data attributes
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
            block,
            re.DOTALL,
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

def main():
    conn = sqlite3.connect(DB_PATH)
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
    conn.execute("DELETE FROM lite_codes")

    total = 0
    for page in [1, 2]:
        print(f"Fetching page {page}...")
        html = fetch_page(page=page)
        cards = parse_cards(html)
        print(f"  Found {len(cards)} cards")
        for c in cards:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO lite_codes
                    (sent_at, invoice_id, transaction_id, phone, code, name, room_no, amount, package, expiry_date, has_db, has_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    c["sent_at"], c["invoice_id"], c["transaction_id"], c["phone"],
                    c["code"], c["name"], c["room_no"], c["amount"], c["package"],
                    c["expiry_date"], c["has_db"], c["has_json"],
                ))
                total += 1
            except Exception as e:
                print(f"  Error inserting {c['invoice_id']}: {e}")
    conn.commit()
    conn.close()
    print(f"Done. Inserted {total} records into lite_codes.")

if __name__ == "__main__":
    main()
