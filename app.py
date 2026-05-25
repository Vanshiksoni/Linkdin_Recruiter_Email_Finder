from __future__ import annotations

import csv
import mimetypes
import os
import re
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import Flask, Response, render_template_string, request
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions


APP_DIR = Path(__file__).parent
load_dotenv(APP_DIR / ".env")

LOG_DIR = APP_DIR / "linkedin_logs"
LOG_DIR.mkdir(exist_ok=True)

BROWSER_NAME = os.getenv("BROWSER", "chrome").strip().lower()
PROFILE_DIRECTORY = os.getenv("PROFILE_DIRECTORY", "Default").strip() or "Default"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([a-zA-Z0-9._%+-]+)\s*"
    r"(?:@|\[\s*at\s*\]|\(\s*at\s*\)|\s+at\s+)"
    r"\s*([a-zA-Z0-9-]+(?:\s*(?:\.|\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*[a-zA-Z0-9-]+)+)\b",
    re.IGNORECASE,
)

app = Flask(__name__)
LINKEDIN_DRIVER = None


@dataclass
class Candidate:
    full_name: str
    gmail_id: str
    phone: str
    resume_link: str
    skills_summary: str
    application_paragraph: str


@dataclass
class RecruiterLead:
    keyword: str
    recruiter_email: str
    post_text: str
    post_url: str
    status: str
    gmail_compose_url: str


@dataclass
class EmailSendResult:
    ok: bool
    message: str


def env_value(name: str) -> str:
    return os.getenv(name, "").strip()


def default_candidate() -> Candidate:
    return Candidate(
        full_name=env_value("CANDIDATE_NAME"),
        gmail_id=env_value("GMAIL_ID"),
        phone=env_value("PHONE"),
        resume_link=env_value("RESUME_LINK"),
        skills_summary=env_value("SKILLS_SUMMARY") or "Java, Spring Boot, REST APIs, SQL, problem solving, clean code, teamwork",
        application_paragraph=env_value("APPLICATION_PARAGRAPH")
        or "I am available for contract opportunities and can join discussions immediately. My experience matches the requirements, and I would be happy to share more details about my background.",
    )


def email_setup_status() -> dict[str, str | bool]:
    resume_file = env_value("RESUME_FILE")
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    resume_exists = bool(resume_file and Path(resume_file).exists())
    can_send = bool(env_value("GMAIL_ID") and app_password and resume_exists)
    if can_send:
        message = "Ready to send email through Gmail SMTP with resume attachment."
    elif not resume_file:
        message = "Add RESUME_FILE in .env to attach the candidate resume."
    elif not resume_exists:
        message = f"Resume file path does not exist: {resume_file}"
    elif not app_password:
        message = "Add GMAIL_APP_PASSWORD in .env. Use a Gmail app password, not your normal password."
    else:
        message = "Add GMAIL_ID in .env."
    return {
        "can_send": can_send,
        "message": message,
        "resume_file": resume_file,
        "has_app_password": bool(app_password),
    }


