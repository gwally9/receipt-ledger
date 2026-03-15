"""
Receipt Scanner Web App
Uses Claude vision API to extract data from receipt images.
Set RECEIPT_DIR environment variable (or edit DEFAULT_RECEIPT_DIR) to point
to the folder containing your receipt photos.
"""

import os
import sqlite3
import base64
import json
import threading
from datetime import datetime
from pathlib import Path

import anthropic
from flask import Flask, render_template, send_from_directory, jsonify, request, g

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_RECEIPT_DIR = os.path.expanduser("./receipts")   # change or set RECEIPT_DIR env var
RECEIPT_DIR = os.environ.get("RECEIPT_DIR", DEFAULT_RECEIPT_DIR)
DB_PATH = os.path.join(os.path.dirname(__file__), "receipts.db")
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}

app = Flask(__name__)
client = anthropic.Anthropic()           # reads ANTHROPIC_API_KEY from env

# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT UNIQUE NOT NULL,
                merchant    TEXT,
                date        TEXT,          -- ISO format YYYY-MM-DD when known
                date_raw    TEXT,          -- original string from receipt
                amount      REAL,
                currency    TEXT DEFAULT 'USD',
                notes       TEXT,
                scan_status TEXT DEFAULT 'ok',   -- ok | error | pending
                scanned_at  TEXT
            )
        """)
        db.commit()

# ── Claude vision scanning ─────────────────────────────────────────────────────
EXTRACTION_PROMPT = """You are a receipt parser. Examine this receipt image and extract:
1. merchant - The store/restaurant/vendor name (string)
2. date - The purchase date in YYYY-MM-DD format if possible; if only partial info leave best guess; if unreadable use null
3. date_raw - The date exactly as printed on the receipt (string or null)
4. amount - The final total amount paid as a number (float, no currency symbol); if unreadable use null
5. currency - 3-letter ISO currency code (e.g. USD, EUR); default to USD if not shown
6. notes - Any brief helpful note (e.g. "tip included", "partial receipt") or null

