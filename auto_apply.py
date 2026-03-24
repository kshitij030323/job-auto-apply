"""
auto_apply.py — AI-powered auto-apply engine.

The AI SEES the screen via screenshots and controls the browser like a human.
It follows external links, handles redirects, fills multi-page forms, and submits.

Two modes:
1. LinkedIn Easy Apply — AI navigates the modal step by step
2. External / Generic — AI follows the "Apply" link to any portal and applies there

The AI is used at EVERY step — it decides what to fill, what to click, where to go.
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

        if "/feed" in page.url and await page.locator("input.search-global-typeahead__input").count() > 0:
            log.info("Already logged into LinkedIn")
            await page.close()
            return True

        if not Config.LINKEDIN_EMAIL or not Config.LINKEDIN_PASSWORD:
            log.error("LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set in .env")
            await page.close()
            return False

        await page.goto("https://www.linkedin.com/login")
        await page.wait_for_timeout(2000)

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
            for _ in range(24):
                await page.wait_for_timeout(5000)
                if "/feed" in page.url:
                    break
            else:
                log.error("LinkedIn login failed — challenge not resolved")
                await page.close()
                return False

        await page.wait_for_timeout(3000)

        if "/feed" in page.url or "/mynetwork" in page.url:
            log.info("LinkedIn login successful")
            await page.close()
            return True

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
        Apply to a LinkedIn job. Handles both:
        - Easy Apply (modal on LinkedIn)
        - External Apply (follows link to company portal and applies there)

        The AI screenshots the page and decides what to do at every step.
        """
        page = await self.context.new_page()
        result = {"success": False, "message": ""}

        try:
            await page.goto(job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Check for Easy Apply button
            easy_btn = page.locator("button.jobs-apply-button, button:has-text('Easy Apply')").first
            if await easy_btn.count() > 0:
                # ── Easy Apply flow ──
                await easy_btn.click()
                await page.wait_for_timeout(2000)

                resume_text = _load_resume_text()
                agent_result = await run_agent(
                    page=page,
                    goal=(
                        "You are inside a LinkedIn Easy Apply modal. Fill ALL form fields "
                        "with my personal info. For each step:\n"
                        "1. Fill all empty fields with appropriate values from my info\n"
                        "2. Upload my resume if there's a file input\n"
                        "3. Click 'Next', 'Review', or 'Submit application' to proceed\n"
                        "4. When you see a confirmation/success message, report status 'done'\n"
                        "5. For questions like years of experience, salary, etc. — answer reasonably based on my resume\n"
                        "6. For Yes/No questions about work authorization, willingness to relocate, etc. — select Yes\n"
                        "7. Do NOT hesitate — fill everything and keep clicking Next/Submit until done"
                    ),
                    personal_info=self.info,
                    resume_text=resume_text,
                    job_description=job_description,
                    max_steps=20,
                    on_step=on_step or (lambda s, t: log.info("Easy Apply step %d: %s", s, t[:100])),
                )

                result["success"] = agent_result.get("success", False)
                result["message"] = agent_result.get("message", "")
                if agent_result.get("success"):
                    result["message"] = f"Application submitted via Easy Apply ({agent_result['steps']} AI steps)"
                return result

            # ── No Easy Apply — check for external Apply button ──
            log.info("No Easy Apply button — looking for external Apply link...")
            external_result = await self._follow_external_apply(page, job_description, on_step)
            return external_result

        except Exception as e:
            result["message"] = f"Error during apply: {e}"
            log.error(result["message"])
            return result
        finally:
            await page.close()

    async def _follow_external_apply(self, page: Page, job_description: str, on_step: callable = None) -> dict:
        """
        When LinkedIn shows an external 'Apply' button, follow it to the company
        portal and let the AI apply there. The AI sees the screen and handles everything.
        """
        result = {"success": False, "message": ""}

        # Use AI to find and click the external apply button
        resume_text = _load_resume_text()
        agent_result = await run_agent(
            page=page,
            goal=(
                "Look at the page. Find the 'Apply' button (it may say 'Apply', 'Apply now', "
                "'Apply on company website', or similar). Click it to go to the external application page. "
                "If you see a popup asking to continue to external site, click 'Continue' or 'Apply'. "
                "Once you land on the external application page, report status 'done' so we can proceed."
            ),
            personal_info=self.info,
            resume_text="",
            max_steps=5,
            on_step=on_step or (lambda s, t: log.info("External nav step %d: %s", s, t[:100])),
        )

        # Wait for potential navigation/redirect
        await page.wait_for_timeout(3000)
        current_url = page.url
        log.info("Navigated to external page: %s", current_url)

        # Now the AI is on the external portal — let it fill the application
        log.info("AI is now on external portal — filling application form...")
        apply_result = await run_agent(
            page=page,
            goal=(
                "You are on a job application page (external company portal). "
                "Fill in ALL form fields with my personal info. This is a REAL application — "
                "be thorough and fill everything:\n"
                "1. Fill name, email, phone, location, and all other fields\n"
                "2. Upload my resume if there's a file upload field\n"
                "3. For custom questions (years of experience, salary, visa, etc.) answer reasonably based on my resume\n"
                "4. For Yes/No questions about work authorization, willingness to relocate — select Yes\n"
                "5. If there are multiple pages/steps, fill each page and click Next/Continue\n"
                "6. Click Submit/Apply when all fields are filled\n"
                "7. If you see a success/confirmation message, report 'done'\n"
                "8. If you need to create an account first, try to do it with my email\n"
                "9. Do NOT skip any fields — fill EVERYTHING you can\n"
                "10. If the page requires login to a portal you don't have access to, report 'needs_human'"
            ),
            personal_info=self.info,
            resume_text=resume_text,
            job_description=job_description,
            max_steps=25,
            on_step=on_step or (lambda s, t: log.info("External apply step %d: %s", s, t[:100])),
        )

        result["success"] = apply_result.get("success", False)
        result["message"] = apply_result.get("message", "")
        if apply_result.get("success"):
            result["message"] = f"Applied via external portal ({apply_result['steps']} AI steps)"
        elif apply_result.get("needs_human"):
            result["message"] = f"External portal needs human help: {apply_result.get('message', '')}"
            result["needs_review"] = True
        return result

    # ─── Generic Portal Apply (AI-driven) ────────────────────────────────────────

    async def generic_apply(self, apply_url: str, job_description: str = "", on_step: callable = None) -> dict:
        """
        AI-powered application for any job portal URL.
        The AI screenshots the screen, fills forms, follows links, and submits.
        """
        page = await self.context.new_page()
        result = {"success": False, "message": ""}

        try:
            await page.goto(apply_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            resume_text = _load_resume_text()

            # First, check if this is a job listing page (not an application form)
            # The AI will figure out if it needs to click "Apply" first
            agent_result = await run_agent(
                page=page,
                goal=(
                    "You are on a job-related page. Your goal is to APPLY for this job. "
                    "Look at the screenshot carefully:\n\n"
                    "IF this is a job listing page (shows job description, not a form):\n"
                    "  - Find and click the 'Apply', 'Apply Now', 'Apply for this job' button\n"
                    "  - Follow any redirects to the application form\n\n"
                    "IF this is an application form:\n"
                    "  - Fill ALL fields with my personal info\n"
                    "  - Upload my resume to any file upload field\n"
                    "  - For custom questions, answer based on my resume\n"
                    "  - For Yes/No questions (work authorization, relocate, etc.) — select Yes\n"
                    "  - Navigate through all steps (click Next/Continue)\n"
                    "  - Click Submit/Apply at the end\n\n"
                    "IF the page asks to login/create account:\n"
                    "  - Try creating an account with my email if possible\n"
                    "  - If login is required and you can't proceed, report 'needs_human'\n\n"
                    "IMPORTANT: Do NOT stop at just filling fields — keep going through "
                    "all steps until the application is SUBMITTED. Fill EVERY field you can. "
                    "When you see a confirmation/success message, report 'done'."
                ),
                personal_info=self.info,
                resume_text=resume_text,
                job_description=job_description,
                max_steps=25,
                on_step=on_step or (lambda s, t: log.info("Apply step %d: %s", s, t[:100])),
            )

            result["success"] = agent_result.get("success", False)
            result["message"] = agent_result.get("message", "")
            if agent_result.get("success"):
                result["message"] = f"Application submitted ({agent_result['steps']} AI steps)"
            elif agent_result.get("needs_human"):
                result["needs_review"] = True
            return result

        except Exception as e:
            result["message"] = f"Error: {e}"
            log.error(result["message"])
            return result
        finally:
            await page.close()


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
