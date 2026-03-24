"""
cli.py — Interactive terminal interface for the Job Auto-Apply bot.

Commands (enter at the prompt):
  apply <url>    — Full pipeline: scrape JD → tailor resume → apply → log
  fill <url>     — Just fill the form (custom portal), don't auto-submit
  tailor <url>   — Only tailor resume for this JD (no apply)
  cover <url>    — Generate a cover letter
  stats          — Show application statistics
  login          — Login to LinkedIn
  setup          — Review / update your personal info & resume
  help           — Show all commands
  quit / exit    — Exit the bot
"""
import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from config import Config
from scraper import scrape_job_description
from resume_tailor import tailor_resume, extract_keywords, generate_cover_letter
from sheets_tracker import log_application, get_stats
from auto_apply import get_applier, shutdown_applier

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cli")

URL_PATTERN = re.compile(r"https?://\S+")

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
BOLD = "\033[1m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
DIM = "\033[2m"
RESET = "\033[0m"


def _banner():
    print(f"""
{CYAN}{BOLD}╔══════════════════════════════════════════════════════╗
║           🤖  Job Auto-Apply Bot  (CLI)              ║
╚══════════════════════════════════════════════════════╝{RESET}
{DIM}Type 'help' to see available commands.{RESET}
""")


def _print_help():
    print(f"""
{BOLD}Available Commands:{RESET}
  {GREEN}apply <url>{RESET}    — Full auto-apply pipeline (scrape → tailor → apply → log)
  {GREEN}fill <url>{RESET}     — Fill form only (you review & submit manually)
  {GREEN}tailor <url>{RESET}   — Get a tailored resume for this JD
  {GREEN}cover <url>{RESET}    — Generate a cover letter
  {GREEN}stats{RESET}          — Show application tracker stats
  {GREEN}login{RESET}          — Login to LinkedIn
  {GREEN}setup{RESET}          — Review / update your personal info & resume path
  {GREEN}help{RESET}           — Show this message
  {GREEN}quit / exit{RESET}    — Exit the bot

{DIM}You can also just paste a URL and you'll be asked what to do with it.{RESET}
""")


def _extract_url(text: str) -> str | None:
    """Pull the first URL from user input."""
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else None


# ─── Setup / Personal Info ────────────────────────────────────────────────────

def _show_current_config():
    """Display current personal info and resume path."""
    info = Config.personal_info()
    print(f"\n{BOLD}📋 Current Personal Info:{RESET}")
    print(f"  Name:     {info.get('name') or f'{RED}(not set){RESET}'}")
    print(f"  Email:    {info.get('email') or f'{RED}(not set){RESET}'}")
    print(f"  Phone:    {info.get('phone') or f'{RED}(not set){RESET}'}")
    print(f"  Location: {info.get('location') or f'{RED}(not set){RESET}'}")
    print(f"\n{BOLD}📄 Resume:{RESET}")
    resume_path = Config.RESUME_PATH
    if resume_path.exists():
        size_kb = resume_path.stat().st_size / 1024
        print(f"  PDF:  {GREEN}{resume_path}{RESET} ({size_kb:.1f} KB)")
    else:
        print(f"  PDF:  {RED}{resume_path} (NOT FOUND){RESET}")
    base_txt = Config.BASE_RESUME_TEXT
    if base_txt.exists():
        lines = len(base_txt.read_text().splitlines())
        print(f"  Text: {GREEN}{base_txt}{RESET} ({lines} lines)")
    else:
        print(f"  Text: {RED}{base_txt} (NOT FOUND){RESET}")
    print()


