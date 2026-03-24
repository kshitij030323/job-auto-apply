"""
scraper.py — Extracts job description text from a URL using Playwright.
Works for LinkedIn, Lever, Greenhouse, Workday, and generic pages.
"""
import logging
import re

from playwright.async_api import async_playwright, Page

log = logging.getLogger(__name__)


async def scrape_job_description(url: str) -> dict:
    """
    Visit a job URL, extract structured info.
    Returns: {"title": str, "company": str, "description": str, "url": str}
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)  # let JS render

            # Detect platform and extract accordingly
            if "linkedin.com" in url:
                return await _extract_linkedin(page, url)
            elif "lever.co" in url:
                return await _extract_lever(page, url)
            elif "greenhouse.io" in url or "boards.greenhouse" in url:
                return await _extract_greenhouse(page, url)
            else:
                return await _extract_generic(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s", url, e)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await browser.close()


async def _extract_linkedin(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h1.top-card-layout__title, h1.topcard__title, h1")
    company = await _safe_text(page, "a.topcard__org-name-link, .top-card-layout__second-subline a, .topcard__flavor--black-link")
    desc = await _safe_text(page, ".description__text, .show-more-less-html__markup, section.description")
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_lever(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h2.posting-headline")
    company = await _safe_text(page, ".main-header-logo a, .posting-categories .sort-by-time")
    desc = await _safe_text(page, "[data-qa='job-description'], .section-wrapper.page-full-width")
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_greenhouse(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h1.app-title")
    company = await _safe_text(page, "span.company-name")
    desc = await _safe_text(page, "#content")
    return {"title": title, "company": company, "description": _clean(desc), "url": url}


async def _extract_generic(page: Page, url: str) -> dict:
    title = await _safe_text(page, "h1")
    # Try common patterns for company name
    company = await _safe_text(page, "[class*='company'], [class*='org'], [data-company]")
    # Get the main content area
    desc = await _safe_text(page, "main, article, [role='main'], .job-description, #job-description")
    if not desc:
        desc = await _safe_text(page, "body")
    return {"title": title, "company": company, "description": _clean(desc[:5000]), "url": url}


async def _safe_text(page: Page, selector: str) -> str:
    """Safely extract text from the first matching element."""
    try:
        el = page.locator(selector).first
        if await el.count() > 0:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


def _clean(text: str) -> str:
    """Collapse whitespace, strip cruft."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
