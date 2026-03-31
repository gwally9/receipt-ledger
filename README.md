# Receipt Ledger 🧾

A local Flask web app that uses Claude's vision API to scan receipt photos and
build a searchable expense ledger with monthly summaries and YTD totals.

---

## Configuration

Edit these constants at the top of `app.py`:

```python
CLAUDE_MODEL = "claude-3-5-haiku-20241022"  # haiku=speed, sonnet=balanced, opus=quality
DEFAULT_RECEIPT_DIR = "./receipts"  # change or set RECEIPT_DIR env var
```

Or set `ANTHROPIC_API_KEY` and `RECEIPT_DIR` via environment variables.

## Performance Optimizations

- **Smart model selection**: Uses Haiku by default (10x cheaper, faster than Opus)
- **Image optimization**: Large images (>5MB) are auto-resized before upload
- **Content-based dedup**: MD5 hashing skips already-scanned files
- **Database indexes**: Optimized queries on date, status, and hash columns
- **Rate limiting**: API endpoints protected against abuse (3 scans/min)
- **SSE streaming**: Real-time scan progress via `/api/scan/stream`

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set your Anthropic API key
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 3. Point the app at your receipt photo directory

**Option A — environment variable (recommended):**
```bash
export RECEIPT_DIR="/path/to/your/receipts"
```

**Option B — edit `app.py` directly:**
Change `DEFAULT_RECEIPT_DIR` near the top of `app.py`.

### 4. (Optional) Configure Claude model
```bash
# Fast/cheap (default):
export CLAUDE_MODEL="claude-3-5-haiku-20241022"

# High quality:
export CLAUDE_MODEL="claude-sonnet-4-20250514"
```

### 5. Run the app
```bash
python app.py
```

Then open http://localhost:5050 in your browser.

---

## How it works

1. On startup the app automatically scans all images in `RECEIPT_DIR`.
2. Each image is sent to Claude (vision) which extracts:
   - **Merchant** — store/restaurant/vendor name
   - **Date** — purchase date (normalized to YYYY-MM-DD)
   - **Amount** — total paid
   - **Currency** — defaults to USD
3. Results are stored in `receipts.db` (SQLite, created automatically).
4. The web UI shows:
   - **YTD total** for the current calendar year
   - **Monthly breakdown** with a visual bar chart
   - **Full receipt table** — sortable, filterable, with inline editing
   - **Photo viewer** — click the 🖼 button or ↗ to open the original image

---

## Supported image formats

`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`, `.heic`, `.heif`

---

## Tips

- **Scan New** — only processes files not yet in the database.
- **Rescan All** — re-processes every file (useful after corrections).
- **Inline editing** — click any merchant, date, or amount field to correct it.
- **Delete** — removes the DB record only; the original photo is untouched.
- Receipts with extraction errors show an `error` pill and can be rescanned after fixing the image.

---

## Files

```
receipt_scanner/
├── app.py          ← Flask app + Claude scanning logic
├── requirements.txt
├── receipts.db     ← SQLite database (auto-created)
├── README.md
└── templates/
    └── index.html  ← Web UI
```
