"""
ai_agent.py — Vision-powered browser automation agent.

The AI literally SEES the screen via screenshots and decides what to do:
1. Takes a screenshot of the current page
2. Also extracts DOM state (fields, buttons) for precise action targeting
3. Sends BOTH the screenshot + DOM info to the vision LLM
4. Gets back structured JSON actions (fill, click, select, upload, etc.)
5. Executes those actions via Playwright
6. Loops until the goal is achieved

The screenshot is the primary input — the AI controls the screen like a human.
"""
import base64
import json
import logging
import re

from openai import OpenAI
from playwright.async_api import Page

from config import Config

log = logging.getLogger(__name__)

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
        )
    return _client


# ─── Screenshot Capture ──────────────────────────────────────────────────────

async def take_screenshot(page: Page) -> str:
    """Take a screenshot and return as base64-encoded PNG string."""
    try:
        screenshot_bytes = await page.screenshot(full_page=False, type="png")
        return base64.b64encode(screenshot_bytes).decode("utf-8")
    except Exception as e:
        log.error("Failed to take screenshot: %s", e)
        return ""


# ─── DOM State Extraction (for action targeting) ─────────────────────────────

_EXTRACT_DOM_STATE_JS = """
() => {
    const state = { url: location.href, title: document.title, fields: [], buttons: [] };

    // Extract visible form fields
    const inputs = document.querySelectorAll('input, textarea, select');
    let fieldIdx = 0;
    inputs.forEach(el => {
        if (el.offsetParent === null && el.type !== 'file') return;
        if (el.type === 'hidden') return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0 && el.type !== 'file') return;

        let label = '';
        if (el.id) {
            const labelEl = document.querySelector(`label[for='${el.id}']`);
            if (labelEl) label = labelEl.innerText.trim();
        }
        if (!label && el.closest('label')) {
            label = el.closest('label').innerText.trim();
        }
        if (!label) {
            const prev = el.previousElementSibling;
            if (prev && ['LABEL','SPAN','DIV','P'].includes(prev.tagName)) {
                label = prev.innerText.trim();
            }
        }

        const field = {
            idx: fieldIdx++,
            tag: el.tagName.toLowerCase(),
            type: el.type || 'text',
            name: el.name || '',
            id: el.id || '',
            placeholder: el.placeholder || '',
            label: label.substring(0, 100),
            value: el.value || '',
            aria_label: el.getAttribute('aria-label') || '',
            required: el.required || el.getAttribute('aria-required') === 'true',
            options: [],
        };

        if (el.tagName === 'SELECT') {
            field.options = Array.from(el.options).slice(0, 20).map(o => ({
                value: o.value, text: o.text.trim()
            }));
        }
        if (el.type === 'file') field.accept = el.accept || '';

        state.fields.push(field);
    });

    // Extract visible buttons
    const btns = document.querySelectorAll(
        'button, [role="button"], input[type="submit"], a.btn, a.button'
    );
    let btnIdx = 0;
    btns.forEach(el => {
        if (el.offsetParent === null) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return;
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        if (!text) return;

        state.buttons.push({
            idx: btnIdx++,
            tag: el.tagName.toLowerCase(),
            text: text.substring(0, 80),
            type: el.type || '',
            id: el.id || '',
            aria_label: el.getAttribute('aria-label') || '',
            disabled: el.disabled || false,
        });
    });

    return state;
}
"""


async def extract_dom_state(page: Page) -> dict:
    """Extract DOM state (fields, buttons) for action targeting."""
    try:
        return await page.evaluate(_EXTRACT_DOM_STATE_JS)
    except Exception as e:
        log.error("Failed to extract DOM state: %s", e)
        return {"url": page.url, "title": "", "fields": [], "buttons": []}


# ─── AI Decision Making (Vision) ─────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an AI agent that can SEE a web browser screenshot and control it. You will receive:
1. A SCREENSHOT of what's currently on screen
2. A list of detected form fields and buttons (for precise targeting)
3. The user's personal info and resume
4. A goal to accomplish

Look at the screenshot to understand the page. Then decide what actions to take.

