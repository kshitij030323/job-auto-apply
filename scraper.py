"""
scraper.py — Extracts job descriptions by screenshotting pages and using AI vision.

The AI literally sees the rendered page and extracts the job info — no CSS selectors.
"""
import logging
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Page

from config import Config
from ai_agent import take_screenshot, extract_job_from_screenshot

log = logging.getLogger(__name__)


def _normalize_linkedin_url(url: str) -> str:
    """Convert LinkedIn collection/search URLs to direct job view URLs."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    job_id = params.get("currentJobId", [None])[0]
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return url


async def scrape_job_description(url: str) -> dict:
    """
    Visit a job URL, screenshot it, and ask AI to extract the job info.
    Returns: {"title": str, "company": str, "description": str, "url": str}
    """
    is_linkedin = "linkedin.com" in url

    if is_linkedin:
        url = _normalize_linkedin_url(url)
        return await _scrape_with_applier_browser(url)

    # Non-LinkedIn: standalone browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=Config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            return await _extract_via_screenshot(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s", url, e)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await page.close()
            await browser.close()


async def _scrape_with_applier_browser(url: str) -> dict:
    """Scrape LinkedIn using the shared browser session (has login cookies)."""
    from auto_apply import get_applier

    try:
        applier = await get_applier()
        if applier.context is None:
            raise RuntimeError("Browser not available — run 'login' first")
        page = await applier.context.new_page()
    except Exception as e:
        log.warning("Shared browser not available (%s), using standalone", e)
        return await _scrape_standalone(url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        # Scroll down to load the full job description
        await page.evaluate("window.scrollBy(0, 300)")
        await page.wait_for_timeout(1000)

        # Try clicking "Show more" / "See more" if visible
        try:
            show_more = page.locator("button:has-text('Show more'), button:has-text('See more'), button:has-text('...more')").first
            if await show_more.count() > 0 and await show_more.is_visible():
                await show_more.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        return await _extract_via_screenshot(page, url)
    except Exception as e:
        log.error("Scraping failed for %s: %s", url, e)
        return {"title": "", "company": "", "description": "", "url": url}
    finally:
        await page.close()


async def _scrape_standalone(url: str) -> dict:
    """Fallback: scrape with a fresh browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=Config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            return await _extract_via_screenshot(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s", url, e)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await page.close()
            await browser.close()


async def _extract_via_screenshot(page: Page, url: str) -> dict:
    """
    Take a screenshot + grab page text, send both to AI for extraction.
    The AI SEES the actual rendered page.
    """
    # Take screenshot of what's visible
    screenshot_b64 = await take_screenshot(page)

    # Also grab raw text as backup context
    try:
        page_text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        page_text = ""

    if not screenshot_b64 and not page_text:
        log.warning("Could not capture page content at all")
        return {"title": "", "company": "", "description": "", "url": url}

    # Also try to get a full-page screenshot for long JDs
    # by scrolling and getting the text below the fold
    try:
        full_text = await page.evaluate("""() => {
            const body = document.body.innerText || '';
            return body.substring(0, 8000);
        }""")
        if len(full_text) > len(page_text):
            page_text = full_text
    except Exception:
        pass

    if not Config.LLM_API_KEY:
        log.warning("No LLM_API_KEY — returning raw text as description")
        return {"title": "", "company": "", "description": page_text[:5000], "url": url}

    # Ask AI to extract job info from what it sees
    result = extract_job_from_screenshot(screenshot_b64, page_text, url)

    if result.get("description"):
        log.info("AI extracted: '%s' at '%s' (%d chars)",
                 result.get("title", "")[:50], result.get("company", "")[:30],
                 len(result["description"]))
    else:
        log.warning("AI couldn't extract description — using raw text fallback")
        result["description"] = page_text[:5000]

    return result
