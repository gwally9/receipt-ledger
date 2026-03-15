# Receipt Ledger 🧾

A local Flask web app that uses Claude's vision API to scan receipt photos and
build a searchable expense ledger with monthly summaries and YTD totals.

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

### 4. Run the app
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