IMPORTANT RULES:
1. Only fill fields that are currently EMPTY (value is "")
2. Match what you SEE in the screenshot to the right personal info
3. For dropdowns, pick the best matching option
4. For file upload fields, use action "upload" — the system handles the file
5. If you see a "Next", "Continue", or "Submit" button and all visible fields are filled, click it
6. If you see a success/confirmation message, report status "done"
7. If you're stuck, report status "stuck"
8. For custom questions (years of experience, salary, visa, etc.), answer reasonably based on the resume
9. NEVER fabricate experience — only use provided info
10. Look at the SCREENSHOT to understand what the page actually shows — trust your eyes

Respond with ONLY valid JSON (no markdown, no explanation):
{
    "thinking": "I can see [describe what you see on screen]. I will [describe your plan].",
    "actions": [
        {"type": "fill", "field_idx": 0, "value": "John Doe"},
        {"type": "select", "field_idx": 3, "value": "option_value"},
        {"type": "click", "button_idx": 2},
        {"type": "upload", "field_idx": 5},
        {"type": "wait", "seconds": 2}
    ],
    "status": "continue"
}

Status values: "continue", "done", "stuck", "needs_human" (captcha/challenge)"""


def _build_fields_text(dom_state: dict) -> str:
    """Build a compact text description of the DOM fields and buttons."""
    parts = []
    for f in dom_state.get("fields", []):
        desc = f"[{f['idx']}] {f['tag']}({f['type']})"
        if f.get("label"): desc += f' label="{f["label"]}"'
        if f.get("name"): desc += f' name="{f["name"]}"'
        if f.get("placeholder"): desc += f' placeholder="{f["placeholder"]}"'
        if f.get("value"): desc += f' value="{f["value"]}"'
        if f.get("required"): desc += " [REQUIRED]"
        if f.get("options"):
            opts = ", ".join(f'{o["value"]}={o["text"]}' for o in f["options"][:10])
            desc += f" options=[{opts}]"
        if f.get("accept"): desc += f' accept="{f["accept"]}"'
        parts.append(desc)

    btns = []
    for b in dom_state.get("buttons", []):
        desc = f'[{b["idx"]}] "{b["text"]}"'
        if b.get("aria_label"): desc += f' aria-label="{b["aria_label"]}"'
        if b.get("disabled"): desc += " [DISABLED]"
        btns.append(desc)

    return f"FIELDS:\n" + ("\n".join(parts) or "(none)") + f"\n\nBUTTONS:\n" + ("\n".join(btns) or "(none)")


def ask_ai_with_vision(
    goal: str,
    screenshot_b64: str,
    dom_state: dict,
    personal_info: dict,
    resume_text: str,
    job_description: str = "",
) -> dict:
    """Send screenshot + DOM state to the vision AI and get back actions."""
    client = _get_client()

    fields_text = _build_fields_text(dom_state)

    text_content = f"""## GOAL
{goal}

## PAGE: {dom_state.get('url', '')}

## DETECTED FORM ELEMENTS
{fields_text}

## YOUR PERSONAL INFO
Name: {personal_info.get('name', '')}
First Name: {personal_info.get('first_name', '')}
Last Name: {personal_info.get('last_name', '')}
Email: {personal_info.get('email', '')}
Phone: {personal_info.get('phone', '')}
Location: {personal_info.get('location', '')}
City: {personal_info.get('city', '')}

## YOUR RESUME
{resume_text[:2000]}"""

    if job_description:
        text_content += f"\n\n## JOB DESCRIPTION\n{job_description[:1500]}"

    # Build the message with both image and text
    user_content = []

    # Add screenshot if available
    if screenshot_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
        })

    user_content.append({"type": "text", "text": text_content})

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content.strip()
        log.debug("AI raw response: %s", raw[:500])
        return _parse_ai_response(raw)
    except Exception as e:
        log.error("AI vision call failed: %s", e)
        # Fall back to text-only if vision fails
        return _ask_ai_text_only(goal, dom_state, personal_info, resume_text, job_description)


def _ask_ai_text_only(goal, dom_state, personal_info, resume_text, job_description=""):
    """Fallback: text-only AI call when vision is unavailable."""
    client = _get_client()
    fields_text = _build_fields_text(dom_state)

    text_msg = f"""## GOAL
{goal}

## PAGE: {dom_state.get('url', '')} — {dom_state.get('title', '')}

## DETECTED FORM ELEMENTS
{fields_text}

