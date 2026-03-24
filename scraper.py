"""
scraper.py — Extracts job description text from a URL using Playwright.

Uses AI (Qwen 3.5 via NVIDIA NIM) to extract structured job info from
raw page text — no brittle CSS selectors that break when sites update.
"""
import json
import logging
import re
from urllib.parse import urlparse, parse_qs

from openai import OpenAI
from playwright.async_api import async_playwright, Page

from config import Config

log = logging.getLogger(__name__)

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL)
    return _client


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
    Visit a job URL, extract structured info using AI.
    Returns: {"title": str, "company": str, "description": str, "url": str}
    """
    is_linkedin = "linkedin.com" in url

    if is_linkedin:
        url = _normalize_linkedin_url(url)
        return await _scrape_with_applier_browser(url)

    # For non-LinkedIn URLs, launch a standalone browser.
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=Config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            return await _extract_with_ai(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s (type: %s)", url, e, type(e).__name__)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await page.close()
            await browser.close()


async def _scrape_with_applier_browser(url: str) -> dict:
    """Scrape a LinkedIn URL using the shared AutoApplier browser session."""
    from auto_apply import get_applier

    try:
        applier = await get_applier()
        if applier.context is None:
            raise RuntimeError("Browser session not available — run 'login' first")
        page = await applier.context.new_page()
    except Exception as e:
        log.warning("Shared browser not available (%s), launching standalone browser", e)
        return await _scrape_standalone(url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        return await _extract_with_ai(page, url)
    except Exception as e:
        log.error("Scraping failed for %s: %s (type: %s)", url, e, type(e).__name__)
        return {"title": "", "company": "", "description": "", "url": url}
    finally:
        await page.close()


async def _scrape_standalone(url: str) -> dict:
    """Fallback: scrape with a fresh non-persistent browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=Config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            return await _extract_with_ai(page, url)
        except Exception as e:
            log.error("Scraping failed for %s: %s (type: %s)", url, e, type(e).__name__)
            return {"title": "", "company": "", "description": "", "url": url}
        finally:
            await page.close()
            await browser.close()


# ─── AI-Powered Extraction ───────────────────────────────────────────────────

_EXTRACT_PAGE_TEXT_JS = """
() => {
    // Get all visible text from the page, structured by sections
    const parts = [];

    // Title - try multiple approaches
    const h1 = document.querySelector('h1');
    if (h1) parts.push('PAGE_TITLE: ' + h1.innerText.trim());

    // Get the full body text — the AI will parse it
    const body = document.body.innerText || '';
    parts.push(body);

    return parts.join('\\n');
}
"""


async def _extract_with_ai(page: Page, url: str) -> dict:
    """
    Extract job info by grabbing ALL visible page text and asking the AI
    to parse out the title, company, and description. No CSS selectors needed.
    """
    # Step 1: Get raw page text
    try:
        raw_text = await page.evaluate(_EXTRACT_PAGE_TEXT_JS)
    except Exception as e:
        log.error("Failed to extract page text: %s", e)
        return {"title": "", "company": "", "description": "", "url": url}

    if not raw_text or len(raw_text.strip()) < 50:
        log.warning("Page has very little text content (%d chars)", len(raw_text or ""))
        return {"title": "", "company": "", "description": "", "url": url}

    # Truncate to avoid token limits (keep first 6000 chars — enough for any JD)
    raw_text = raw_text[:6000]

    # Step 2: Ask AI to extract structured info
    if not Config.LLM_API_KEY:
        log.warning("No LLM_API_KEY set — falling back to raw text extraction")
        return _fallback_extract(raw_text, url)

    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You extract job posting information from raw web page text. "
                    "Return ONLY a JSON object with these fields:\n"
                    '{"title": "job title", "company": "company name", "description": "full job description text"}\n'
                    "For the description, include ALL the job details: responsibilities, requirements, qualifications, benefits, etc. "
                    "Do NOT summarize — keep the full text. Do NOT add commentary. "
                    "If you cannot find a field, use an empty string."
                )},
                {"role": "user", "content": f"Extract the job posting info from this page text:\n\n{raw_text}"},
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        raw_resp = resp.choices[0].message.content.strip()
        result = _parse_json_response(raw_resp)
        result["url"] = url

        if result.get("description"):
            log.info("AI extracted JD: '%s' at '%s' (%d chars)",
                     result.get("title", "")[:50], result.get("company", "")[:30],
                     len(result["description"]))
        else:
            log.warning("AI could not extract description from page")
            # Fall back to raw text
            fb = _fallback_extract(raw_text, url)
            if fb["description"]:
                return fb

        return result

    except Exception as e:
        log.error("AI extraction failed: %s — falling back to raw text", e)
        return _fallback_extract(raw_text, url)


def _parse_json_response(raw: str) -> dict:
    """Parse JSON from AI response, handling markdown blocks and think tags."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"title": "", "company": "", "description": ""}


def _fallback_extract(raw_text: str, url: str) -> dict:
    """
    Simple text-based fallback when AI is unavailable.
    Grabs the first h1-like line as title and the bulk text as description.
    """
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    title = ""
    company = ""
    desc_lines = []

    for i, line in enumerate(lines):
        if line.startswith("PAGE_TITLE: "):
            title = line.replace("PAGE_TITLE: ", "")
            continue
        desc_lines.append(line)

    description = "\n".join(desc_lines).strip()
    # Clean up
    description = re.sub(r"\n{3,}", "\n\n", description)
    description = description[:5000]

    return {"title": title, "company": company, "description": description, "url": url}
