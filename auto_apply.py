"""
auto_apply.py — Playwright-based auto-apply engine.

Two modes:
1. LinkedIn Easy Apply — logs in, clicks Easy Apply, fills steps, submits
2. Generic portal — detects form fields, fills with personal info, uploads resume

IMPORTANT: This runs YOUR browser on YOUR machine. It's your account, your actions.
LinkedIn may flag aggressive automation — use slow_mo and reasonable delays.
"""
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

from config import Config

log = logging.getLogger(__name__)

# ─── Common field-matching patterns ───────────────────────────────────────────
# Maps form field identifiers (label text, name attr, placeholder) to config keys
FIELD_MAP = {
    "first name": "first_name",
    "first_name": "first_name",
    "fname": "first_name",
    "last name": "last_name",
    "last_name": "last_name",
    "lname": "last_name",
    "full name": "name",
    "your name": "name",
    "name": "name",
    "email": "email",
    "e-mail": "email",
    "phone": "phone",
    "mobile": "phone",
    "telephone": "phone",
    "phone number": "phone",
    "location": "location",
    "city": "city",
    "current location": "city",
}


class AutoApplier:
    """Manages a persistent browser session for applying to jobs."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context: BrowserContext | None = None
        self.info = Config.personal_info()

    async def start(self):
        """Launch browser with persistent state (saves login cookies)."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path.home() / ".job-bot-browser"),
            headless=Config.HEADLESS,
            slow_mo=Config.SLOW_MO,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = self.browser
        log.info("Browser started (headless=%s)", Config.HEADLESS)

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    # ─── LinkedIn Easy Apply ──────────────────────────────────────────────────

    async def linkedin_login(self):
        """Log into LinkedIn if not already logged in."""
        page = await self.context.new_page()
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Check if already logged in
        if "/feed" in page.url and await page.locator("input.search-global-typeahead__input").count() > 0:
            log.info("Already logged into LinkedIn")
            await page.close()
            return True

        # Navigate to login
        if not Config.LINKEDIN_EMAIL or not Config.LINKEDIN_PASSWORD:
            log.error("LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set in .env")
            await page.close()
            return False

        await page.goto("https://www.linkedin.com/login")
        await page.fill("#username", Config.LINKEDIN_EMAIL)
        await page.fill("#password", Config.LINKEDIN_PASSWORD)
        await page.click("[type='submit']")
        await page.wait_for_timeout(5000)

        # Check for security challenge
        if "checkpoint" in page.url or "challenge" in page.url:
            log.warning("LinkedIn security challenge detected — solve it manually in the browser window!")
            # Wait up to 2 minutes for manual resolution
            for _ in range(24):
                await page.wait_for_timeout(5000)
                if "/feed" in page.url:
                    break
            else:
                log.error("LinkedIn login failed — challenge not resolved")
                await page.close()
                return False

        log.info("LinkedIn login successful")
        await page.close()
        return True

    async def linkedin_easy_apply(self, job_url: str) -> dict:
        """
        Apply to a LinkedIn job via Easy Apply.
        Returns: {"success": bool, "message": str}
        """
        page = await self.context.new_page()
        result = {"success": False, "message": ""}

        try:
            await page.goto(job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Find and click Easy Apply button
            easy_btn = page.locator("button.jobs-apply-button, button:has-text('Easy Apply')").first
            if await easy_btn.count() == 0:
                result["message"] = "No Easy Apply button found — may require external application"
                return result

            await easy_btn.click()
            await page.wait_for_timeout(2000)

            # Walk through Easy Apply modal steps
            max_steps = 10
            for step in range(max_steps):
                log.info("Easy Apply step %d", step + 1)

                # Fill any visible input fields
                await self._fill_visible_fields(page)

                # Upload resume if file input exists
                file_input = page.locator("input[type='file']")
                if await file_input.count() > 0:
                    if Config.RESUME_PATH.exists():
                        await file_input.set_input_files(str(Config.RESUME_PATH))
                        log.info("Uploaded resume")
                        await page.wait_for_timeout(1000)

                # Check for Submit button (final step)
                submit_btn = page.locator("button[aria-label*='Submit'], button:has-text('Submit application')")
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                    await page.wait_for_timeout(2000)
                    result["success"] = True
                    result["message"] = "Application submitted via Easy Apply"
                    log.info("Application submitted!")
                    return result

                # Click Next / Review
                next_btn = page.locator("button[aria-label='Continue to next step'], button:has-text('Next'), button:has-text('Review')")
                if await next_btn.count() > 0:
                    await next_btn.click()
                    await page.wait_for_timeout(1500)
                else:
                    # No next or submit — might be stuck
                    result["message"] = f"Got stuck at step {step + 1}. Check the browser window."
                    return result

            result["message"] = "Exceeded max steps without finding submit button"
            return result

        except Exception as e:
            result["message"] = f"Error during Easy Apply: {e}"
            log.error(result["message"])
            return result
        finally:
            await page.close()

    # ─── Generic Portal Apply ─────────────────────────────────────────────────

    async def generic_apply(self, apply_url: str) -> dict:
        """
        Navigate to a custom job portal application page and fill the form.
        Returns: {"success": bool, "message": str, "needs_review": bool}
        """
        page = await self.context.new_page()
        result = {"success": False, "message": "", "needs_review": True}

        try:
            await page.goto(apply_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Fill detectable fields
            filled_count = await self._fill_visible_fields(page)

            # Upload resume to any file input
            file_inputs = page.locator("input[type='file']")
            for i in range(await file_inputs.count()):
                fi = file_inputs.nth(i)
                accept = await fi.get_attribute("accept") or ""
                if any(ext in accept.lower() for ext in [".pdf", ".doc", "application/"]) or not accept:
                    if Config.RESUME_PATH.exists():
                        await fi.set_input_files(str(Config.RESUME_PATH))
                        log.info("Uploaded resume to file input #%d", i)
                        await page.wait_for_timeout(1000)
                        break

            result["message"] = f"Filled {filled_count} fields. Form is ready for your review."
            result["needs_review"] = True  # Always pause for review on custom portals
            result["success"] = True

            # Keep page open for manual review — don't auto-submit unknown forms
            log.info("Generic form filled — awaiting manual review")
            return result

        except Exception as e:
            result["message"] = f"Error filling form: {e}"
            log.error(result["message"])
            return result
        # NOTE: don't close page — let user review and submit manually

    # ─── Field Detection & Filling ────────────────────────────────────────────

    async def _fill_visible_fields(self, page: Page) -> int:
        """
        Detect visible form fields and fill them with personal info.
        Returns count of fields filled.
        """
        filled = 0
        inputs = page.locator("input[type='text'], input[type='email'], input[type='tel'], input:not([type])")

        for i in range(await inputs.count()):
            inp = inputs.nth(i)
            if not await inp.is_visible():
                continue

            # Skip if already filled
            current_val = await inp.input_value()
            if current_val.strip():
                continue

            # Identify field by label, name, placeholder, or aria-label
            identifier = await self._identify_field(page, inp)
            if not identifier:
                continue

            # Look up what value to fill
            config_key = FIELD_MAP.get(identifier.lower())
            if config_key and config_key in self.info and self.info[config_key]:
                await inp.fill(self.info[config_key])
                filled += 1
                log.debug("Filled '%s' → %s", identifier, config_key)

        return filled

    async def _identify_field(self, page: Page, element) -> str:
        """Try to figure out what a form field is asking for."""
        # Check aria-label
        aria = await element.get_attribute("aria-label") or ""
        if aria:
            return aria.lower().strip()

        # Check associated label
        el_id = await element.get_attribute("id") or ""
        if el_id:
            label = page.locator(f"label[for='{el_id}']")
            if await label.count() > 0:
                return (await label.inner_text()).lower().strip()

        # Check name attribute
        name = await element.get_attribute("name") or ""
        if name:
            return name.lower().replace("_", " ").replace("-", " ").strip()

        # Check placeholder
        ph = await element.get_attribute("placeholder") or ""
        return ph.lower().strip()


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
