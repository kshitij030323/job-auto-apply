"""
config.py — Central configuration loaded from .env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:

    # Qwen (OpenAI-compatible endpoint)
    QWEN_API_KEY = os.getenv("QWEN_API_KEY")
    QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")

    # Google Sheets
    GSHEETS_CREDS = os.getenv("GOOGLE_SHEETS_CREDS_FILE", "credentials.json")
    GSHEET_ID = os.getenv("GOOGLE_SHEET_ID")

    # LinkedIn
    LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL")
    LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD")

    # Personal details for form filling
    YOUR_NAME = os.getenv("YOUR_NAME", "")
    YOUR_PHONE = os.getenv("YOUR_PHONE", "")
    YOUR_EMAIL = os.getenv("YOUR_EMAIL", "")
    YOUR_LOCATION = os.getenv("YOUR_LOCATION", "")
    RESUME_PATH = Path(os.getenv("RESUME_PATH", "./my_resume.pdf"))
    BASE_RESUME_TEXT = Path(os.getenv("BASE_RESUME_TEXT_PATH", "./base_resume.txt"))

    # Playwright
    HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
    SLOW_MO = int(os.getenv("SLOW_MO", "800"))  # ms between actions

    @classmethod
    def personal_info(cls) -> dict:
        """Returns a dict used by auto-apply to fill common form fields."""
        return {
            "name": cls.YOUR_NAME,
            "full_name": cls.YOUR_NAME,
            "first_name": cls.YOUR_NAME.split()[0] if cls.YOUR_NAME else "",
            "last_name": cls.YOUR_NAME.split()[-1] if cls.YOUR_NAME else "",
            "email": cls.YOUR_EMAIL,
            "phone": cls.YOUR_PHONE,
            "location": cls.YOUR_LOCATION,
            "city": cls.YOUR_LOCATION.split(",")[0].strip() if cls.YOUR_LOCATION else "",
        }