Respond ONLY with a JSON object with these exact keys. No markdown, no explanation.
Example: {"merchant":"Trader Joe's","date":"2024-03-15","date_raw":"03/15/24","amount":47.83,"currency":"USD","notes":null}"""

def scan_image(filepath: str) -> dict:
    """Send one image to Claude and return extracted fields."""
    ext = Path(filepath).suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".webp": "image/webp",
        ".gif": "image/gif",  ".heic": "image/jpeg", ".heif": "image/jpeg",
    }
    media_type = mime_map.get(ext, "image/jpeg")

    with open(filepath, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": EXTRACTION_PROMPT}
            ]
        }]
    )

    raw = message.content[0].text.strip()
    # Strip any accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

def scan_directory(receipt_dir: str, rescan: bool = False) -> dict:
    """
    Walk receipt_dir and scan any image not yet in the DB (unless rescan=True).
    Returns counts: {scanned, skipped, errors}
    """
    counts = {"scanned": 0, "skipped": 0, "errors": 0}
    receipt_path = Path(receipt_dir)
    if not receipt_path.exists():
        return counts

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        for img_path in sorted(receipt_path.iterdir()):
            if img_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            filename = img_path.name

            # Check if already scanned
            existing = db.execute(
                "SELECT id, scan_status FROM receipts WHERE filename = ?", (filename,)
            ).fetchone()

            if existing and not rescan:
                counts["skipped"] += 1
                continue
            if existing and existing["scan_status"] == "error" and not rescan:
                counts["skipped"] += 1
                continue

            try:
                data = scan_image(str(img_path))
                now = datetime.utcnow().isoformat()

                if existing:
                    db.execute("""
                        UPDATE receipts SET merchant=?, date=?, date_raw=?, amount=?,
                            currency=?, notes=?, scan_status='ok', scanned_at=?
                        WHERE filename=?
                    """, (data.get("merchant"), data.get("date"), data.get("date_raw"),
                          data.get("amount"), data.get("currency","USD"),
                          data.get("notes"), now, filename))
                else:
                    db.execute("""
                        INSERT INTO receipts (filename, merchant, date, date_raw, amount,
                            currency, notes, scan_status, scanned_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?)
                    """, (filename, data.get("merchant"), data.get("date"),
                          data.get("date_raw"), data.get("amount"),
                          data.get("currency","USD"), data.get("notes"), now))

                db.commit()
                counts["scanned"] += 1
            except Exception as e:
                now = datetime.utcnow().isoformat()
                if existing:
                    db.execute(
                        "UPDATE receipts SET scan_status='error', notes=?, scanned_at=? WHERE filename=?",
                        (str(e)[:200], now, filename)
                    )
                else:
                    db.execute("""
                        INSERT INTO receipts (filename, scan_status, notes, scanned_at)
                        VALUES (?, 'error', ?, ?)
                    """, (filename, str(e)[:200], now))
                db.commit()
                counts["errors"] += 1

    return counts

# ── Background initial scan ───────────────────────────────────────────────────
_scan_lock = threading.Lock()
_scan_status = {"running": False, "last_result": None}

def _bg_scan(rescan=False):
    with _scan_lock:
        _scan_status["running"] = True
        try:
            result = scan_directory(RECEIPT_DIR, rescan=rescan)
            _scan_status["last_result"] = result
        finally:
            _scan_status["running"] = False

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    db = get_db()

    # Monthly summary (only successful scans with amount and date)
    monthly = db.execute("""
        SELECT
            substr(date, 1, 7) AS month,
            COUNT(*)           AS count,
            SUM(amount)        AS total
        FROM receipts
        WHERE scan_status = 'ok'
          AND date IS NOT NULL
          AND amount IS NOT NULL
        GROUP BY month
        ORDER BY month DESC
    """).fetchall()

    # Year-to-date total
    current_year = datetime.utcnow().strftime("%Y")
    ytd_row = db.execute("""
        SELECT SUM(amount) AS ytd
        FROM receipts
        WHERE scan_status = 'ok'
          AND date LIKE ?
          AND amount IS NOT NULL
    """, (f"{current_year}%",)).fetchone()
    ytd = ytd_row["ytd"] or 0.0

    # All receipts
    receipts = db.execute("""
        SELECT * FROM receipts
        ORDER BY
            CASE WHEN date IS NULL THEN 1 ELSE 0 END,
            date DESC,
            filename DESC
    """).fetchall()

    return render_template("index-receipt.html",
                           monthly=monthly,
                           ytd=ytd,
                           receipts=receipts,
                           current_year=current_year,
                           scan_status=_scan_status,
                           receipt_dir=RECEIPT_DIR)

@app.route("/photos/<path:filename>")
def serve_photo(filename):
    """Serve original receipt images from the configured directory."""
    return send_from_directory(RECEIPT_DIR, filename)

@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a background scan. Pass ?rescan=1 to re-process all files."""
    if _scan_status["running"]:
        return jsonify({"status": "already_running"})
    rescan = request.args.get("rescan", "0") == "1"
    thread = threading.Thread(target=_bg_scan, args=(rescan,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "rescan": rescan})

@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(_scan_status)

@app.route("/api/receipt/<int:receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id):
    db = get_db()
    db.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    db.commit()
    return jsonify({"status": "deleted"})

@app.route("/api/receipt/<int:receipt_id>", methods=["PATCH"])
def update_receipt(receipt_id):
    """Allow manual correction of any field."""
    data = request.get_json()
    allowed = {"merchant", "date", "amount", "currency", "notes"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"status": "no_changes"})
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db = get_db()
    db.execute(f"UPDATE receipts SET {set_clause} WHERE id=?",
               (*updates.values(), receipt_id))
    db.commit()
    return jsonify({"status": "updated"})

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    # Kick off an initial scan in the background
    thread = threading.Thread(target=_bg_scan, daemon=True)
    thread.start()
    app.run(debug=True, port=5050, use_reloader=False)
