# PixelMatcher

Visual regression testing for AEM (and any) XML sitemaps.
Opens every page in headless Chromium, captures scroll-strip screenshots, diffs them against a saved baseline, and generates a self-contained HTML report with red-highlighted changed pixels.

---

## Table of Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation — macOS](#installation--macos)
4. [Installation — Windows](#installation--windows)
5. [Starting the virtual environment every time](#starting-the-virtual-environment-every-time)
6. [Quick start](#quick-start)
7. [CLI options](#cli-options)
8. [Typical workflows](#typical-workflows)
9. [Storage layout](#storage-layout)
10. [Reading the report](#reading-the-report)
11. [Troubleshooting](#troubleshooting)

---

## How it works

1. Fetches and parses the sitemap XML (supports sitemap index files).
2. Opens each URL in a headless Chromium browser at 1440 × 900 px.
3. Scrolls down the page strip by strip (900 px each), waiting 0.3 s per strip so lazy-loaded images and footers have time to appear.
4. Saves each strip as a JPEG under `pixelmatcher/<site>/baseline/<page>/`.
5. On the second run, captures again and compares pixel-by-pixel against the saved baseline.
6. Writes a dark-theme HTML report — failed pages first, with side-by-side Baseline / Current / Diff images embedded directly in the file.

---

## Requirements

- Python **3.10 or newer**
- Internet access to reach the target website
- ~200 MB disk space for Chromium (downloaded once by Playwright)

---

## Installation — macOS

> **Do this once.** After the initial setup you only need to activate the virtual environment each time (see [Starting the virtual environment every time](#starting-the-virtual-environment-every-time)).

Open the **Terminal** app and run the steps below one by one.

---

### Step 1 — Check your Python version

```bash
python3 --version
```

You need **3.10 or higher**. Example of a good response: `Python 3.12.2`

If your version is older, install a newer Python:

```bash
# Option A — download the installer from python.org (recommended for beginners)
# Visit https://www.python.org/downloads/ and run the .pkg installer

# Option B — install via Homebrew
brew install python@3.12
```

---

### Step 2 — Navigate to the project folder

```bash
cd ~/Documents/PixelMatcher
```

Replace the path with wherever you placed `pixelmatcher.py` and `requirements.txt`.

---

### Step 3 — Create the virtual environment

> **This is a one-time step.** It creates a `.venv` folder inside your project that holds all the Python packages.

```bash
python3 -m venv .venv
```

---

### Step 4 — Activate the virtual environment

```bash
source .venv/bin/activate
```

Your prompt will change to show `(.venv)` at the start, like this:

```
(.venv) yourname@Mac PixelMatcher %
```

**You must do this every time you open a new Terminal window before running PixelMatcher.**

---

### Step 5 — Install Python packages

> **One-time step.** Only needed after creating the virtual environment for the first time.

```bash
pip install -r requirements.txt
```

Wait for it to finish. You will see several packages being downloaded and installed.

---

### Step 6 — Install the Chromium browser

> **One-time step.** Downloads the headless browser (~150 MB).

```bash
python3 -m playwright install chromium
```

---

### Step 7 — Verify everything works

```bash
python3 pixelmatcher.py --help
```

You should see a list of options printed in the terminal. Setup is complete.

---

## Installation — Windows

> **Do this once.** After the initial setup you only need to activate the virtual environment each time (see [Starting the virtual environment every time](#starting-the-virtual-environment-every-time)).

Open **Command Prompt** (`cmd`) or **PowerShell** and run the steps below one by one.

---

### Step 1 — Install Python

Download the installer from [python.org/downloads](https://www.python.org/downloads/windows/) and run it.

> **Important:** On the first screen of the installer, tick **"Add Python to PATH"** before clicking Install Now. Without this, the `python` command will not be found.

After installation, open a new terminal and verify:

```cmd
python --version
```

You need **3.10 or higher**. Example of a good response: `Python 3.12.2`

---

### Step 2 — Navigate to the project folder

```cmd
cd C:\PixelMatcher
```

Replace the path with wherever you placed `pixelmatcher.py` and `requirements.txt`.

Tip: In File Explorer you can Shift + Right-click the folder and choose **"Open in Terminal"** or **"Open PowerShell window here"**.

---

### Step 3 — Create the virtual environment

> **One-time step.** Creates a `.venv` folder inside your project.

**Command Prompt:**
```cmd
python -m venv .venv
```

**PowerShell:**
```powershell
python -m venv .venv
```

---

### Step 4 — Activate the virtual environment

**Command Prompt:**
```cmd
.venv\Scripts\activate
```

**PowerShell:**
```powershell
.venv\Scripts\Activate.ps1
```

> If PowerShell shows an error about running scripts being disabled, run this **once** to allow it, then try the activate command again:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

Your prompt will change to show `(.venv)` at the start, like this:

```
(.venv) C:\PixelMatcher>
```

**You must do this every time you open a new terminal window before running PixelMatcher.**

---

### Step 5 — Install Python packages

> **One-time step.** Only needed after creating the virtual environment for the first time.

```cmd
pip install -r requirements.txt
```

Wait for it to finish. You will see several packages being downloaded and installed.

---

### Step 6 — Install the Chromium browser

> **One-time step.** Downloads the headless browser (~150 MB).

```cmd
python -m playwright install chromium
```

---

### Step 7 — Verify everything works

```cmd
python pixelmatcher.py --help
```

You should see a list of options printed in the terminal. Setup is complete.

---

## Starting the virtual environment every time

The virtual environment **must be activated every time you open a new terminal** before running PixelMatcher. The Python packages and Chromium you installed in the setup steps are only available when the environment is active.

You will know it is active when you see `(.venv)` at the start of your terminal prompt.

### macOS — activate

Open Terminal, navigate to your project folder, then run:

```bash
cd ~/Documents/PixelMatcher
source .venv/bin/activate
```

Your prompt becomes:
```
(.venv) yourname@Mac PixelMatcher %
```

### Windows — activate (Command Prompt)

Open Command Prompt, navigate to your project folder, then run:

```cmd
cd C:\PixelMatcher
.venv\Scripts\activate
```

Your prompt becomes:
```
(.venv) C:\PixelMatcher>
```

### Windows — activate (PowerShell)

Open PowerShell, navigate to your project folder, then run:

```powershell
cd C:\PixelMatcher
.venv\Scripts\Activate.ps1
```

Your prompt becomes:
```
(.venv) PS C:\PixelMatcher>
```

### How to deactivate

When you are done and want to leave the virtual environment:

```bash
deactivate
```

The `(.venv)` prefix will disappear from your prompt.

---

## Quick start

> Make sure the virtual environment is **activated** (you see `(.venv)`) before running any command below.

### Step 1 — Capture a baseline

Run the tool against your sitemap for the first time. It will open every page, take screenshots, and save them as the baseline. **No report is generated on this first run.**

**macOS:**
```bash
python3 pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

You will see output like:
```
[PixelMatcher] Fetching sitemap... 42 URLs found.
[PixelMatcher] Mode: BASELINE (no baseline yet)
[PixelMatcher] Screenshotting 42 pages with 5 workers...
[PixelMatcher] Progress: 10/42...
[PixelMatcher] Progress: 20/42...
[PixelMatcher] Progress: 42/42 done.
[PixelMatcher] Baseline captured — 42 pages, 187 strips saved.
[PixelMatcher] Run again to compare.
```

### Step 2 — Run a comparison

Run the same command again after you have made changes to the site (or just to verify nothing changed):

**macOS:**
```bash
python3 pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

You will see output like:
```
[PixelMatcher] Fetching sitemap... 42 URLs found.
[PixelMatcher] Mode: COMPARE (baseline exists)
[PixelMatcher] Screenshotting 42 pages with 5 workers...
[PixelMatcher] Progress: 42/42 done.
[PixelMatcher] Diffing pages...
[PixelMatcher] ✓ 40 passed  ✗ 2 failed  ⚠ 0 errors  ★ 1 new
[PixelMatcher] Report → pixelmatcher/example_com/reports/report_2026-03-31_10-32-18.html
```

Open the `.html` file in any browser to see the full report.

### Step 3 — Reset the baseline

If you want to start fresh (e.g. after a redesign), delete the old baseline and recapture:

**macOS:**
```bash
python3 pixelmatcher.py --sitemap https://www.example.com/sitemap.xml --reset
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml --reset
```

---

## CLI options

**macOS:**
```
python3 pixelmatcher.py --sitemap <url> [options]
```

**Windows:**
```
python pixelmatcher.py --sitemap <url> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--sitemap` | *(required)* | Full URL to the `sitemap.xml` file |
| `--workers` | `5` | Number of pages captured in parallel. Raise to `10` for faster runs; lower to `1` if you hit memory issues. |
| `--threshold` | `0.02` | Maximum fraction of pixels allowed to differ before a strip is marked **FAIL** (e.g. `0.02` = 2 %). |
| `--reset` | off | Delete the saved baseline for this site and capture a fresh one. |
| `--viewport-width` | `1440` | Browser viewport width in CSS pixels. |
| `--strip-height` | `900` | Height of each scroll strip in CSS pixels. |
| `--max-capture-width` | `1440` | Strips are resized to at most this width before saving. `1440` means no downscale. |
| `--jpeg-quality` | `85` | JPEG quality for saved strips (1–100). Higher = sharper, larger files. |
| `--device-scale-factor` | `1.0` | Set to `2` for retina/HiDPI captures (2× resolution, ~4× file size). |
| `--diff-pixel-threshold` | `15` | Per-channel RGB difference above which a pixel counts as changed. Raise (e.g. `25`) to ignore minor JPEG artefacts. |
| `--diff-blur` | `0` | Gaussian blur radius applied to both images before comparing (e.g. `0.5` reduces JPEG ringing noise). |
| `--report-thumb-width` | `800` | Width of images embedded in the HTML report. |
| `--report-jpeg-quality` | `80` | JPEG quality for images embedded in the report. |

---

## Typical workflows

### Nightly regression check

**macOS:**
```bash
python3 pixelmatcher.py \
  --sitemap https://www.example.com/sitemap.xml \
  --workers 10 \
  --threshold 0.01
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml --workers 10 --threshold 0.01
```

### After a code deployment

**macOS:**
```bash
python3 pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml
```

### High-resolution capture for design review

**macOS:**
```bash
python3 pixelmatcher.py \
  --sitemap https://www.example.com/sitemap.xml \
  --device-scale-factor 2 \
  --jpeg-quality 92 \
  --reset
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml --device-scale-factor 2 --jpeg-quality 92 --reset
```

### Ignore minor JPEG / animation noise

**macOS:**
```bash
python3 pixelmatcher.py \
  --sitemap https://www.example.com/sitemap.xml \
  --threshold 0.05 \
  --diff-pixel-threshold 25 \
  --diff-blur 0.5
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap https://www.example.com/sitemap.xml --threshold 0.05 --diff-pixel-threshold 25 --diff-blur 0.5
```

### Stop early (Ctrl + C)

Press **Ctrl + C** at any time. The tool finishes the page currently being captured, then writes a partial HTML report marked **(aborted)**. You can review what was captured so far.

---

## Storage layout

All files are stored inside a `pixelmatcher/` folder created in whichever directory you run the command from.

```
pixelmatcher/
└── example_com/                      ← one folder per sitemap host
    ├── baseline/                     ← reference screenshots (do not edit manually)
    │   ├── homepage_a1b2c3d4e5/
    │   │   ├── strip_000.jpg         ← top 900 px of the page
    │   │   ├── strip_001.jpg         ← next 900 px
    │   │   └── strip_002.jpg         ← bottom of the page (includes footer)
    │   └── about_us_f6g7h8i9j0/
    │       └── strip_000.jpg
    ├── runs/
    │   └── run_2026-03-31_10-30-00/
    │       └── screenshots/          ← current-run captures (temporary)
    │           └── homepage_a1b2c3d4e5/
    │               └── strip_000.jpg
    └── reports/
        └── report_2026-03-31_10-32-18.html   ← open this in a browser
```

> **Tip:** The `pixelmatcher/baseline/` folder can be committed to version control to share reference screenshots with your team. Add `pixelmatcher/*/runs/` to `.gitignore` to exclude temporary run data.

---

## Reading the report

Open the `.html` file in Chrome, Firefox, Edge, or Safari — no server required, no internet needed.

| Card colour | Meaning |
|-------------|---------|
| 🔴 Red border | **FAIL** — one or more strips exceeded the diff threshold |
| 🟢 Green border | **PASS** — all strips within threshold |
| 🔵 Blue border | **NEW** — page is in the sitemap but had no baseline; screenshots saved as the new baseline automatically |
| ⚫ Grey border | **ERROR** — page timed out or could not be loaded |

**FAIL cards** show each failed strip as a three-image row: **Baseline · Current · Diff** (changed pixels highlighted in red). Passed strips are collapsed under a toggle so they are out of the way.

**PASS cards** show a single thumbnail collapsed by default.

All images are embedded directly in the HTML file — there are no separate image files or folders needed to view the report.

---

## Troubleshooting

### "Chromium is not installed for this Python environment"

Your virtual environment is not activated, or Playwright was installed outside it. Fix:

**macOS:**
```bash
source .venv/bin/activate
python3 -m playwright install chromium
```

**Windows (cmd):**
```cmd
.venv\Scripts\activate
python -m playwright install chromium
```

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
python -m playwright install chromium
```

---

### "python is not recognized" or "python3: command not found"

- **macOS:** Use `python3` instead of `python`. If that also fails, re-install Python from [python.org](https://www.python.org/downloads/).
- **Windows:** Python was not added to PATH during installation. Re-run the Python installer, choose **Modify**, and tick **"Add Python to environment variables"**.

---

### pip install fails with "no module named pip"

Re-create the virtual environment:

**macOS:**
```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (cmd):**
```cmd
rmdir /s /q .venv
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Report HTML file is very large

Reduce the size of embedded images:

**macOS:**
```bash
python3 pixelmatcher.py --sitemap <url> --report-thumb-width 600 --report-jpeg-quality 70
```

**Windows:**
```cmd
python pixelmatcher.py --sitemap <url> --report-thumb-width 600 --report-jpeg-quality 70
```

---