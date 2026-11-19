import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
from supabase import Client, create_client

# Pull credentials from environment variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")

# Initialize clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY
)


def generate_ai_email(business_name, owner_name, notes):
  """Uses Groq API to draft a short, highly personalized cold email

  tailored specifically to the plumber's business context.
  """
  name_salutation = owner_name if owner_name else "there"
  prompt = f"""
    Write a short, professional, and friendly cold outreach email to {business_name}, a US plumbing company. 
    Owner's name: {name_salutation}.
    Business insights/notes from our AI scraper: {notes if notes else 'Local plumbing shop handling emergency calls.'}
    
    Rules:
    - Keep it under 4 sentences.
    - Focus directly on how an AI receptionist answers missed after-hours emergency calls so they capture every $500+ job.
    - Zero corporate fluff, hype, or buzzwords. Sound like a peer building software for local businesses.
    - End with a low-friction question asking if they want to call a live 30-second demo number.
    """

  try:
    response = ai_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=150,
    )
    return response.choices[0].message.content.strip()
  except Exception as e:
    print(f"AI email generation failed: {e}")
    return (
        f"Hi {name_salutation},\n\nI noticed {business_name} handles local"
        " plumbing work. Quick question—how many emergency calls go to"
        " voicemail after hours? We build AI receptionists that answer in 1"
        " second and text you booked jobs. Worth a quick look?"
    )


def send_daily_outreach_batch():
  print("Fetching pending leads batch (limit 15) from Supabase...")
  response = (
      supabase.table("leads")
      .select("*")
      .eq("status", "pending")
      .limit(15)
      .execute()
  )
  leads = response.data

  if not leads:
    print("No pending leads found in the database. Run your scraper first!")
    return

  print(f"Found {len(leads)} leads to process. Connecting to SMTP server...")
  server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
  server.starttls()
  server.login(SENDER_EMAIL, SENDER_PASSWORD)

  for lead in leads:
    lead_id = lead["id"]
    business_name = lead["business_name"]
    owner_name = lead["owner_name"]
    email = lead["email"]
    notes = lead.get("notes", "")

    try:
      # 1. Generate unique AI email body
      ai_body = generate_ai_email(business_name, owner_name, notes)

      # 2. Assemble email packet
      msg = MIMEMultipart()
      msg["From"] = SENDER_EMAIL
      msg["To"] = email
      msg["Subject"] = f"Quick question regarding {business_name}"
      msg.attach(MIMEText(ai_body, "plain"))

      # 3. Send email
      server.sendmail(SENDER_EMAIL, email, msg.as_string())

      # 4. Update status in Supabase so it's never emailed again
      supabase.table("leads").update({"status": "contacted"}).eq(
          "id", lead_id
      ).execute()
      print(f"Successfully sent AI email to {email}")

    except Exception as e:
      print(f"Failed for {email}: {e}")
      # Mark as failed so it doesn't block the queue indefinitely
      supabase.table("leads").update({"status": "failed"}).eq(
          "id", lead_id
      ).execute()

  server.quit()
  print("Daily outreach batch completed successfully.")


if __name__ == "__main__":
  send_daily_outreach_batch()