## YOUR PERSONAL INFO
Name: {personal_info.get('name', '')} | Email: {personal_info.get('email', '')}
Phone: {personal_info.get('phone', '')} | Location: {personal_info.get('location', '')}

## YOUR RESUME
{resume_text[:2000]}"""

    if job_description:
        text_msg += f"\n\n## JOB DESCRIPTION\n{job_description[:1500]}"

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text_msg},
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content.strip()
        return _parse_ai_response(raw)
    except Exception as e:
        log.error("AI text-only call also failed: %s", e)
        return {"thinking": f"API error: {e}", "actions": [], "status": "stuck"}


def _parse_ai_response(raw: str) -> dict:
    """Parse the AI's JSON response, handling common formatting issues."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Strip <think> tags from reasoning models
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
        log.error("Failed to parse AI response: %s", text[:500])
        return {"thinking": "Failed to parse response", "actions": [], "status": "stuck"}


# ─── Vision-Based JD Extraction ──────────────────────────────────────────────

def extract_job_from_screenshot(screenshot_b64: str, page_text: str, url: str) -> dict:
    """
    Ask the AI to extract job info from a screenshot of the page.
    This is used by the scraper — the AI sees the actual rendered page.
    """
    client = _get_client()

    user_content = []
    if screenshot_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
        })
    user_content.append({
        "type": "text",
        "text": (
            "Extract the job posting information from this page screenshot.\n"
            "Return ONLY a JSON object:\n"
            '{"title": "job title", "company": "company name", "description": "full job description"}\n\n'
            "Include ALL job details in the description — responsibilities, requirements, qualifications, benefits.\n"
            "Do NOT summarize. Keep the full text. If you can't find something, use empty string.\n\n"
            f"Page URL: {url}\n\n"
            f"Page text (for reference):\n{page_text[:4000]}"
        ),
    })

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You extract job posting information from web pages. Return only valid JSON."},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        raw = resp.choices[0].message.content.strip()
        result = _parse_ai_response(raw)
        result["url"] = url
        return result
    except Exception as e:
        log.error("Vision JD extraction failed: %s", e)
        return {"title": "", "company": "", "description": "", "url": url}


# ─── Action Execution ────────────────────────────────────────────────────────

async def _execute_fill(page: Page, fields: list, field_idx: int, value: str) -> bool:
    """Fill a form field by its extracted index."""
    if field_idx >= len(fields):
        log.warning("Field index %d out of range (%d fields)", field_idx, len(fields))
        return False

    field = fields[field_idx]
    try:
        selector = _build_selector(field)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            log.warning("Could not find field: %s", selector)
            return False
        await locator.click()
        await locator.fill(value)
        log.info("Filled [%d] '%s' = '%s'",
                 field_idx, field.get("label") or field.get("name") or field.get("placeholder"), value[:30])
        return True
    except Exception as e:
        log.warning("Failed to fill [%d]: %s", field_idx, e)
        return False


async def _execute_select(page: Page, fields: list, field_idx: int, value: str) -> bool:
    """Select an option in a dropdown."""
    if field_idx >= len(fields):
        return False
    field = fields[field_idx]
    try:
        selector = _build_selector(field)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return False
        try:
            await locator.select_option(value=value)
        except Exception:
            await locator.select_option(label=value)
        log.info("Selected [%d] = '%s'", field_idx, value)
        return True
    except Exception as e:
        log.warning("Failed to select [%d]: %s", field_idx, e)
        return False


async def _execute_click(page: Page, buttons: list, button_idx: int) -> bool:
    """Click a button by its extracted index."""
    if button_idx >= len(buttons):
        log.warning("Button index %d out of range (%d buttons)", button_idx, len(buttons))
        return False
    btn = buttons[button_idx]
    try:
        selector = _build_button_selector(btn)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            locator = page.get_by_text(btn["text"], exact=False).first
            if await locator.count() == 0:
                log.warning("Could not find button: %s", btn["text"])
                return False
        await locator.click()
        log.info("Clicked [%d] '%s'", button_idx, btn["text"][:50])
        return True
    except Exception as e:
        log.warning("Failed to click [%d]: %s", button_idx, e)
        return False