def _run_setup():
    """Interactive setup to update personal info and resume."""
    print(f"\n{BOLD}{CYAN}═══ Setup Wizard ═══{RESET}\n")
    _show_current_config()

    print(f"{DIM}Press Enter to keep current value. Type a new value to update.{RESET}\n")

    env_path = Path(".env")
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    def _update_env(key: str, value: str):
        """Update or add a key in the .env file."""
        nonlocal env_lines
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
                env_lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            env_lines.append(f"{key}={value}")

    # Name
    current = Config.YOUR_NAME
    new_val = input(f"  Your Full Name [{current or 'not set'}]: ").strip()
    if new_val:
        _update_env("YOUR_NAME", new_val)

    # Email
    current = Config.YOUR_EMAIL
    new_val = input(f"  Email [{current or 'not set'}]: ").strip()
    if new_val:
        _update_env("YOUR_EMAIL", new_val)

    # Phone
    current = Config.YOUR_PHONE
    new_val = input(f"  Phone [{current or 'not set'}]: ").strip()
    if new_val:
        _update_env("YOUR_PHONE", new_val)

    # Location
    current = Config.YOUR_LOCATION
    new_val = input(f"  Location [{current or 'not set'}]: ").strip()
    if new_val:
        _update_env("YOUR_LOCATION", new_val)

    # Resume PDF path
    current = str(Config.RESUME_PATH)
    new_val = input(f"  Resume PDF path [{current}]: ").strip()
    if new_val:
        new_path = Path(new_val).expanduser().resolve()
        if new_path.exists():
            _update_env("RESUME_PATH", str(new_path))
            print(f"  {GREEN}✓ Resume found: {new_path}{RESET}")
        else:
            print(f"  {RED}✗ File not found: {new_path} — keeping current value{RESET}")

    # Base resume text path
    current = str(Config.BASE_RESUME_TEXT)
    new_val = input(f"  Base resume text path [{current}]: ").strip()
    if new_val:
        new_path = Path(new_val).expanduser().resolve()
        if new_path.exists():
            _update_env("BASE_RESUME_TEXT_PATH", str(new_path))
            print(f"  {GREEN}✓ Base resume text found: {new_path}{RESET}")
        else:
            print(f"  {RED}✗ File not found: {new_path} — keeping current value{RESET}")

    # LinkedIn credentials
    print(f"\n{BOLD}🔐 LinkedIn (optional):{RESET}")
    current = Config.LINKEDIN_EMAIL
    new_val = input(f"  LinkedIn Email [{current or 'not set'}]: ").strip()
    if new_val:
        _update_env("LINKEDIN_EMAIL", new_val)

    current = Config.LINKEDIN_PASSWORD
    masked = "****" if current else "not set"
    new_val = input(f"  LinkedIn Password [{masked}]: ").strip()
    if new_val:
        _update_env("LINKEDIN_PASSWORD", new_val)

    # Save .env
    env_path.write_text("\n".join(env_lines) + "\n")
    print(f"\n{GREEN}✅ Settings saved to .env{RESET}")
    print(f"{YELLOW}⚠  Restart the bot for changes to take effect.{RESET}\n")


# ─── Command Implementations ─────────────────────────────────────────────────

async def do_login():
    """Log into LinkedIn."""
    print(f"{CYAN}🔐 Logging into LinkedIn...{RESET}")
    applier = await get_applier()
    success = await applier.linkedin_login()
    if success:
        print(f"{GREEN}✅ LinkedIn login successful! Cookies saved for future sessions.{RESET}")
    else:
        print(f"{RED}❌ Login failed. Check browser window for security challenge.{RESET}")


