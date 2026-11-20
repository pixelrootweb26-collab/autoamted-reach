import os
import re
import json
import time
import imaplib
import email as email_lib
import random
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from bs4 import BeautifulSoup
from supabase import create_client, Client
from groq import Groq

# ==========================================
# CONFIGURATION (MAPPED TO YOUR EXISTING SECRETS)
# ==========================================
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GMAIL_USER = os.environ["SENDER_EMAIL"]
GMAIL_APP_PASSWORD = os.environ["SENDER_PASSWORD"]
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

DAILY_SEND_LIMIT = 20          # Safe daily limit per run
FOLLOWUP_DELAY_HOURS = 60      # 2.5 days (between 2 and 3 days)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

NICHES = ["plumbing company", "HVAC repair", "emergency plumber", "roofing contractor", "dental clinic", "auto repair shop"]
CITIES = [
    "Austin TX", "Dallas TX", "Houston TX", "Denver CO", "Phoenix AZ",
    "Tampa FL", "Orlando FL", "Atlanta GA", "Charlotte NC", "Nashville TN",
    "Columbus OH", "Indianapolis IN", "Seattle WA", "San Diego CA", "Las Vegas NV"
]

# ==========================================
# 1. IMAP REPLY CHECKER + AUTO-STAR IN GMAIL
# ==========================================
def sync_and_star_replies():
    """Finds prospect replies in Gmail, Stars (\Flagged) them for you, and stops follow-ups."""
    print("📥 Scanning Gmail Inbox for prospect replies...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return

        # Fetch all contacted leads from Supabase that haven't been marked replied yet
        active_leads_res = supabase.table("leads").select("id, email, business_name").in_("status", ["initial_sent", "followup_sent"]).execute()
        active_leads = {lead["email"].lower(): lead for lead in (active_leads_res.data or [])}

        if not active_leads:
            mail.logout()
            return

        for num in messages[0].split()[-150:]:
            _, data = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if data and data[0]:
                msg = email_lib.message_from_bytes(data[0][1])
                from_header = msg.get("From", "")
                match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', from_header)
                if match:
                    sender = match.group(0).lower()
                    if sender in active_leads:
                        lead = active_leads[sender]
                        # 1. Star (\Flagged) the email in Gmail so it appears in your Starred tab
                        mail.store(num, '+FLAGS', '\\Flagged')
                        # 2. Update Supabase status to 'replied' so automated follow-ups stop
                        supabase.table("leads").update({"status": "replied"}).eq("id", lead["id"]).execute()
                        print(f"⭐ Reply detected & STARRED in Gmail: {lead['business_name']} ({sender})")

        mail.logout()
    except Exception as e:
        print(f"[IMAP Warning] Could not sync replies: {e}")

# ==========================================
# 2. SCRAPER & GROQ DATA EXTRACTOR
# ==========================================
def get_headers():
    return {"User-Agent": random.choice(USER_AGENTS)}

def extract_emails(text: str) -> list[str]:
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    found = re.findall(pattern, text)
    blacklist = ('sentry', 'wixpress', 'example.com', '.png', '.jpg', '.jpeg', '.webp', 'noreply', 'no-reply', 'support@google')
    valid = []
    for e in set(found):
        e_low = e.lower().strip(".")
        if not any(b in e_low for b in blacklist) and len(e_low) < 65:
            valid.append(e_low)
    return valid

def scrape_website_details(url: str, fallback_name: str, city_hint: str) -> dict | None:
    try:
        resp = requests.get(url, headers=get_headers(), timeout=10)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        combined_text = soup.get_text(" ", strip=True)
        emails = extract_emails(resp.text)

        # Check contact/about subpages
        subpages = 0
        for link in soup.find_all("a", href=True):
            if subpages >= 2:
                break
            href = link["href"].lower()
            if any(k in href for k in ["contact", "about", "team"]):
                full_url = href if href.startswith("http") else requests.compat.urljoin(url, link["href"])
                if url.split("//")[-1].split("/")[0] not in full_url:
                    continue
                try:
                    sub_resp = requests.get(full_url, headers=get_headers(), timeout=6)
                    if sub_resp.status_code == 200:
                        subpages += 1
                        emails = list(set(emails + extract_emails(sub_resp.text)))
                        combined_text += " " + BeautifulSoup(sub_resp.text, "html.parser").get_text(" ", strip=True)
                except Exception:
                    continue

        if not emails:
            return None

        emails.sort(key=lambda x: (0 if not x.startswith(("info@", "contact@", "office@")) else 1))
        chosen_email = emails[0]

        owner_name = None
        business_name = fallback_name
        business_address = city_hint

        if groq_client:
            prompt = f"""Extract exact business info from this website text. Return ONLY valid JSON with keys:
"owner_name": string (First and Last name of owner/founder if mentioned, or null),
"business_name": string (Clean official business name),
"business_address": string (Full street address if present, otherwise "{city_hint}").

Text:
{combined_text[:2500]}"""
            try:
                comp = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                raw = re.sub(r"^```json|```$", "", comp.choices[0].message.content.strip()).strip()
                data = json.loads(raw)
                owner_name = data.get("owner_name")
                if owner_name and str(owner_name).lower() in ("null", "none", "unknown", "n/a"):
                    owner_name = None
                business_name = data.get("business_name") or fallback_name
                business_address = data.get("business_address") or city_hint
            except Exception:
                pass

        return {
            "email": chosen_email,
            "owner_name": owner_name,
            "business_name": business_name,
            "business_address": business_address,
            "website": url,
            "status": "pending"
        }
    except Exception:
        return None

def run_scraper(niche: str, city: str, max_results: int = 10):
    query = f"{niche} {city} contact email"
    print(f"🔍 Scraping: '{query}'")
    try:
        resp = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=get_headers(), timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select(".result")[:max_results]
    except Exception:
        return

    skip_domains = ("yelp.com", "yellowpages.com", "facebook.com", "instagram.com", "linkedin.com", "angi.com", "bbb.org", "thumbtack.com")

    saved = 0
    for res in results:
        a_tag = res.select_one(".result__a")
        if not a_tag:
            continue
        href = a_tag.get("href", "")
        if not href.startswith("http") or any(d in href.lower() for d in skip_domains):
            continue

        raw_title = a_tag.get_text(strip=True)
        clean_biz_name = re.split(r'\s*[-|–—:]\s*', raw_title)[0].strip()

        details = scrape_website_details(href, fallback_name=clean_biz_name, city_hint=city)
        if details:
            details["niche"] = niche
            try:
                db_res = supabase.table("leads").upsert(details, on_conflict="email", ignore_duplicates=True).execute()
                if db_res.data:
                    saved += 1
                    print(f"  ✅ Saved: {details['business_name']} | {details['email']} | Owner: {details['owner_name']}")
            except Exception:
                pass
        time.sleep(1.5)
    print(f"📦 Stored {saved} new unique leads for {city}\n")

# ==========================================
# 3. THREADED SENDER & FORMATTER
# ==========================================
def send_gmail_threaded(to_email: str, subject: str, body_text: str, in_reply_to: str = None) -> str | None:
    msg = MIMEMultipart()
    msg["From"] = f"Rohan Singh <{GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    new_msg_id = make_msgid(domain="gmail.com")
    msg["Message-ID"] = new_msg_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body_text, "plain"))

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        return new_msg_id
    except Exception as e:
        print(f"[SMTP Error] {to_email}: {e}")
        return None

