"""
auto_apply.py — AI-powered auto-apply engine.

Uses an LLM (Qwen 3.5 via NVIDIA NIM) to understand web pages and
intelligently fill job application forms — no brittle CSS selectors.

Two modes:
1. LinkedIn Easy Apply — AI navigates the modal step by step
2. Generic portal — AI fills the form, leaves it for user review

IMPORTANT: This runs YOUR browser on YOUR machine. It's your account, your actions.
"""
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

from config import Config
from ai_agent import run_agent

log = logging.getLogger(__name__)


def _load_resume_text() -> str:
    """Load the plain-text resume for AI context."""
    try:
        return Config.BASE_RESUME_TEXT.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


class AutoApplier:
    """Manages a persistent browser session for applying to jobs."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context: BrowserContext | None = None
        self.info = Config.personal_info()

    async def start(self):
        """Launch browser with persistent state (saves login cookies)."""
        data_dir = str(Path.home() / ".job-bot-browser")
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self._launch_persistent(data_dir)
        except Exception as first_err:
            if "Opening in existing browser session" not in str(first_err):
                raise
            log.warning("Stale browser lock detected — cleaning up and retrying...")
            _kill_stale_chromium(data_dir)
            lock_file = Path(data_dir) / "SingletonLock"
            lock_file.unlink(missing_ok=True)
            await asyncio.sleep(1)
            self.browser = await self._launch_persistent(data_dir)
        self.context = self.browser
        log.info("Browser started (headless=%s)", Config.HEADLESS)

    async def _launch_persistent(self, data_dir: str):
        return await self.playwright.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=Config.HEADLESS,
            slow_mo=Config.SLOW_MO,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    # ─── LinkedIn Login (AI-assisted) ──────────────────────────────────────────

    async def linkedin_login(self):
        """Log into LinkedIn — AI handles unexpected states."""
        page = await self.context.new_page()
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Check if already logged in
        if "/feed" in page.url and await page.locator("input.search-global-typeahead__input").count() > 0:
            log.info("Already logged into LinkedIn")
            await page.close()
            return True

        if not Config.LINKEDIN_EMAIL or not Config.LINKEDIN_PASSWORD:
            log.error("LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set in .env")
            await page.close()
            return False

        # Navigate to login page
        await page.goto("https://www.linkedin.com/login")
        await page.wait_for_timeout(2000)

        # Let AI handle the login form — it can see what's on the page
        login_info = dict(self.info)
        login_info["linkedin_email"] = Config.LINKEDIN_EMAIL
        login_info["linkedin_password"] = Config.LINKEDIN_PASSWORD

        result = await run_agent(
            page=page,
            goal="Log into LinkedIn. Fill the email/username field with the linkedin_email and the password field with the linkedin_password, then click the Sign In button. If you see a security challenge or captcha, report needs_human.",
            personal_info=login_info,
            resume_text="",
            max_steps=5,
            on_step=lambda s, t: log.info("Login step %d: %s", s, t[:100]),
        )

        if result.get("needs_human"):
            log.warning("Security challenge detected — solve it manually in the browser!")
            # Wait for manual resolution
            for _ in range(24):
                await page.wait_for_timeout(5000)
                if "/feed" in page.url:
                    break
            else:
                log.error("LinkedIn login failed — challenge not resolved")
                await page.close()
                return False

        await page.wait_for_timeout(3000)

        # Verify login
        if "/feed" in page.url or "/mynetwork" in page.url:
            log.info("LinkedIn login successful")
            await page.close()
            return True

        # Check current state
        if "checkpoint" in page.url or "challenge" in page.url:
            log.warning("Security challenge — solve it in the browser window!")
            for _ in range(24):
                await page.wait_for_timeout(5000)
                if "/feed" in page.url:
                    log.info("LinkedIn login successful after challenge")
                    await page.close()
                    return True
            log.error("Login failed — challenge not resolved")
            await page.close()
            return False

        log.info("LinkedIn login completed (url: %s)", page.url)
        await page.close()
        return True

    # ─── LinkedIn Easy Apply (AI-driven) ────────────────────────────────────────

    async def linkedin_easy_apply(self, job_url: str, job_description: str = "", on_step: callable = None) -> dict:
        """
        Apply to a LinkedIn job via Easy Apply — AI navigates the entire flow.
        Returns: {"success": bool, "message": str}
        """
        page = await self.context.new_page()
        result = {"success": False, "message": ""}

        try:
            await page.goto(job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Click the Easy Apply button first
            easy_btn = page.locator("button.jobs-apply-button, button:has-text('Easy Apply')").first
            if await easy_btn.count() == 0:
                result["message"] = "No Easy Apply button found — may require external application"
                return result

            await easy_btn.click()
            await page.wait_for_timeout(2000)

            # Now let the AI handle the multi-step modal
            resume_text = _load_resume_text()
            agent_result = await run_agent(
                page=page,
                goal=(
                    "You are inside a LinkedIn Easy Apply modal. Fill in all the form fields "
                    "with my personal info. For each step:\n"
                    "1. Fill all empty fields with appropriate values from my info\n"
                    "2. Upload my resume if there's a file input\n"
                    "3. Click 'Next', 'Review', or 'Submit application' to proceed\n"
                    "4. When you see a confirmation/success message, report status 'done'\n"
                    "5. For questions like years of experience, salary, etc. — answer reasonably based on my resume\n"
                    "6. For Yes/No questions about work authorization, willingness to relocate, etc. — select Yes"
                ),
                personal_info=self.info,
                resume_text=resume_text,
                job_description=job_description,
                max_steps=15,
                on_step=on_step or (lambda s, t: log.info("Easy Apply step %d: %s", s, t[:100])),
            )

            result["success"] = agent_result.get("success", False)
            result["message"] = agent_result.get("message", "")
            if agent_result.get("success"):
                result["message"] = f"Application submitted via Easy Apply ({agent_result['steps']} AI steps)"
            return result

        except Exception as e:
            result["message"] = f"Error during Easy Apply: {e}"
            log.error(result["message"])
            return result
        finally:
            await page.close()

    # ─── Generic Portal Apply (AI-driven) ────────────────────────────────────────

    async def generic_apply(self, apply_url: str, job_description: str = "", on_step: callable = None) -> dict:
        """
        AI-powered form filling for any job application portal.
        Returns: {"success": bool, "message": str, "needs_review": bool}
        """
        page = await self.context.new_page()
        result = {"success": False, "message": "", "needs_review": True}

        try:
            await page.goto(apply_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            resume_text = _load_resume_text()
            agent_result = await run_agent(
                page=page,
                goal=(
                    "You are on a job application form. Fill in all the form fields "
                    "with my personal info. Upload my resume if there's a file upload. "
                    "Fill in all fields you can identify — name, email, phone, location, etc. "
                    "For custom questions, answer based on my resume. "
                    "Do NOT click the final Submit button — just fill all fields and report 'done'. "
                    "The user will review and submit manually."
                ),
                personal_info=self.info,
                resume_text=resume_text,
                job_description=job_description,
                max_steps=10,
                on_step=on_step or (lambda s, t: log.info("Form fill step %d: %s", s, t[:100])),
            )

            result["success"] = True
            result["message"] = agent_result.get("message", f"AI filled form in {agent_result.get('steps', '?')} steps")
            result["needs_review"] = True
            log.info("AI form fill complete — awaiting manual review")
            return result

        except Exception as e:
            result["message"] = f"Error filling form: {e}"
            log.error(result["message"])
            return result
        # NOTE: don't close page — let user review and submit manually


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _kill_stale_chromium(data_dir: str):
    """Kill any Chromium processes that are using the given user-data-dir."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                           capture_output=True, timeout=5)
        else:
            result = subprocess.run(
                ["pgrep", "-f", f"--user-data-dir={data_dir}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = result.stdout.strip().split()
            for pid in pids:
                if pid:
                    log.info("Killing stale Chromium process %s", pid)
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
    except Exception as e:
        log.debug("Could not kill stale Chromium: %s", e)


# ─── Module-level convenience functions ───────────────────────────────────────

_applier: AutoApplier | None = None


async def get_applier() -> AutoApplier:
    """Get or create the singleton AutoApplier instance."""
    global _applier
    if _applier is None:
        _applier = AutoApplier()
        await _applier.start()
    return _applier


async def shutdown_applier():
    global _applier
    if _applier:
        await _applier.stop()
        _applier = None
