# 🤖 Job Auto-Apply Bot (CLI)

An interactive terminal-based job application automation system. Paste a job URL → AI tailors your resume → Playwright fills the form → Google Sheets logs it.

## Architecture

```
You (Terminal CLI) ──→ bot.py
                        │
                        ├── scraper.py ──→ Playwright (headless) ──→ Extracts JD
                        │
                        ├── resume_tailor.py ──→ Qwen API ──→ Tailored resume
                        │
                        ├── auto_apply.py ──→ Playwright (visible) ──→ Fills & submits forms
                        │
                        └── sheets_tracker.py ──→ Google Sheets API ──→ Tracks everything
```

## Setup (15 minutes)

### 1. Clone & Install

```bash
cd job-auto-apply
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Create your `.env`

```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 3. Get Each Credential

**Qwen API:**
1. Sign up at [DashScope](https://dashscope.console.aliyun.com/)
2. Create an API key
3. Set `QWEN_API_KEY` in `.env`
4. (Or use any OpenAI-compatible endpoint — just change `QWEN_BASE_URL`)

**Google Sheets:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable Google Sheets API + Google Drive API
3. Create a Service Account → Download JSON key as `credentials.json`
4. Create a Google Sheet → Share it with the service account email
5. Copy the Sheet ID from the URL to `GOOGLE_SHEET_ID`

**LinkedIn (optional):**
- Put your email/password in `.env`
- First run: use the `login` command, solve any CAPTCHA in the browser window

### 4. Prepare Your Resume

1. **PDF resume**: Place your actual resume PDF at `./my_resume.pdf` (or set `RESUME_PATH` in `.env`)
2. **Base resume text**: Edit `./base_resume.txt` with a plain-text version of your resume — this is what the AI uses to tailor your resume for each job

### 5. Set Your Personal Info

You have two options:

**Option A — Edit `.env` directly:**
```env
YOUR_NAME=Your Full Name
YOUR_PHONE=+91XXXXXXXXXX
YOUR_EMAIL=your_email@example.com
YOUR_LOCATION=Bengaluru, India
```

**Option B — Use the interactive setup:**
```bash
python bot.py
# Then type: setup
```
The setup wizard will walk you through each field.

### 6. Run

```bash
python bot.py
```

## CLI Commands

| Command | What it does |
|---------|-------------|
| `apply <url>` | Full pipeline: scrape → tailor → apply → log |
| `fill <url>` | Fill form fields, leave browser open for your review |
| `tailor <url>` | Just get a tailored resume (no apply) |
| `cover <url>` | Generate a cover letter paragraph |
| `stats` | Show application statistics |
| `login` | Login to LinkedIn (do this first) |
| `setup` | Review / update personal info, resume path, etc. |
| `help` | Show all commands |
| `quit` | Exit the bot |

**Or just paste a URL** — the bot will ask what you want to do with it.

## How It Works

### LinkedIn Easy Apply
1. Bot opens the job page in Playwright
2. Clicks "Easy Apply"
3. Walks through each modal step, filling fields + uploading resume
4. Clicks Submit
5. Logs to Google Sheets

### Custom Job Portals
1. Bot opens the application page
2. Detects form fields by label/name/placeholder
3. Fills matching fields (name, email, phone, etc.)
4. Uploads resume to file inputs
5. **Does NOT auto-submit** — leaves browser open for you to review
6. Logs to Google Sheets

## Important Notes

- **Run on your own machine** — this uses YOUR browser, YOUR accounts
- **LinkedIn rate limiting** — don't spam 100 applies. Space them out.
- **CAPTCHA** — LinkedIn may show CAPTCHAs. The bot waits for you to solve them.
- **Headless mode** — Set `HEADLESS=true` in `.env` once you trust the flow. Keep it `false` (default) while testing so you can see what's happening.
- **Browser state persists** — login cookies are saved in `~/.job-bot-browser/`, so you don't need to re-login every time.

## Customization

**Add new job portals:** Edit `FIELD_MAP` in `auto_apply.py` to add field mappings specific to portals you use often.

**Change AI model:** Set `QWEN_BASE_URL` and `QWEN_MODEL` in `.env` to use OpenAI, Anthropic, or any OpenAI-compatible API.

**Add more personal fields:** Extend `Config.personal_info()` in `config.py` and `FIELD_MAP` in `auto_apply.py`.