def build_initial_email(lead: dict) -> tuple[str, str]:
    owner = lead.get("owner_name")
    biz = lead["business_name"]
    addr = lead.get("business_address")
    greeting = f"Hi {owner.split()[0]}," if owner else f"Hi {biz} team,"
    addr_mention = f" at {addr}" if addr and len(addr) > 10 else ""

    subject = f"Quick question for {biz}"
    body = f"""{greeting}

I came across {biz}{addr_mention} and wanted to reach out.

Many local service businesses lose high-intent calls when technicians are out on jobs or after hours. We build 24/7 AI voice receptionists and web funnels designed to capture and book those missed leads automatically.

Would you be open to a quick 5-minute demo showing how this works for {biz}?

Best,
Rohan Singh
PixelRoot
"""
    return subject, body

def build_followup_email(lead: dict) -> tuple[str, str]:
    owner = lead.get("owner_name")
    biz = lead["business_name"]
    greeting = f"Hi {owner.split()[0]}," if owner else f"Hi {biz} team,"

    subject = f"Re: Quick question for {biz}"
    body = f"""{greeting}

Just bumping this to the top of your inbox in case it got buried during a busy week.

If you're currently handling incoming calls manually at {biz}, I can send over a 60-second sample call recording tailored to your exact workflow—no strings attached.

Worth sending over?

Best,
Rohan Singh
PixelRoot
"""
    return subject, body

def send_due_followups(max_sends: int) -> int:
    print("🔄 Checking for leads ready for 2.5-day follow-up...")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FOLLOWUP_DELAY_HOURS)).isoformat()

    res = supabase.table("leads").select("*").eq("status", "initial_sent").lte("initial_sent_at", cutoff).limit(max_sends).execute()
    sent = 0
    for lead in (res.data or []):
        subject, body = build_followup_email(lead)
        sent_id = send_gmail_threaded(lead["email"], subject, body, in_reply_to=lead.get("message_id"))
        if sent_id:
            supabase.table("leads").update({
                "status": "followup_sent",
                "followup_sent_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", lead["id"]).execute()
            sent += 1
            print(f"  🔁 Follow-up sent to {lead['email']}")
            time.sleep(random.uniform(12, 22))
    return sent

def send_initial_outreach(max_sends: int) -> int:
    print("🚀 Sending initial emails to new pending leads...")
    res = supabase.table("leads").select("*").eq("status", "pending").limit(max_sends).execute()
    sent = 0
    for lead in (res.data or []):
        subject, body = build_initial_email(lead)
        msg_id = send_gmail_threaded(lead["email"], subject, body)
        if msg_id:
            supabase.table("leads").update({
                "status": "initial_sent",
                "message_id": msg_id,
                "initial_sent_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", lead["id"]).execute()
            sent += 1
            print(f"  ✉️ Initial email sent to {lead['email']}")
            time.sleep(random.uniform(12, 22))
    return sent

if __name__ == "__main__":
    print("=== GOD-TIER OUTREACH ENGINE STARTED ===")
    sync_and_star_replies()

    for _ in range(3):
        run_scraper(random.choice(NICHES), random.choice(CITIES), max_results=10)

    followups = send_due_followups(max_sends=10)
    remaining = max(0, DAILY_SEND_LIMIT - followups)
    if remaining > 0:
        send_initial_outreach(max_sends=remaining)
    print("=== RUN COMPLETE ===")
