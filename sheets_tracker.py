"""
sheets_tracker.py — Logs every application to a Google Sheet for tracking.

Sheet columns:
A: Date | B: Company | C: Role | D: URL | E: Status | F: Keywords | G: Notes
"""
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import Config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = ["Date", "Company", "Role", "URL", "Status", "Keywords", "Notes"]


def _get_sheet():
    """Authenticate and return the first worksheet."""
    creds = Credentials.from_service_account_file(Config.GSHEETS_CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(Config.GSHEET_ID)
    ws = sh.sheet1

    # Auto-create headers if sheet is empty
    if not ws.row_values(1):
        ws.append_row(HEADERS)

    return ws


def log_application(
    company: str,
    role: str,
    url: str,
    status: str = "Applied",
    keywords: list[str] | None = None,
    notes: str = "",
) -> bool:
    """Append a row to the tracking sheet. Returns True on success."""
    try:
        ws = _get_sheet()
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            company,
            role,
            url,
            status,
            ", ".join(keywords or []),
            notes,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("Logged application: %s @ %s", role, company)
        return True
    except Exception as e:
        log.error("Failed to log to Google Sheets: %s", e)
        return False


def update_status(url: str, new_status: str) -> bool:
    """Find a row by URL and update its status."""
    try:
        ws = _get_sheet()
        cell = ws.find(url, in_column=4)
        if cell:
            ws.update_cell(cell.row, 5, new_status)
            log.info("Updated status for %s → %s", url, new_status)
            return True
        log.warning("URL not found in sheet: %s", url)
        return False
    except Exception as e:
        log.error("Failed to update status: %s", e)
        return False


def get_stats() -> dict:
    """Return application statistics."""
    try:
        ws = _get_sheet()
        records = ws.get_all_records()
        total = len(records)
        by_status = {}
        for r in records:
            s = r.get("Status", "Unknown")
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": total, "by_status": by_status}
    except Exception as e:
        log.error("Failed to get stats: %s", e)
        return {"total": 0, "by_status": {}}
