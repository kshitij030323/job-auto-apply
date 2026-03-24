"""
scraper.py — Extracts job description text from a URL using Playwright.
Works for LinkedIn, Lever, Greenhouse, Workday, and generic pages.
"""
import logging
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Page

from config import Config

log = logging.getLogger(__name__)

# Persistent browser profile path (shared with auto_apply.py)
_BROWSER_DATA_DIR = str(Path.home() / ".job-bot-browser")


def _normalize_linkedin_url(url: str) -> str:
    """
    Convert LinkedIn collection/search URLs to direct job view URLs.
    e.g. .../jobs/collections/recommended/?currentJobId=123... → .../jobs/view/123
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    job_id = params.get("currentJobId", [None])[0]
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return url


async def scrape_job_description(url: str) -> dict:
    """
    Visit a job URL, extract structured info.
    Returns: {"title": str, "company": str, "description": str, "url": str}
    """
    is_linkedin = "linkedin.com" in url

    if is_linkedin:
        url = _normalize_linkedin_url(url)

    async with async_playwright() as p:
        # Use persistent context for LinkedIn (needs login cookies),
        # plain headless browser for everything else.
        if is_linkedin:
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=_BROWSER_DATA_DIR,
                headless=Config.HEADLESS,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page()
        else:
            browser = await p.chromium.launch(headless=Config.HEADLESS)
            page = await browser.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)  # let JS render

            # Detect platform and extract accordingly
            if is_linkedin:
                return await _extract_linkedin(page, url)
            elif "lever.co" in url:
                return await _extract_lever(page, url)
            elif "greenhouse.io" in url or "boards.greenhouse" in url:
                return await _extract_greenhouse(page, url)
            else:
                return await _extract_generic(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s (type: %s)", url, e, type(e).__name__)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await page.close()
            await browser.close()


async def _extract_linkedin(page: Page, url: str) -> dict:
    # Wait for job detail content to load
    try:
        await page.wait_for_selector(
            ".description__text, .show-more-less-html__markup, "
            ".jobs-description__content, .jobs-description, "
            "section.description, .job-details-jobs-unified-top-card__job-title",
            timeout=10000,
        )
    except Exception:
        log.warning("LinkedIn job detail selectors did not appear — page may require login")

    title = await _safe_text(
        page,
        "h1.top-card-layout__title, h1.topcard__title, "
        ".job-details-jobs-unified-top-card__job-title, "
        ".jobs-unified-top-card__job-title, h1",
    )
    company = await _safe_text(
        page,
        "a.topcard__org-name-link, "
        ".top-card-layout__second-subline a, "
        ".topcard__flavor--black-link, "
        ".job-details-jobs-unified-top-card__company-name, "
        ".jobs-unified-top-card__company-name",
    )
    desc = await _safe_text(
        page,
        ".description__text, "
        ".show-more-less-html__markup, "
        ".jobs-description__content, "
        ".jobs-description, "
        "section.description, "
        "article.jobs-description__container",
    )

    if not desc:
        log.warning("No job description found on LinkedIn page: %s", url)

    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_lever(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h2.posting-headline, .posting-headline h2")
    company = await _safe_text(page, ".main-header-logo a, .posting-categories .sort-by-time, .company-name")
    desc = await _safe_text(page, "[data-qa='job-description'], .section-wrapper.page-full-width, .posting-page")
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_greenhouse(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h1.app-title, .app-title")
    company = await _safe_text(page, "span.company-name, .company-name")
    desc = await _safe_text(page, "#content, .content")
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_generic(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h1")
    # Try common patterns for company name
    company = await _safe_text(page, "[class*='company'], [class*='org'], [data-company], [class*='employer']")
    # Get the main content area
    desc = await _safe_text(page, "main, article, [role='main'], .job-description, #job-description, [class*='description']")
    if not desc:
        desc = await _safe_text(page, "body")
    desc = desc[:5000] if desc else ""
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _safe_text(page: Page, selector: str) -> str:
    """Safely extract text from the first matching element."""
    try:
        locator = page.locator(selector)
        if await locator.count() > 0:
            return (await locator.first.inner_text()).strip()
    except Exception:
        pass
    return ""


def _clean(text: str) -> str:
    """Collapse whitespace, strip cruft."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