async def _execute_upload(page: Page, fields: list, field_idx: int, file_path: str) -> bool:
    """Upload a file to a file input."""
    if field_idx >= len(fields):
        return False
    field = fields[field_idx]
    try:
        selector = _build_selector(field)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            locator = page.locator("input[type='file']").first
        await locator.set_input_files(file_path)
        log.info("Uploaded file to [%d]", field_idx)
        return True
    except Exception as e:
        log.warning("Failed to upload [%d]: %s", field_idx, e)
        return False


def _build_selector(field: dict) -> str:
    """Build a CSS selector to find a specific form field."""
    tag = field.get("tag", "input")
    if field.get("id"):
        return f"#{field['id']}"
    if field.get("name"):
        return f"{tag}[name='{field['name']}']"
    if field.get("aria_label"):
        return f"{tag}[aria-label='{field['aria_label']}']"
    if field.get("placeholder"):
        return f"{tag}[placeholder='{field['placeholder']}']"
    return f"{tag}[type='{field.get('type', 'text')}']"


def _build_button_selector(btn: dict) -> str:
    """Build a CSS selector to find a specific button."""
    if btn.get("id"):
        return f"#{btn['id']}"
    if btn.get("aria_label"):
        return f"[aria-label='{btn['aria_label']}']"
    tag = btn.get("tag", "button")
    text = btn.get("text", "")
    if text:
        return f"{tag}:has-text('{text[:40]}')"
    return tag


# ─── Main Agent Loop ─────────────────────────────────────────────────────────

async def run_agent(
    page: Page,
    goal: str,
    personal_info: dict,
    resume_text: str,
    job_description: str = "",
    max_steps: int = 15,
    on_step: callable = None,
) -> dict:
    """
    Run the vision AI agent loop on a page.

    Each step:
    1. Takes a screenshot (AI sees the screen)
    2. Extracts DOM state (for precise action targeting)
    3. Sends both to the AI
    4. Executes returned actions
    5. Repeats until done

    Returns: {"success": bool, "message": str, "steps": int}
    """
    resume_path = str(Config.RESUME_PATH) if Config.RESUME_PATH.exists() else ""

    for step in range(1, max_steps + 1):
        log.info("AI Agent step %d/%d", step, max_steps)

        # 1. Let page settle, then capture what the AI sees
        await page.wait_for_timeout(1500)
        screenshot_b64 = await take_screenshot(page)
        dom_state = await extract_dom_state(page)

        # 2. Ask AI what to do (sends screenshot + DOM state)
        decision = ask_ai_with_vision(
            goal=goal,
            screenshot_b64=screenshot_b64,
            dom_state=dom_state,
            personal_info=personal_info,
            resume_text=resume_text,
            job_description=job_description,
        )

        thinking = decision.get("thinking", "")
        actions = decision.get("actions", [])
        status = decision.get("status", "continue")

        if on_step:
            on_step(step, thinking)

        log.info("AI sees: %s", thinking[:200])
        log.info("AI status: %s, actions: %d", status, len(actions))

        # 3. Check terminal states
        if status == "done":
            return {"success": True, "message": thinking, "steps": step}
        if status == "stuck":
            return {"success": False, "message": f"AI got stuck: {thinking}", "steps": step}
        if status == "needs_human":
            return {"success": False, "message": f"Needs human help: {thinking}", "steps": step, "needs_human": True}

        # 4. Execute actions
        fields = dom_state.get("fields", [])
        buttons = dom_state.get("buttons", [])

        for action in actions:
            atype = action.get("type", "")

            if atype == "fill":
                await _execute_fill(page, fields, action.get("field_idx", 0), action.get("value", ""))
                await page.wait_for_timeout(300)
            elif atype == "select":
                await _execute_select(page, fields, action.get("field_idx", 0), action.get("value", ""))
                await page.wait_for_timeout(300)
            elif atype == "click":
                await _execute_click(page, buttons, action.get("button_idx", 0))
                await page.wait_for_timeout(1500)
            elif atype == "upload":
                if resume_path:
                    await _execute_upload(page, fields, action.get("field_idx", 0), resume_path)
                    await page.wait_for_timeout(1000)
                else:
                    log.warning("Upload requested but no resume file found")
            elif atype == "wait":
                secs = min(action.get("seconds", 2), 10)
                await page.wait_for_timeout(int(secs * 1000))
            else:
                log.warning("Unknown action type: %s", atype)

    return {"success": False, "message": f"Exceeded {max_steps} steps", "steps": max_steps}