async def do_apply(url: str):
    """Full pipeline: scrape → tailor → apply → log."""
    print(f"\n{CYAN}⏳ Starting full apply pipeline...{RESET}")

    # Step 1: Scrape JD
    print(f"{YELLOW}📄 Step 1/4: Scraping job description...{RESET}")
    job = await scrape_job_description(url)
    if not job["description"]:
        print(f"{RED}❌ Couldn't extract job description. Try 'fill' instead for manual mode.{RESET}")
        return

    title = job.get("title") or "Unknown Role"
    company = job.get("company") or ""
    header = f"{BOLD}{title}{RESET}" + (f" at {BOLD}{company}{RESET}" if company else "")
    print(f"  Found: {header}")

    # Step 2: Tailor resume
    print(f"{YELLOW}🧠 Step 2/4: Tailoring resume...{RESET}")
    tailored = tailor_resume(job["description"])
    keywords = extract_keywords(job["description"])

    # Step 3: Apply
    print(f"{YELLOW}🚀 Step 3/4: Applying...{RESET}")
    applier = await get_applier()

    if "linkedin.com" in url:
        result = await applier.linkedin_easy_apply(url)
    else:
        result = await applier.generic_apply(url)

    # Step 4: Log to Sheets
    status = "Applied" if result["success"] else "Failed"
    if result.get("needs_review"):
        status = "Pending Review"

    log_application(
        company=company or "Unknown",
        role=title or "Unknown",
        url=url,
        status=status,
        keywords=keywords,
        notes=result["message"],
    )

    # Report back
    emoji = f"{GREEN}✅" if result["success"] else f"{RED}❌"
    print(f"""
{emoji} Application Result{RESET}
  📋 {title}{f' at {company}' if company else ''}
  🔗 {url}
  📊 Status: {status}
  💬 {result['message']}
  🏷  Keywords: {', '.join(keywords[:5])}
""")

    if len(tailored) > 100:
        print(f"{BOLD}📝 Tailored Resume Preview:{RESET}")
        print(f"{DIM}{tailored[:3000]}{RESET}\n")


async def do_fill(url: str):
    """Fill a form without submitting."""
    print(f"{CYAN}⏳ Opening form and filling fields...{RESET}")
    applier = await get_applier()
    result = await applier.generic_apply(url)
    emoji = f"{GREEN}✅" if result["success"] else f"{RED}❌"
    print(f"{emoji} {result['message']}{RESET}")
    print(f"{YELLOW}Browser window is open — review and submit manually.{RESET}\n")


async def do_tailor(url: str):
    """Tailor resume only — no apply."""
    print(f"{CYAN}📄 Scraping job description...{RESET}")
    job = await scrape_job_description(url)
    if not job["description"]:
        print(f"{RED}❌ Couldn't extract job description from that URL.{RESET}")
        return

    print(f"{CYAN}🧠 Tailoring your resume...{RESET}")
    tailored = tailor_resume(job["description"])
    keywords = extract_keywords(job["description"])

    title = job.get("title") or "Job"
    company = job.get("company") or ""
    header = title + (f" at {company}" if company else "")

    print(f"""
{GREEN}✅ Resume tailored for {header}{RESET}
🏷  Keywords: {', '.join(keywords[:8])}

{BOLD}📝 Tailored Resume:{RESET}
{tailored[:3500]}
""")


async def do_cover(url: str):
    """Generate cover letter for a job URL."""
    print(f"{CYAN}📄 Scraping job description...{RESET}")
    job = await scrape_job_description(url)
    if not job["description"]:
        print(f"{RED}❌ Couldn't extract job description.{RESET}")
        return

    print(f"{CYAN}✍️  Writing cover letter...{RESET}")
    letter = generate_cover_letter(job["description"], job.get("company", ""))
    print(f"\n{BOLD}✉️  Cover Letter:{RESET}\n{letter}\n")


def do_stats():
    """Show application statistics from Google Sheets."""
    stats = get_stats()
    if stats["total"] == 0:
        print(f"{YELLOW}📊 No applications logged yet. Use 'apply' to get started!{RESET}")
        return

    print(f"\n{BOLD}📊 Application Stats{RESET}")
    print(f"  Total: {BOLD}{stats['total']}{RESET}")
    for status, count in sorted(stats["by_status"].items()):
        print(f"  • {status}: {count}")
    print()


# ─── URL Action Chooser ──────────────────────────────────────────────────────

