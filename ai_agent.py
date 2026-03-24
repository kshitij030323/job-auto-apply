"""
ai_agent.py — AI-powered browser automation agent.

Instead of brittle CSS selectors and hardcoded field maps, this module:
1. Extracts the visible page state (forms, fields, buttons, text)
2. Sends it to the LLM (Qwen 3.5 via NVIDIA NIM)
3. Gets back structured JSON actions (fill, click, select, upload, etc.)
4. Executes those actions via Playwright
5. Loops until the goal is achieved or max steps reached

The AI sees what a human sees and decides what to do.
"""
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


# ─── Page State Extraction ───────────────────────────────────────────────────

# JavaScript that runs in the browser to extract visible page state
_EXTRACT_PAGE_STATE_JS = """
() => {
    const state = { url: location.href, title: document.title, fields: [], buttons: [], page_text: "" };

    // Extract visible form fields
    const inputs = document.querySelectorAll(
        'input, textarea, select'
    );
    let fieldIdx = 0;
    inputs.forEach(el => {
        if (el.offsetParent === null && el.type !== 'file') return;  // hidden
        if (el.type === 'hidden') return;

        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0 && el.type !== 'file') return;

        // Find label text
        let label = '';
        if (el.id) {
            const labelEl = document.querySelector(`label[for='${el.id}']`);
            if (labelEl) label = labelEl.innerText.trim();
        }
        if (!label && el.closest('label')) {
            label = el.closest('label').innerText.trim();
        }
        if (!label) {
            // Check preceding sibling or parent for label-like text
            const prev = el.previousElementSibling;
            if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'DIV')) {
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

        // For select elements, get options
        if (el.tagName === 'SELECT') {
            field.options = Array.from(el.options).slice(0, 20).map(o => ({
                value: o.value,
                text: o.text.trim()
            }));
        }

        // For file inputs, get accept type
        if (el.type === 'file') {
            field.accept = el.accept || '';
        }

        state.fields.push(field);
    });

    // Extract visible buttons and clickable elements
    const btns = document.querySelectorAll(
        'button, [role="button"], input[type="submit"], a.btn, a.button, [class*="btn"]'
    );
    let btnIdx = 0;
    btns.forEach(el => {
        if (el.offsetParent === null) return;  // hidden
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
            class: (el.className || '').toString().substring(0, 100),
            aria_label: el.getAttribute('aria-label') || '',
            disabled: el.disabled || false,
        });
    });

    // Get key page text (headings, important content) — truncated
    const headings = Array.from(document.querySelectorAll('h1, h2, h3')).map(h => h.innerText.trim()).filter(Boolean);
    const mainContent = document.querySelector('main, [role="main"], .main-content, #main')
    const bodyText = mainContent ? mainContent.innerText : document.body.innerText;
    state.page_text = headings.join(' | ') + '\\n\\n' + bodyText.substring(0, 3000);

    return state;
}
"""


async def extract_page_state(page: Page) -> dict:
    """Run JS in the browser to extract the visible page state."""
    try:
        return await page.evaluate(_EXTRACT_PAGE_STATE_JS)
    except Exception as e:
        log.error("Failed to extract page state: %s", e)
        return {"url": page.url, "title": "", "fields": [], "buttons": [], "page_text": ""}


# ─── AI Decision Making ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an AI agent controlling a web browser to apply for jobs. You can see the current page state and must decide what actions to take.

You will be given:
- The current page URL, title, and text content
- All visible form fields with their labels, types, and current values
- All visible buttons
- The user's personal info (name, email, phone, location)
- The user's resume text
- The goal (e.g., "apply to this job", "fill this form", "log into LinkedIn")

Your job is to analyze what's on the page and return a JSON object with actions to take.

IMPORTANT RULES:
1. Only fill fields that are currently EMPTY (value is "")
2. Match field labels/names to the appropriate personal info
3. For dropdowns (select), pick the BEST matching option from the available options
4. For file upload fields, use action "upload" — the system will handle the file
5. If you see a "Next", "Continue", or "Submit" button and all fields are filled, click it
6. If the page shows a success message or confirmation, report status "done"
7. If you're stuck (no fields to fill, no clear next action), report status "stuck"
8. For LinkedIn Easy Apply modals, fill fields step by step and click Next/Review/Submit
9. For unknown/custom questions, use your best judgment based on the resume and job description
10. NEVER fabricate experience or qualifications — only use info from the provided resume and personal info

When you encounter questions like "years of experience", "willing to relocate", "visa status",
"salary expectations", etc. — answer based on what you can infer from the resume, or use reasonable
defaults (be honest).

