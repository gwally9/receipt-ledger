"""
Receipt Scanner Web App
Uses Claude vision API to extract data from receipt images.
Set RECEIPT_DIR environment variable (or edit DEFAULT_RECEIPT_DIR) to point
to the folder containing your receipt photos.

Database (receipts.db) is stored inside RECEIPT_DIR alongside the photos so
it travels with the data. Images are fingerprinted with an MD5 hash — a file
is only sent to Claude once unless its content actually changes.
"""

import os
import sqlite3
import base64
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import anthropic
from flask import Flask, render_template, send_from_directory, jsonify, request, g

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_RECEIPT_DIR = os.path.expanduser("./receipts")   # change or set RECEIPT_DIR env var
RECEIPT_DIR = os.environ.get("RECEIPT_DIR", DEFAULT_RECEIPT_DIR)

# DB lives inside the receipt directory — it travels with the photos
os.makedirs(RECEIPT_DIR, exist_ok=True)
DB_PATH = os.path.join(RECEIPT_DIR, "receipts.db")

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
                file_hash   TEXT,          -- MD5 of image bytes; skip re-scan when unchanged
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
        # Non-destructive migration: add file_hash to any pre-existing database
        cols = {row[1] for row in db.execute("PRAGMA table_info(receipts)")}
        if "file_hash" not in cols:
            db.execute("ALTER TABLE receipts ADD COLUMN file_hash TEXT")
        db.commit()

# ── File hashing ──────────────────────────────────────────────────────────────
def hash_file(filepath: str) -> str:
    """Return the MD5 hex-digest of a file's contents."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

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
    Walk receipt_dir and scan images whose content has not been seen before.

    Skip logic (unless rescan=True):
      • Compute the MD5 hash of the image file.
      • If a row with that exact hash already exists and scan_status='ok',
        skip it — even if the filename changed.
      • Only call Claude when the hash is new (brand-new photo) or
        the file was modified since last scan (hash changed).

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
            current_hash = hash_file(str(img_path))

            # Look up by filename first (handles renames gracefully too)
            existing = db.execute(
                "SELECT id, file_hash, scan_status FROM receipts WHERE filename = ?",
                (filename,)
            ).fetchone()

            # Also check if this exact file content was already scanned
            # under a different filename (duplicate photo with new name)
            if not existing:
                existing = db.execute(
                    "SELECT id, file_hash, scan_status FROM receipts WHERE file_hash = ?",
                    (current_hash,)
                ).fetchone()

            if not rescan and existing:
                stored_hash = existing["file_hash"]
                status      = existing["scan_status"]
                # Skip only if the file content is unchanged and last scan succeeded
                if stored_hash == current_hash and status == "ok":
                    counts["skipped"] += 1
                    continue
                # If status=error and content unchanged, skip too (avoid hammering bad files)
                if stored_hash == current_hash and status == "error":
                    counts["skipped"] += 1
                    continue
                # Hash changed → file was replaced/updated → fall through to re-scan

            try:
                data = scan_image(str(img_path))
                now  = datetime.utcnow().isoformat()

                if existing:
                    db.execute("""
                        UPDATE receipts
                           SET filename=?, file_hash=?, merchant=?, date=?, date_raw=?,
                               amount=?, currency=?, notes=?, scan_status='ok', scanned_at=?
                         WHERE id=?
                    """, (filename, current_hash,
                          data.get("merchant"), data.get("date"), data.get("date_raw"),
                          data.get("amount"), data.get("currency", "USD"),
                          data.get("notes"), now, existing["id"]))
                else:
                    db.execute("""
                        INSERT INTO receipts
                            (filename, file_hash, merchant, date, date_raw,
                             amount, currency, notes, scan_status, scanned_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)
                    """, (filename, current_hash,
                          data.get("merchant"), data.get("date"), data.get("date_raw"),
                          data.get("amount"), data.get("currency", "USD"),
                          data.get("notes"), now))

                db.commit()
                counts["scanned"] += 1

            except Exception as e:
                now = datetime.utcnow().isoformat()
                err_msg = str(e)[:200]
                if existing:
                    db.execute("""
                        UPDATE receipts
                           SET filename=?, file_hash=?, scan_status='error',
                               notes=?, scanned_at=?
                         WHERE id=?
                    """, (filename, current_hash, err_msg, now, existing["id"]))
                else:
                    db.execute("""
                        INSERT INTO receipts (filename, file_hash, scan_status, notes, scanned_at)
                        VALUES (?, ?, 'error', ?, ?)
                    """, (filename, current_hash, err_msg, now))
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