async def handle_url(url: str):
    """When user pastes a bare URL, ask what to do."""
    print(f"\n{BOLD}What would you like to do with this job?{RESET}")
    print(f"  {GREEN}1{RESET}) 🚀 Full Apply")
    print(f"  {GREEN}2{RESET}) 📝 Fill Only")
    print(f"  {GREEN}3{RESET}) 🧠 Tailor Resume")
    print(f"  {GREEN}4{RESET}) ✉️  Cover Letter")
    print(f"  {GREEN}5{RESET}) Cancel")

    choice = input(f"\n{CYAN}Choose [1-5]: {RESET}").strip()

    if choice == "1":
        await do_apply(url)
    elif choice == "2":
        await do_fill(url)
    elif choice == "3":
        await do_tailor(url)
    elif choice == "4":
        await do_cover(url)
    else:
        print("Cancelled.\n")


# ─── Pre-flight Check ────────────────────────────────────────────────────────

def _ensure_env_file():
    """Create .env from .env.example if it doesn't exist."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            import shutil
            shutil.copy(example, env_path)
            print(f"{YELLOW}📝 Created .env from .env.example{RESET}\n")
        else:
            env_path.touch()


def _update_env_value(key: str, value: str):
    """Update or add a key=value in the .env file."""
    env_path = Path(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def _preflight_check():
    """Check essential config and interactively ask for missing items."""
    _ensure_env_file()
    needs_restart = False

    # ── Resume PDF ──
    if not Config.RESUME_PATH.exists():
        print(f"{YELLOW}📄 Resume PDF not found at: {Config.RESUME_PATH}{RESET}")
        path_input = input(f"{CYAN}  Enter the full path to your resume PDF: {RESET}").strip()
        if path_input:
            resolved = Path(path_input).expanduser().resolve()
            if resolved.exists() and resolved.suffix.lower() == ".pdf":
                _update_env_value("RESUME_PATH", str(resolved))
                Config.RESUME_PATH = resolved
                print(f"  {GREEN}✓ Resume found: {resolved}{RESET}\n")
            else:
                print(f"  {RED}✗ File not found or not a PDF: {resolved}{RESET}")
                print(f"  {DIM}You can set it later with 'setup' or by editing .env{RESET}\n")
                needs_restart = True
        else:
            print(f"  {DIM}Skipped — set it later with 'setup'{RESET}\n")

    # ── Base resume text ──
    if not Config.BASE_RESUME_TEXT.exists():
        print(f"{YELLOW}📝 Base resume text not found at: {Config.BASE_RESUME_TEXT}{RESET}")
        print(f"  {DIM}This is a plain-text version of your resume used by AI for tailoring.{RESET}")
        path_input = input(f"{CYAN}  Enter path to a .txt resume file (or press Enter to create one): {RESET}").strip()
        if path_input:
            resolved = Path(path_input).expanduser().resolve()
            if resolved.exists():
                _update_env_value("BASE_RESUME_TEXT_PATH", str(resolved))
                Config.BASE_RESUME_TEXT = resolved
                print(f"  {GREEN}✓ Base resume text found: {resolved}{RESET}\n")
            else:
                print(f"  {RED}✗ File not found: {resolved}{RESET}\n")
        else:
            # Create a template for them
            Config.BASE_RESUME_TEXT.write_text(
                "YOUR FULL NAME\n"
                "City, Country | email@example.com | +91-XXXXXXXXXX\n\n"
                "SUMMARY\n"
                "[Write 2-3 sentences about your experience]\n\n"
                "EXPERIENCE\n"
                "[Your work experience here]\n\n"
                "SKILLS\n"
                "[Your skills here]\n\n"
                "EDUCATION\n"
                "[Your education here]\n"
            )
            print(f"  {GREEN}✓ Created template at {Config.BASE_RESUME_TEXT}{RESET}")
            print(f"  {YELLOW}⚠  Edit this file with your real resume content!{RESET}\n")

    # ── Personal info ──
    missing_info = []
    if not Config.YOUR_NAME:
        missing_info.append(("YOUR_NAME", "Your Full Name"))
    if not Config.YOUR_EMAIL:
        missing_info.append(("YOUR_EMAIL", "Your Email"))
    if not Config.YOUR_PHONE:
        missing_info.append(("YOUR_PHONE", "Your Phone"))
    if not Config.YOUR_LOCATION:
        missing_info.append(("YOUR_LOCATION", "Your Location (e.g. Bengaluru, India)"))

    if missing_info:
        print(f"{YELLOW}👤 Personal info is needed for auto-filling applications:{RESET}")
        for env_key, prompt_label in missing_info:
            val = input(f"{CYAN}  {prompt_label}: {RESET}").strip()
            if val:
                _update_env_value(env_key, val)
                setattr(Config, env_key, val)
                needs_restart = True
        if needs_restart:
            # Reload personal_info dict
            Config.YOUR_NAME = Config.YOUR_NAME  # already set via setattr
        print()

    # ── API key check (non-interactive, just warn) ──
    if not Config.QWEN_API_KEY:
        print(f"{YELLOW}⚠  QWEN_API_KEY is not set in .env (needed for resume tailoring){RESET}")
        print(f"{DIM}  Get one at https://dashscope.console.aliyun.com/{RESET}\n")

    # ── Show summary ──
    info = Config.personal_info()
    resume_ok = f"{GREEN}✓{RESET}" if Config.RESUME_PATH.exists() else f"{RED}✗{RESET}"
    print(f"{DIM}─── Current Config ───{RESET}")
    print(f"  {resume_ok} Resume: {Config.RESUME_PATH}")
    print(f"  👤 {info.get('name') or '(not set)'} | {info.get('email') or '(not set)'} | {info.get('phone') or '(not set)'}")
    print(f"  📍 {info.get('location') or '(not set)'}")
    print()


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def main_async():
    _banner()
    _preflight_check()

    while True:
        try:
            raw = input(f"{CYAN}{BOLD}job-bot ❯ {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Goodbye!{RESET}")
            break

        if not raw:
            continue

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            print(f"{DIM}Shutting down...{RESET}")
            break

        elif cmd == "help":
            _print_help()

        elif cmd == "setup":
            _run_setup()

        elif cmd == "login":
            await do_login()

        elif cmd == "stats":
            do_stats()

        elif cmd == "apply":
            url = _extract_url(arg)
            if not url:
                url = input(f"{CYAN}  Enter job URL: {RESET}").strip()
            if url and URL_PATTERN.match(url):
                await do_apply(url)
            else:
                print(f"{RED}Invalid URL. Usage: apply https://job-url-here{RESET}\n")

        elif cmd == "fill":
            url = _extract_url(arg)
            if not url:
                url = input(f"{CYAN}  Enter application form URL: {RESET}").strip()
            if url and URL_PATTERN.match(url):
                await do_fill(url)
            else:
                print(f"{RED}Invalid URL. Usage: fill https://application-form-url{RESET}\n")

        elif cmd == "tailor":
            url = _extract_url(arg)
            if not url:
                url = input(f"{CYAN}  Enter job URL: {RESET}").strip()
            if url and URL_PATTERN.match(url):
                await do_tailor(url)
            else:
                print(f"{RED}Invalid URL. Usage: tailor https://job-url-here{RESET}\n")

        elif cmd == "cover":
            url = _extract_url(arg)
            if not url:
                url = input(f"{CYAN}  Enter job URL: {RESET}").strip()
            if url and URL_PATTERN.match(url):
                await do_cover(url)
            else:
                print(f"{RED}Invalid URL. Usage: cover https://job-url-here{RESET}\n")

        else:
            # Check if it's a bare URL
            url = _extract_url(raw)
            if url:
                await handle_url(url)
            else:
                print(f"{DIM}Unknown command: '{cmd}'. Type 'help' for available commands.{RESET}\n")

    await shutdown_applier()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