Respond with ONLY a valid JSON object in this format — no markdown, no explanation:
{
    "thinking": "Brief explanation of what you see on the page and your reasoning",
    "actions": [
        {"type": "fill", "field_idx": 0, "value": "John Doe"},
        {"type": "select", "field_idx": 3, "value": "option_value"},
        {"type": "click", "button_idx": 2},
        {"type": "upload", "field_idx": 5},
        {"type": "wait", "seconds": 2}
    ],
    "status": "continue"
}

Status must be one of:
- "continue" — actions taken, need to check page again for next step
- "done" — goal achieved (application submitted, login successful, etc.)
- "stuck" — cannot proceed (no matching fields, error on page, etc.)
- "needs_human" — requires human intervention (captcha, security challenge, etc.)
"""


def _build_user_message(page_state: dict, goal: str, personal_info: dict, resume_text: str, job_description: str = "") -> str:
    """Build the user message with all context for the AI."""
    fields_desc = []
    for f in page_state.get("fields", []):
        desc = f"  [{f['idx']}] {f['tag']}({f['type']})"
        if f.get("label"):
            desc += f" label=\"{f['label']}\""
        if f.get("name"):
            desc += f" name=\"{f['name']}\""
        if f.get("placeholder"):
            desc += f" placeholder=\"{f['placeholder']}\""
        if f.get("aria_label"):
            desc += f" aria-label=\"{f['aria_label']}\""
        if f.get("value"):
            desc += f" value=\"{f['value']}\""
        if f.get("required"):
            desc += " [REQUIRED]"
        if f.get("options"):
            opts = ", ".join(f"{o['value']}={o['text']}" for o in f["options"][:10])
            desc += f" options=[{opts}]"
        if f.get("accept"):
            desc += f" accept=\"{f['accept']}\""
        fields_desc.append(desc)

    buttons_desc = []
    for b in page_state.get("buttons", []):
        desc = f"  [{b['idx']}] \"{b['text']}\""
        if b.get("aria_label"):
            desc += f" aria-label=\"{b['aria_label']}\""
        if b.get("disabled"):
            desc += " [DISABLED]"
        buttons_desc.append(desc)

    msg = f"""## GOAL
{goal}

## CURRENT PAGE
URL: {page_state.get('url', '')}
Title: {page_state.get('title', '')}

## FORM FIELDS
{chr(10).join(fields_desc) if fields_desc else "(no form fields visible)"}

## BUTTONS
{chr(10).join(buttons_desc) if buttons_desc else "(no buttons visible)"}

## PAGE CONTENT
{page_state.get('page_text', '')[:2000]}

## YOUR PERSONAL INFO
Name: {personal_info.get('name', '')}
First Name: {personal_info.get('first_name', '')}
Last Name: {personal_info.get('last_name', '')}
Email: {personal_info.get('email', '')}
Phone: {personal_info.get('phone', '')}
Location: {personal_info.get('location', '')}
City: {personal_info.get('city', '')}

## YOUR RESUME
{resume_text[:2000]}
"""

    if job_description:
        msg += f"\n## JOB DESCRIPTION\n{job_description[:1500]}\n"

    return msg


def _parse_ai_response(raw: str) -> dict:
    """Parse the AI's JSON response, handling common formatting issues."""
    # Strip markdown code blocks if present
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Some models wrap in <think> tags — strip those
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        log.error("Failed to parse AI response: %s", text[:500])
        return {"thinking": "Failed to parse response", "actions": [], "status": "stuck"}


def ask_ai(goal: str, page_state: dict, personal_info: dict, resume_text: str, job_description: str = "") -> dict:
    """Send page state to the AI and get back actions."""
    client = _get_client()
    user_msg = _build_user_message(page_state, goal, personal_info, resume_text, job_description)

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content.strip()
        log.debug("AI raw response: %s", raw[:500])
        return _parse_ai_response(raw)
    except Exception as e:
        log.error("AI API call failed: %s", e)
        return {"thinking": f"API error: {e}", "actions": [], "status": "stuck"}


# ─── Action Execution ────────────────────────────────────────────────────────

async def _execute_fill(page: Page, fields: list, field_idx: int, value: str) -> bool:
    """Fill a form field by its extracted index."""
    if field_idx >= len(fields):
        log.warning("Field index %d out of range (only %d fields)", field_idx, len(fields))
        return False

    field = fields[field_idx]
    try:
        # Build a selector to find this specific element
        selector = _build_selector(field)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            log.warning("Could not find field: %s", selector)
            return False

        await locator.click()
        await locator.fill(value)
        log.info("Filled field [%d] '%s' with '%s'",
                 field_idx, field.get("label") or field.get("name") or field.get("placeholder"), value[:30])
        return True
    except Exception as e:
        log.warning("Failed to fill field [%d]: %s", field_idx, e)
        return False


