import json
import os
from openai import OpenAI
from supabase import Client, create_client

# Initialize Supabase and Groq API clients
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY or not GROQ_API_KEY:
  raise ValueError("Required environment variables are missing.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Groq uses an OpenAI-compatible client interface
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY
)


def ai_qualify_and_enrich_lead(raw_business_text):
  """Uses Groq API to filter out bad leads, extract owner name,

  and generate custom sales notes for the AI outreach phase.
  """
  prompt = f"""
    Analyze the following raw scraped business text for a plumbing company in the US:
    ---
    {raw_business_text}
    ---

    Task:
    1. Determine if this is a genuine independent local plumbing business (is_valid: true/false). Discard national chains or non-plumbers.
    2. Extract the owner or manager's name if mentioned (otherwise null).
    3. Clean up the business name.
    4. Write a 1-sentence sales note highlighting why they need an AI receptionist (e.g., missing 24/7 booking, emergency services offered, small local crew).

    Return ONLY a valid JSON object with these exact keys:
    {{
        "is_valid": true,
        "business_name": "Clean Business Name",
        "owner_name": "Owner Name or null",
        "notes": "Sales note for personalization"
    }}
    """

  try:
    response = ai_client.chat.completions.create(
        model="llama-3.3-70b-versatile",  # Fast, highly accurate Groq model
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    result = json.loads(response.choices[0].message.content)
    return result
  except Exception as e:
    print(f"AI qualification failed: {e}")
    return None


def save_lead_to_supabase(lead_data, email):
  """Saves the AI-filtered lead to Supabase.

  Email UNIQUE constraint handles duplicate prevention.
  """
  try:
    supabase.table("leads").insert({
        "business_name": lead_data["business_name"],
        "owner_name": lead_data.get("owner_name"),
        "email": email,
        "notes": lead_data["notes"],
        "status": "pending",
    }).execute()
    print(f"Saved qualified lead: {lead_data['business_name']} ({email})")
  except Exception as e:
    error_message = str(e).lower()
    if "duplicate key value" in error_message or "unique constraint" in error_message:
      print(f"Skipped duplicate email: {email}")
    else:
      print(f"Error saving lead: {e}")


def run_smart_scraper():
  print("Starting AI-powered plumbing lead scraper...")

  # Mock raw scraped results from directory searches / web text
  raw_scraped_batch = [
      {
          "email": "info@apexflowplumbers.example",
          "raw_text": (
              "Apex Flow Plumbing LLC - Owner John Miller. Serving Chicago"
              " area. Offers 24/7 emergency water heater repairs. Website"
              " has no contact form or booking widget."
          ),
      },
      {
          "email": "contact@rotorootermegacorps.example",
          "raw_text": (
              "Roto-Rooter National Franchise Corporation. Nationwide"
              " corporate customer service line."
          ),
      },
  ]

  for item in raw_scraped_batch:
    print(f"Processing raw entry for email: {item['email']}...")
    ai_result = ai_qualify_and_nested = ai_qualify_and_enrich_lead(
        item["raw_text"]
    )

    if ai_result and ai_result.get("is_valid") is True:
      save_lead_to_supabase(ai_result, item["email"])
    else:
      print(
          f"Lead rejected by AI filter (Not a valid independent plumber):"
          f" {item['email']}"
      )

  print("AI scraping and qualification run completed.")


if __name__ == "__main__":
  run_smart_scraper()
