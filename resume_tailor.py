"""
resume_tailor.py — Uses LLM API (OpenAI-compatible) to tailor resume text to a job description.
Supports NVIDIA NIM, DashScope, OpenAI, or any compatible endpoint.
"""
import logging
from openai import OpenAI
from config import Config

log = logging.getLogger(__name__)

client = OpenAI(
    api_key=Config.LLM_API_KEY,
    base_url=Config.LLM_BASE_URL,
)

SYSTEM_PROMPT = """You are a resume optimization expert. Given a base resume and a job description,
produce a TAILORED version of the resume that:
1. Reorders bullet points to prioritize relevant experience
2. Weaves in keywords from the job description naturally
3. Quantifies achievements where possible
4. Keeps it truthful — never fabricate experience
5. Outputs clean plain text, ready to paste

Be concise. No commentary — just output the tailored resume."""


def load_base_resume() -> str:
    """Load the user's base resume text from disk."""
    try:
        return Config.BASE_RESUME_TEXT.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("Base resume text file not found at %s", Config.BASE_RESUME_TEXT)
        return ""


def tailor_resume(job_description: str, base_resume: str | None = None) -> str:
    """
    Send base resume + JD to Qwen, get back a tailored resume.
    Returns the tailored text, or the original if API fails.
    """
    if not base_resume:
        base_resume = load_base_resume()

    if not base_resume:
        return "(No base resume found — please create base_resume.txt)"

    user_msg = f"""## BASE RESUME
{base_resume}

## JOB DESCRIPTION
{job_description}

Produce the tailored resume now."""

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
            max_tokens=2000,
        )
        tailored = resp.choices[0].message.content.strip()
        log.info("Resume tailored successfully (%d chars)", len(tailored))
        return tailored
    except Exception as e:
        log.error("Qwen API error: %s", e)
        return f"(Tailoring failed: {e})\n\nOriginal resume:\n{base_resume}"


def extract_keywords(job_description: str) -> list[str]:
    """Extract key skills/keywords from a JD for quick matching."""
    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": "Extract the top 10 technical skills and keywords from this job description. Return ONLY a comma-separated list, nothing else."},
                {"role": "user", "content": job_description},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        # Handle both comma-separated and newline-separated responses
        if "," in raw:
            keywords = raw.split(",")
        else:
            keywords = raw.split("\n")
        return [kw.strip().lstrip("•-0123456789. ") for kw in keywords if kw.strip()]
    except Exception as e:
        log.error("Keyword extraction failed: %s", e)
        return []


def generate_cover_letter(job_description: str, company_name: str = "") -> str:
    """Generate a short, punchy cover letter paragraph."""
    base_resume = load_base_resume()
    prompt = f"""Write a 3-4 sentence cover letter paragraph for this role{f' at {company_name}' if company_name else ''}.
Be specific, not generic. Match my experience to their needs.

My resume: {base_resume[:1500]}

Job description: {job_description[:1500]}

Output ONLY the paragraph, no greeting/sign-off."""

    try:
        resp = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return ""