async def _execute_select(page: Page, fields: list, field_idx: int, value: str) -> bool:
    """Select an option in a dropdown by its extracted index."""
    if field_idx >= len(fields):
        return False

    field = fields[field_idx]
    try:
        selector = _build_selector(field)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return False

        # Try selecting by value first, then by label
        try:
            await locator.select_option(value=value)
        except Exception:
            await locator.select_option(label=value)
        log.info("Selected [%d] '%s' = '%s'",
                 field_idx, field.get("label") or field.get("name"), value)
        return True
    except Exception as e:
        log.warning("Failed to select [%d]: %s", field_idx, e)
        return False


async def _execute_click(page: Page, buttons: list, button_idx: int) -> bool:
    """Click a button by its extracted index."""
    if button_idx >= len(buttons):
        log.warning("Button index %d out of range (only %d buttons)", button_idx, len(buttons))
        return False

    btn = buttons[button_idx]
    try:
        selector = _build_button_selector(btn)
        locator = page.locator(selector).first
        if await locator.count() == 0:
            # Fallback: try by text
            locator = page.get_by_text(btn["text"], exact=False).first
            if await locator.count() == 0:
                log.warning("Could not find button: %s", btn["text"])
                return False

        await locator.click()
        log.info("Clicked button [%d] '%s'", button_idx, btn["text"][:50])
        return True
    except Exception as e:
        log.warning("Failed to click button [%d]: %s", button_idx, e)
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
            # Fallback for file inputs (often hidden)
            locator = page.locator("input[type='file']").first
        await locator.set_input_files(file_path)
        log.info("Uploaded file to field [%d]", field_idx)
        return True
    except Exception as e:
        log.warning("Failed to upload to field [%d]: %s", field_idx, e)
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
    # Last resort: by type
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
    Run the AI agent loop on a page.

    Args:
        page: Playwright page to control
        goal: What the agent should accomplish
        personal_info: User's personal info dict
        resume_text: Plain text resume
        job_description: JD text (for context)
        max_steps: Maximum number of AI reasoning steps
        on_step: Optional callback(step_num, thinking_text) for UI updates

    Returns: {"success": bool, "message": str, "steps": int}
    """
    resume_path = str(Config.RESUME_PATH) if Config.RESUME_PATH.exists() else ""

    for step in range(1, max_steps + 1):
        log.info("AI Agent step %d/%d", step, max_steps)

        # 1. Extract what's on the page
        await page.wait_for_timeout(1500)  # let page settle
        page_state = await extract_page_state(page)

        # 2. Ask AI what to do
        decision = ask_ai(goal, page_state, personal_info, resume_text, job_description)

        thinking = decision.get("thinking", "")
        actions = decision.get("actions", [])
        status = decision.get("status", "continue")

        if on_step:
            on_step(step, thinking)

        log.info("AI thinks: %s", thinking[:200])
        log.info("AI status: %s, actions: %d", status, len(actions))

        # 3. Check if we're done
        if status == "done":
            return {"success": True, "message": thinking, "steps": step}
        if status == "stuck":
            return {"success": False, "message": f"AI got stuck: {thinking}", "steps": step}
        if status == "needs_human":
            return {"success": False, "message": f"Needs human help: {thinking}", "steps": step, "needs_human": True}

        # 4. Execute the actions
        fields = page_state.get("fields", [])
        buttons = page_state.get("buttons", [])

        for action in actions:
            action_type = action.get("type", "")

            if action_type == "fill":
                await _execute_fill(page, fields, action.get("field_idx", 0), action.get("value", ""))
                await page.wait_for_timeout(300)

            elif action_type == "select":
                await _execute_select(page, fields, action.get("field_idx", 0), action.get("value", ""))
                await page.wait_for_timeout(300)

            elif action_type == "click":
                await _execute_click(page, buttons, action.get("button_idx", 0))
                await page.wait_for_timeout(1500)  # wait for navigation/modal

            elif action_type == "upload":
                if resume_path:
                    await _execute_upload(page, fields, action.get("field_idx", 0), resume_path)
                    await page.wait_for_timeout(1000)
                else:
                    log.warning("Upload requested but no resume file found")

            elif action_type == "wait":
                secs = min(action.get("seconds", 2), 10)
                await page.wait_for_timeout(int(secs * 1000))

            else:
                log.warning("Unknown action type: %s", action_type)

    return {"success": False, "message": f"Exceeded {max_steps} steps without completion", "steps": max_steps}