def find_brave_binary() -> str:
    configured = os.getenv("BRAVE_BINARY", "").strip()
    candidates = [
        configured,
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        str(Path(os.getenv("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise WebDriverException("Brave browser was not found. Set BRAVE_BINARY in .env or use BROWSER=chrome.")


def browser_profile_dir() -> Path:
    configured = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
    if configured:
        return Path(configured)

    if BROWSER_NAME == "brave":
        real_brave_profile = Path(os.getenv("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data"
        if real_brave_profile.exists():
            return real_brave_profile

    return APP_DIR / f".{BROWSER_NAME}_browser_profile"


def build_driver():
    profile_dir = browser_profile_dir()
    if BROWSER_NAME in {"chrome", "brave"}:
        options = ChromeOptions()
        if BROWSER_NAME == "brave":
            options.binary_location = find_brave_binary()
            options.add_argument("--remote-allow-origins=*")
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument(f"--profile-directory={PROFILE_DIRECTORY}")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")
        options.add_argument("--disable-webrtc")
        options.add_argument("--log-level=3")
        options.add_argument("--start-maximized")
        return webdriver.Chrome(options=options)

    options = EdgeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--profile-directory={PROFILE_DIRECTORY}")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")
    options.add_argument("--disable-webrtc")
    options.add_argument("--log-level=3")
    options.add_argument("--start-maximized")
    return webdriver.Edge(options=options)


def get_linkedin_driver():
    global LINKEDIN_DRIVER
    if LINKEDIN_DRIVER is not None:
        try:
            _ = LINKEDIN_DRIVER.current_url
            return LINKEDIN_DRIVER
        except WebDriverException:
            LINKEDIN_DRIVER = None

    LINKEDIN_DRIVER = build_driver()
    return LINKEDIN_DRIVER


def close_linkedin_driver() -> None:
    global LINKEDIN_DRIVER
    if LINKEDIN_DRIVER is not None:
        try:
            LINKEDIN_DRIVER.quit()
        finally:
            LINKEDIN_DRIVER = None


def linkedin_search_url(keyword: str) -> str:
    encoded = quote_plus(keyword)
    return (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={encoded}&datePosted=%22past-24h%22&origin=FACETED_SEARCH"
    )


def is_linkedin_login_page(driver) -> bool:
    current_url = (driver.current_url or "").lower()
    if "/login" in current_url or "checkpoint" in current_url:
        return True

    body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    login_markers = ["email or phone", "sign in", "join linkedin", "forgot password"]
    return any(marker in body_text for marker in login_markers)


def ensure_linkedin_logged_in(driver, wait_seconds: int = 120) -> bool:
    driver.get("https://www.linkedin.com/feed/")
    time.sleep(4)
    if not is_linkedin_login_page(driver):
        return True

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        try:
            if not is_linkedin_login_page(driver):
                return True
        except WebDriverException:
            return False
    return False


def clean_email(email: str) -> str:
    return email.strip(".,;:()[]{}<>\"'").lower()


def normalize_obfuscated_domain(domain: str) -> str:
    normalized = re.sub(r"\s*(?:\[dot\]|\(dot\)|\bdot\b)\s*", ".", domain, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*\.\s*", ".", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip(".")


def extract_emails(text: str) -> list[str]:
    seen: set[str] = set()
    emails: list[str] = []
    for match in EMAIL_RE.findall(text):
        email = clean_email(match)
        if email not in seen:
            seen.add(email)
            emails.append(email)

    for match in OBFUSCATED_EMAIL_RE.finditer(text):
        local_part = match.group(1).strip(".,;:()[]{}<>\"'")
        domain = normalize_obfuscated_domain(match.group(2))
        email = clean_email(f"{local_part}@{domain}")
        if email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def expand_visible_linkedin_text(driver) -> None:
    for _ in range(2):
        buttons = driver.find_elements(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'see more')]")
        for button in buttons[:20]:
            try:
                driver.execute_script("arguments[0].click();", button)
                time.sleep(0.2)
            except WebDriverException:
                continue


def extract_post_text_and_emails(post) -> tuple[str, list[str]]:
    text_parts = [post.text.strip()]

    for element in post.find_elements(By.CSS_SELECTOR, "a[href^='mailto:']"):
        href = element.get_attribute("href") or ""
        text_parts.append(href.replace("mailto:", "").split("?", 1)[0])

    for element in post.find_elements(By.CSS_SELECTOR, "a[href], span, div"):
        value = (element.get_attribute("aria-label") or "").strip()
        if "@" in value or " at " in value.lower():
            text_parts.append(value)

    combined_text = "\n".join(part for part in text_parts if part)
    return combined_text, extract_emails(combined_text)


def extract_page_text_and_emails(driver) -> tuple[str, list[str]]:
    text_parts: list[str] = []
    try:
        text_parts.append(driver.find_element(By.TAG_NAME, "body").text)
    except WebDriverException:
        pass

    try:
        text_parts.append(driver.execute_script("return document.body ? document.body.innerText : '';") or "")
    except WebDriverException:
        pass

    for selector in ["a[href^='mailto:']", "a[href]", "span", "div"]:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements[:300]:
            href = (element.get_attribute("href") or "").strip()
            aria = (element.get_attribute("aria-label") or "").strip()
            title = (element.get_attribute("title") or "").strip()
            visible = (element.text or "").strip()
            for value in [href, aria, title, visible]:
                value_lower = value.lower()
                if "mailto:" in value_lower:
                    text_parts.append(value.replace("mailto:", "").split("?", 1)[0])
                elif "@" in value or " at " in value_lower or "[at]" in value_lower or "(at)" in value_lower:
                    text_parts.append(value)

    combined_text = "\n".join(part for part in text_parts if part)
    return combined_text, extract_emails(combined_text)


def should_skip_email(email: str, candidate: Candidate) -> bool:
    blocked_fragments = [
        "linkedin.com",
        "licdn.com",
        "example.com",
        "email.com",
        "domain.com",
        "yourname@gmail.com",
    ]
    email_lower = email.lower()
    if candidate.gmail_id and email_lower == candidate.gmail_id.lower():
        return True
    return any(fragment in email_lower for fragment in blocked_fragments)


def add_email_lead(
    leads: list[RecruiterLead],
    seen_emails: set[str],
    candidate: Candidate,
    keyword: str,
    email: str,
    text: str,
    post_url: str,
    status: str,
) -> None:
    email = clean_email(email)
    if not email or email in seen_emails or should_skip_email(email, candidate):
        return
    seen_emails.add(email)
    leads.append(
        RecruiterLead(
            keyword=keyword,
            recruiter_email=email,
            post_text=text[:500],
            post_url=post_url,
            status=status,
            gmail_compose_url=make_gmail_compose_url(candidate, email, keyword, post_url),
        )
    )


def make_gmail_compose_url(candidate: Candidate, email: str, keyword: str, post_url: str = "") -> str:
    subject = make_application_subject(keyword)
    body = make_application_body(candidate, keyword, post_url)
    return (
        "https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={quote_plus(email)}"
        f"&su={quote_plus(subject)}"
        f"&body={quote_plus(body)}"
    )


def make_application_subject(keyword: str) -> str:
    return f"Application for {keyword} role"


def make_application_body(candidate: Candidate, keyword: str, post_url: str = "") -> str:
    post_line = f"\nLinkedIn Post Reference: {post_url}\n" if post_url else ""
    return (
        "Dear Recruiter,\n\n"
        f"I hope you are doing well. I found your recent LinkedIn post regarding {keyword} opportunities.\n\n"
        "I am interested in applying for this role. Please find my candidate details below:\n\n"
        f"Name: {candidate.full_name}\n"
        f"Gmail ID: {candidate.gmail_id}\n"
        f"Phone: {candidate.phone}\n"
        f"Resume/Profile Link: {candidate.resume_link}\n"
        f"{post_line}\n"
        "Application Note:\n"
        f"{candidate.application_paragraph}\n\n"
        "Profile Summary:\n"
        f"{candidate.skills_summary}\n\n"
        "I have attached my resume for your review. Please let me know if any additional details are required.\n\n"
        "Regards,\n"
        f"{candidate.full_name}\n"
        f"{candidate.gmail_id}"
    )


def send_application_email(candidate: Candidate, recruiter_email: str, keyword: str, post_url: str) -> EmailSendResult:
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    resume_file = os.getenv("RESUME_FILE", "").strip()

    if not gmail_password:
        return EmailSendResult(False, "GMAIL_APP_PASSWORD is missing in .env. Use a Gmail app password, not your normal Gmail password.")

    if not resume_file:
        return EmailSendResult(False, "RESUME_FILE is missing in .env. Add the full local path of the resume PDF/DOCX.")

    resume_path = Path(resume_file)
    if not resume_path.exists():
        return EmailSendResult(False, f"Resume file not found: {resume_path}")

    message = EmailMessage()
    message["From"] = candidate.gmail_id
    message["To"] = recruiter_email
    message["Subject"] = make_application_subject(keyword)
    message.set_content(make_application_body(candidate, keyword, post_url))

    content_type, _ = mimetypes.guess_type(resume_path.name)
    if content_type:
        maintype, subtype = content_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"

    message.add_attachment(
        resume_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=resume_path.name,
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(candidate.gmail_id, gmail_password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError:
        return EmailSendResult(False, "Gmail authentication failed. Check Gmail ID and app password.")
    except OSError as exc:
        return EmailSendResult(False, f"Could not send email: {exc}")
    except smtplib.SMTPException as exc:
        return EmailSendResult(False, f"Gmail SMTP error: {exc}")

    log_path = LOG_DIR / "sent_email_log.csv"
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["sent_at", "candidate_name", "gmail_id", "recruiter_email", "keyword", "resume_file", "post_url"],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "sent_at": datetime.now().isoformat(timespec="seconds"),
                "candidate_name": candidate.full_name,
                "gmail_id": candidate.gmail_id,
                "recruiter_email": recruiter_email,
                "keyword": keyword,
                "resume_file": str(resume_path),
                "post_url": post_url,
            }
        )

    return EmailSendResult(True, f"Email sent to {recruiter_email} with resume attached.")


def open_linkedin_login_browser() -> None:
    driver = get_linkedin_driver()
    driver.get("https://www.linkedin.com/feed/")


def open_gmail_login_browser() -> None:
    os.startfile("https://mail.google.com/mail/u/0/#inbox")


def open_gmail_compose_browser(candidate: Candidate, recruiter_email: str, keyword: str, post_url: str) -> None:
    os.startfile(make_gmail_compose_url(candidate, recruiter_email, keyword, post_url))


def search_linkedin_posts(candidate: Candidate, keywords: list[str], max_posts: int) -> list[RecruiterLead]:
    leads: list[RecruiterLead] = []
    seen_emails: set[str] = set()
    driver = get_linkedin_driver()
    if not ensure_linkedin_logged_in(driver):
        return [
            RecruiterLead(
                keyword=", ".join(keywords) or "LinkedIn Search",
                recruiter_email="",
                post_text="LinkedIn login was not completed in the automation browser. Click Open LinkedIn Login, complete login or verification, keep that browser open, then search again.",
                post_url="https://www.linkedin.com/login",
                status="Login Needed",
                gmail_compose_url="",
            )
        ]

    for keyword in keywords:
        driver.get(linkedin_search_url(keyword))
        time.sleep(6)
        for _ in range(3):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

        expand_visible_linkedin_text(driver)
        page_text, page_emails = extract_page_text_and_emails(driver)
        for email in page_emails:
            add_email_lead(
                leads,
                seen_emails,
                candidate,
                keyword,
                email,
                page_text,
                linkedin_search_url(keyword),
                "Recruiter Email Found on Page",
            )

        posts = driver.find_elements(By.CSS_SELECTOR, "div.feed-shared-update-v2, li.reusable-search__result-container")
        if not posts:
            if is_linkedin_login_page(driver):
                leads.append(
                    RecruiterLead(
                        keyword=keyword,
                        recruiter_email="",
                        post_text="LinkedIn login is required. Complete login in the automation browser, keep it open, then search again.",
                        post_url=linkedin_search_url(keyword),
                        status="Login Needed",
                        gmail_compose_url="",
                    )
                )
            continue

        for post in posts[:max_posts]:
            text, emails = extract_post_text_and_emails(post)
            text = text.strip()
            if not text:
                continue
            post_url = linkedin_search_url(keyword)
            links = post.find_elements(By.CSS_SELECTOR, "a[href*='linkedin.com/feed/update'], a[href*='activity']")
            if links:
                post_url = links[0].get_attribute("href") or post_url

            if emails:
                for email in emails:
                    add_email_lead(
                        leads,
                        seen_emails,
                        candidate,
                        keyword,
                        email,
                        text,
                        post_url,
                        "Recruiter Email Found in Post",
                    )
            else:
                leads.append(
                    RecruiterLead(
                        keyword=keyword,
                        recruiter_email="",
                        post_text=text[:500],
                        post_url=post_url,
                        status="Manual Review Needed",
                        gmail_compose_url="",
                    )
                )
    return leads


def write_csv(candidate: Candidate, leads: list[RecruiterLead]) -> Path:
    path = LOG_DIR / f"linkedin_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "candidate_name",
                "gmail_id",
                "keyword",
                "recruiter_email",
                "status",
                "post_url",
                "post_text",
            ],
        )
        writer.writeheader()
        for lead in leads:
            writer.writerow(
                {
                    "candidate_name": candidate.full_name,
                    "gmail_id": candidate.gmail_id,
                    "keyword": lead.keyword,
                    "recruiter_email": lead.recruiter_email,
                    "status": lead.status,
                    "post_url": lead.post_url,
                    "post_text": lead.post_text,
                }
            )
    return path


def latest_leads_csv() -> Path | None:
    files = sorted(LOG_DIR.glob("linkedin_leads_*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
    for file in files:
        with file.open(newline="", encoding="utf-8") as handle:
            if sum(1 for _ in csv.DictReader(handle)) > 0:
                return file
    return files[0] if files else None


def read_leads_csv(path: Path, candidate: Candidate) -> list[RecruiterLead]:
    leads: list[RecruiterLead] = []
    with path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            keyword = (row.get("keyword") or "").strip()
            recruiter_email = (row.get("recruiter_email") or "").strip()
            post_url = (row.get("post_url") or "").strip()
            leads.append(
                RecruiterLead(
                    keyword=keyword,
                    recruiter_email=recruiter_email,
                    post_text=(row.get("post_text") or "").strip()[:500],
                    post_url=post_url,
                    status=(row.get("status") or "").strip(),
                    gmail_compose_url=make_gmail_compose_url(candidate, recruiter_email, keyword, post_url) if recruiter_email else "",
                )
            )
    return leads


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>API 1 LinkedIn Gmail Assistant</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #f4f6f8; color: #17202a; }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    h1 { margin: 0 0 6px; font-size: 28px; }
    p { line-height: 1.5; }
    .topbar { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 20px; }
    .panel { background: white; border: 1px solid #d9dee5; border-radius: 8px; padding: 18px; margin-bottom: 18px; }
    form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    label { display: grid; gap: 6px; font-weight: 700; }
    input, textarea { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #b8c0cc; border-radius: 6px; font: inherit; }
    textarea { min-height: 92px; resize: vertical; }
    h2 { margin: 0 0 10px; font-size: 20px; }
    .wide { grid-column: 1 / -1; }
    button, a.button { display: inline-block; border: 0; border-radius: 6px; background: #1967d2; color: white; padding: 10px 14px; text-decoration: none; font-weight: 700; cursor: pointer; }
    .secondary { background: #475569; }
    .danger { background: #b42318; }
    table { width: 100%; border-collapse: collapse; background: white; }
    th, td { border-bottom: 1px solid #e1e6ee; padding: 10px; text-align: left; vertical-align: top; }
    th { background: #eef2f7; }
    .status { font-weight: 700; }
    .muted { color: #5f6b7a; }
    .email-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
    .email-list li { display: flex; justify-content: space-between; gap: 12px; align-items: center; border: 1px solid #d9dee5; border-radius: 6px; padding: 10px; background: #f8fafc; }
    .email-value { font-weight: 700; word-break: break-all; }
    @media (max-width: 760px) { main { padding: 16px; } .topbar, form { display: block; } label { margin-bottom: 12px; } }
  </style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>LinkedIn Recruiter Email Finder</h1>
      <p class="muted">Search recent LinkedIn posts, collect public recruiter emails, prepare Gmail drafts, and send formal emails with resume attachments.</p>
    </div>
    <div>
      <a class="button secondary" href="/login-linkedin" target="_blank">Open LinkedIn Login</a>
      <a class="button secondary" href="/login-gmail" target="_blank">Open Gmail Login</a>
      <a class="button secondary" href="/latest-leads">Load Latest Leads</a>
      <a class="button danger" href="/close-linkedin" target="_blank">Close Automation Browser</a>
    </div>
  </div>

  <section class="panel">
    <strong>Gmail Sending Setup:</strong> {{ email_setup.message }}
    <p class="muted">
      Gmail login/compose opens in your system default browser. Automatic resume attachment needs
      <code>GMAIL_APP_PASSWORD</code> and <code>RESUME_FILE</code> in <code>.env</code>.
    </p>
    <p class="muted">
      If LinkedIn asks for login during search, complete login in the automation browser and keep that browser open.
      The search waits for login before scanning posts.
    </p>
  </section>

  <section class="panel">
    <form method="post" action="/search">
      <label>Full Name
        <input name="full_name" required value="{{ candidate.full_name if candidate else default_name }}">
      </label>
      <label>Gmail ID
        <input name="gmail_id" type="email" required value="{{ candidate.gmail_id if candidate else default_gmail }}">
      </label>
      <label>Phone
        <input name="phone" required value="{{ candidate.phone if candidate else default_phone }}">
      </label>
      <label>Resume Link
        <input name="resume_link" required value="{{ candidate.resume_link if candidate else default_resume }}">
      </label>
      <label class="wide">Keywords, comma separated
        <input name="keywords" required value="{{ keywords or 'Java Developer contract email, send resume Java Developer, .NET Developer contract email' }}">
      </label>
      <label class="wide">Skills Summary
        <textarea name="skills_summary" required>{{ candidate.skills_summary if candidate else 'Java, Spring Boot, REST APIs, SQL, problem solving, clean code, teamwork' }}</textarea>
      </label>
      <label class="wide">Application Paragraph
        <textarea name="application_paragraph" required>{{ candidate.application_paragraph if candidate else default_paragraph }}</textarea>
      </label>
      <label>Posts per keyword
        <input name="max_posts" type="number" min="1" max="20" value="{{ max_posts or 8 }}">
      </label>
      <div style="align-self:end">
        <button type="submit">Search LinkedIn Posts</button>
      </div>
    </form>
  </section>

  <section class="panel">
    <h2>Manual Recruiter Email</h2>
    <p class="muted">Use this when you already know the recruiter email or want to test Gmail compose/send before LinkedIn finds one.</p>
    <form method="post" action="/manual-email">
      <input type="hidden" name="full_name" value="{{ candidate.full_name if candidate else default_name }}">
      <input type="hidden" name="gmail_id" value="{{ candidate.gmail_id if candidate else default_gmail }}">
      <input type="hidden" name="phone" value="{{ candidate.phone if candidate else default_phone }}">
      <input type="hidden" name="resume_link" value="{{ candidate.resume_link if candidate else default_resume }}">
      <input type="hidden" name="skills_summary" value="{{ candidate.skills_summary if candidate else default_skills }}">
      <input type="hidden" name="application_paragraph" value="{{ candidate.application_paragraph if candidate else default_paragraph }}">
      <label>Recruiter Email
        <input name="recruiter_email" type="email" required placeholder="recruiter@company.com">
      </label>
      <label>Job Keyword / Role
        <input name="keyword" required value="Java Developer contract">
      </label>
      <label class="wide">LinkedIn Post URL
        <input name="post_url" placeholder="Optional LinkedIn post link">
      </label>
      <div style="align-self:end">
        <button type="submit">Prepare Email</button>
      </div>
    </form>
  </section>

  {% if error %}
    <section class="panel"><strong>Error:</strong> {{ error }}</section>
  {% endif %}

  {% if csv_file %}
    <section class="panel">CSV proof log created: <strong>{{ csv_file }}</strong></section>
  {% endif %}

  {% if send_message %}
    <section class="panel"><strong>Email Status:</strong> {{ send_message }}</section>
  {% endif %}

  {% if leads %}
    {% set found_emails = [] %}
    {% for lead in leads %}
      {% if lead.recruiter_email and lead.recruiter_email not in found_emails %}
        {% set _ = found_emails.append(lead.recruiter_email) %}
      {% endif %}
    {% endfor %}

    <section class="panel">
      <table>
        <thead>
          <tr>
            <th>Keyword</th>
            <th>Email</th>
            <th>Status</th>
            <th>Post</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% for lead in leads %}
            <tr>
              <td>{{ lead.keyword }}</td>
              <td>{{ lead.recruiter_email or '-' }}</td>
              <td class="status">{{ lead.status }}</td>
              <td>
                <a href="{{ lead.post_url }}" target="_blank" rel="noreferrer">Open Post/Search</a>
                <p class="muted">{{ lead.post_text }}</p>
              </td>
              <td>
                {% if lead.gmail_compose_url %}
                  <form method="post" action="/compose-gmail" style="display:block; margin-top: 8px;">
                    <input type="hidden" name="full_name" value="{{ candidate.full_name }}">
                    <input type="hidden" name="gmail_id" value="{{ candidate.gmail_id }}">
                    <input type="hidden" name="phone" value="{{ candidate.phone }}">
                    <input type="hidden" name="resume_link" value="{{ candidate.resume_link }}">
                    <input type="hidden" name="skills_summary" value="{{ candidate.skills_summary }}">
                    <input type="hidden" name="application_paragraph" value="{{ candidate.application_paragraph }}">
                    <input type="hidden" name="recruiter_email" value="{{ lead.recruiter_email }}">
                    <input type="hidden" name="keyword" value="{{ lead.keyword }}">
                    <input type="hidden" name="post_url" value="{{ lead.post_url }}">
                    <button class="secondary" type="submit">Open Compose in Default Browser</button>
                  </form>
                  <form method="post" action="/send-email" style="display:block; margin-top: 8px;">
                    <input type="hidden" name="full_name" value="{{ candidate.full_name }}">
                    <input type="hidden" name="gmail_id" value="{{ candidate.gmail_id }}">
                    <input type="hidden" name="phone" value="{{ candidate.phone }}">
                    <input type="hidden" name="resume_link" value="{{ candidate.resume_link }}">
                    <input type="hidden" name="skills_summary" value="{{ candidate.skills_summary }}">
                    <input type="hidden" name="application_paragraph" value="{{ candidate.application_paragraph }}">
                    <input type="hidden" name="recruiter_email" value="{{ lead.recruiter_email }}">
                    <input type="hidden" name="keyword" value="{{ lead.keyword }}">
                    <input type="hidden" name="post_url" value="{{ lead.post_url }}">
                    <button type="submit">Send Email</button>
                  </form>
                {% else %}
                  -
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h2>Recruiter Emails Found</h2>
      {% if found_emails %}
        <ul class="email-list">
          {% for email in found_emails %}
            <li>
              <span class="email-value">{{ email }}</span>
              <a class="button secondary" href="mailto:{{ email }}">Mail</a>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="muted">No recruiter email was visible in the scanned LinkedIn posts. Try increasing posts per keyword or using keywords like "email Java Developer contract", "share resume Java", or "send CV Java Developer".</p>
      {% endif %}
    </section>
  {% endif %}
</main>
</body>
</html>
"""


@app.context_processor
def inject_email_setup() -> dict[str, dict[str, str | bool]]:
    return {"email_setup": email_setup_status()}


@app.get("/")
def index() -> str:
    candidate = default_candidate()
    return render_template_string(
        TEMPLATE,
        candidate=None,
        leads=[],
        error="",
        csv_file="",
        send_message="",
        keywords="",
        max_posts=8,
        default_name=candidate.full_name,
        default_gmail=candidate.gmail_id,
        default_phone=candidate.phone,
        default_resume=candidate.resume_link,
        default_skills=candidate.skills_summary,
        default_paragraph=candidate.application_paragraph,
    )


@app.get("/latest-leads")
def latest_leads() -> str:
    candidate = default_candidate()
    csv_path = latest_leads_csv()
    leads: list[RecruiterLead] = []
    error = ""
    csv_file = ""
    if csv_path is None:
        error = "No LinkedIn lead CSV found yet. Run a LinkedIn search first."
    else:
        leads = read_leads_csv(csv_path, candidate)
        csv_file = str(csv_path.relative_to(APP_DIR))

    return render_template_string(
        TEMPLATE,
        candidate=candidate,
        leads=leads,
        error=error,
        csv_file=csv_file,
        send_message="Loaded latest saved leads. Use Open Compose in Default Browser or Send Email for leads with recruiter emails.",
        keywords="",
        max_posts=8,
        default_name=candidate.full_name,
        default_gmail=candidate.gmail_id,
        default_phone=candidate.phone,
        default_resume=candidate.resume_link,
        default_skills=candidate.skills_summary,
        default_paragraph=candidate.application_paragraph,
    )


@app.get("/login-linkedin")
def login_linkedin() -> Response:
    try:
        open_linkedin_login_browser()
        message = (
            f"LinkedIn login browser opened using {BROWSER_NAME}. "
            f"Profile: {browser_profile_dir()} / {PROFILE_DIRECTORY}. "
            "Log in there, then return to this app."
        )
    except WebDriverException as exc:
        message = f"Could not open {BROWSER_NAME} automation browser: {exc}"
    return Response(message, mimetype="text/plain")


@app.get("/login-gmail")
def login_gmail() -> Response:
    try:
        open_gmail_login_browser()
        message = (
            "Gmail opened in your system default browser. "
            "Log in there, then return to this app."
        )
    except OSError as exc:
        message = f"Could not open Gmail in your default browser: {exc}"
    return Response(message, mimetype="text/plain")


@app.get("/close-linkedin")
def close_linkedin() -> Response:
    close_linkedin_driver()
    return Response("Automation browser closed. You can open LinkedIn Login again.", mimetype="text/plain")


@app.post("/search")
def search() -> str:
    candidate = Candidate(
        full_name=request.form.get("full_name", "").strip(),
        gmail_id=request.form.get("gmail_id", "").strip(),
        phone=request.form.get("phone", "").strip(),
        resume_link=request.form.get("resume_link", "").strip(),
        skills_summary=request.form.get("skills_summary", "").strip(),
        application_paragraph=request.form.get("application_paragraph", "").strip(),
    )
    keywords_raw = request.form.get("keywords", "").strip()
    keywords = [item.strip() for item in keywords_raw.split(",") if item.strip()]
    max_posts = int(request.form.get("max_posts", "8") or 8)

    leads: list[RecruiterLead] = []
    error = ""
    csv_file = ""
    try:
        leads = search_linkedin_posts(candidate, keywords, max_posts)
        csv_file = str(write_csv(candidate, leads).relative_to(APP_DIR))
    except WebDriverException as exc:
        error = f"Chrome/Selenium error: {exc}"

    return render_template_string(
        TEMPLATE,
        candidate=candidate,
        leads=leads,
        error=error,
        csv_file=csv_file,
        send_message="",
        keywords=keywords_raw,
        max_posts=max_posts,
        default_name=os.getenv("CANDIDATE_NAME", ""),
        default_gmail=os.getenv("GMAIL_ID", ""),
        default_phone=os.getenv("PHONE", ""),
        default_resume=os.getenv("RESUME_LINK", ""),
        default_skills=default_candidate().skills_summary,
        default_paragraph=default_candidate().application_paragraph,
    )


@app.post("/compose-gmail")
def compose_gmail() -> str:
    candidate = Candidate(
        full_name=request.form.get("full_name", "").strip(),
        gmail_id=request.form.get("gmail_id", "").strip(),
        phone=request.form.get("phone", "").strip(),
        resume_link=request.form.get("resume_link", "").strip(),
        skills_summary=request.form.get("skills_summary", "").strip(),
        application_paragraph=request.form.get("application_paragraph", "").strip(),
    )
    recruiter_email = request.form.get("recruiter_email", "").strip()
    keyword = request.form.get("keyword", "").strip()
    post_url = request.form.get("post_url", "").strip()
    lead = RecruiterLead(
        keyword=keyword,
        recruiter_email=recruiter_email,
        post_text="Gmail compose opened in the automation browser. Attachments cannot be added through Gmail compose URLs; use Send Email for resume attachment.",
        post_url=post_url,
        status="Gmail Compose Opened",
        gmail_compose_url=make_gmail_compose_url(candidate, recruiter_email, keyword, post_url) if recruiter_email else "",
    )

    error = ""
    send_message = f"Gmail compose opened for {recruiter_email}."
    try:
        open_gmail_compose_browser(candidate, recruiter_email, keyword, post_url)
    except OSError as exc:
        error = f"Could not open Gmail compose: {exc}"
        send_message = ""

    return render_template_string(
        TEMPLATE,
        candidate=candidate,
        leads=[lead],
        error=error,
        csv_file="",
        send_message=send_message,
        keywords=keyword,
        max_posts=8,
        default_name=os.getenv("CANDIDATE_NAME", ""),
        default_gmail=os.getenv("GMAIL_ID", ""),
        default_phone=os.getenv("PHONE", ""),
        default_resume=os.getenv("RESUME_LINK", ""),
        default_skills=default_candidate().skills_summary,
        default_paragraph=default_candidate().application_paragraph,
    )


@app.post("/manual-email")
def manual_email() -> str:
    candidate = Candidate(
        full_name=request.form.get("full_name", "").strip(),
        gmail_id=request.form.get("gmail_id", "").strip(),
        phone=request.form.get("phone", "").strip(),
        resume_link=request.form.get("resume_link", "").strip(),
        skills_summary=request.form.get("skills_summary", "").strip(),
        application_paragraph=request.form.get("application_paragraph", "").strip(),
    )
    recruiter_email = request.form.get("recruiter_email", "").strip()
    keyword = request.form.get("keyword", "").strip()
    post_url = request.form.get("post_url", "").strip()
    lead = RecruiterLead(
        keyword=keyword,
        recruiter_email=recruiter_email,
        post_text="Manual recruiter email prepared. Use Open Compose in Default Browser to preview, or Send Email to send with resume attachment.",
        post_url=post_url or "https://www.linkedin.com/",
        status="Manual Email Ready",
        gmail_compose_url=make_gmail_compose_url(candidate, recruiter_email, keyword, post_url) if recruiter_email else "",
    )

    return render_template_string(
        TEMPLATE,
        candidate=candidate,
        leads=[lead],
        error="",
        csv_file="",
        send_message=f"Prepared email for {recruiter_email}.",
        keywords=keyword,
        max_posts=8,
        default_name=os.getenv("CANDIDATE_NAME", ""),
        default_gmail=os.getenv("GMAIL_ID", ""),
        default_phone=os.getenv("PHONE", ""),
        default_resume=os.getenv("RESUME_LINK", ""),
        default_skills=default_candidate().skills_summary,
        default_paragraph=default_candidate().application_paragraph,
    )


@app.post("/send-email")
def send_email() -> str:
    candidate = Candidate(
        full_name=request.form.get("full_name", "").strip(),
        gmail_id=request.form.get("gmail_id", "").strip(),
        phone=request.form.get("phone", "").strip(),
        resume_link=request.form.get("resume_link", "").strip(),
        skills_summary=request.form.get("skills_summary", "").strip(),
        application_paragraph=request.form.get("application_paragraph", "").strip(),
    )
    recruiter_email = request.form.get("recruiter_email", "").strip()
    keyword = request.form.get("keyword", "").strip()
    post_url = request.form.get("post_url", "").strip()

    result = send_application_email(candidate, recruiter_email, keyword, post_url)
    lead = RecruiterLead(
        keyword=keyword,
        recruiter_email=recruiter_email,
        post_text="Email send action completed for this recruiter lead.",
        post_url=post_url,
        status="Email Sent" if result.ok else "Email Not Sent",
        gmail_compose_url=make_gmail_compose_url(candidate, recruiter_email, keyword, post_url) if recruiter_email else "",
    )

    return render_template_string(
        TEMPLATE,
        candidate=candidate,
        leads=[lead],
        error="" if result.ok else result.message,
        csv_file="linkedin_logs/sent_email_log.csv" if result.ok else "",
        send_message=result.message,
        keywords=keyword,
        max_posts=8,
        default_name=os.getenv("CANDIDATE_NAME", ""),
        default_gmail=os.getenv("GMAIL_ID", ""),
        default_phone=os.getenv("PHONE", ""),
        default_resume=os.getenv("RESUME_LINK", ""),
        default_skills=default_candidate().skills_summary,
        default_paragraph=default_candidate().application_paragraph,
    )


if __name__ == "__main__":
    app.run(debug=False, port=int(os.getenv("PORT", "5010")), threaded=True)
