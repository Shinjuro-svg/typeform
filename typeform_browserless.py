#!/usr/bin/env python3
import requests
import json
import os
import time
import random
import tempfile
from dotenv import load_dotenv
from supabase import create_client, Client
import openai
from playwright.sync_api import sync_playwright

# --- Configuration (UPDATE THESE VALUES) ---
TYPEFORM_FORM_ID = "qedCsWYt"
TYPEFORM_URL = f"https://form.typeform.com/to/{TYPEFORM_FORM_ID}"

# Set to True to run without a browser window
HEADLESS = False

# Define the path to your placeholder file (for file_upload fields).
PITCH_DECK_PATH = os.path.join(os.getcwd(), "placeholder_deck.pdf")

# Load environment variables from .env file
load_dotenv()

# Supabase Connection Details
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Create Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# OpenAI Connection Details
openai.api_key = os.getenv("OPENAI_API_KEY")

# --- Helpers & Core Functions ---

def get_rows():
    response = supabase.table("form_submissions").select("*").execute()
    rows = response.data
    return rows

def get_form_fields(form_id: str) -> list[dict]:
    """Retrieve public Typeform fields. Returns list of dicts with ref, title, type and options."""
    print("Step 1: Discovering public form fields via Typeform API...")
    api_url = f"https://api.typeform.com/forms/{form_id}"
    try:
        resp = requests.get(api_url, timeout=15)
        resp.raise_for_status()
        form_data = resp.json()
        fields = []
        for f in form_data.get("fields", []):
            fields.append({
                "ref": f.get("ref"),
                "title": f.get("title"),
                "type": f.get("type"),
                "options": [
                    c.get("label")
                    for c in f.get("properties", {}).get("choices", [])
                ] if f.get("type") in ["multiple_choice", "picture_choice"] else []
            })
        # print(fields)
        
        return fields
    except Exception as e:
        print(f"Error retrieving Typeform fields: {e}")
        return []

def map_row_to_typeform(fields: list, row: dict, model: str = "gpt-5"):
    print("Step 2: Mapping Supabase row to Typeform fields using GPT...")
    
    prompt = f"""
You are a smart assistant filling a Typeform using a Supabase row.
Output ONLY a JSON object, no extra text. 
Keys = Typeform question titles in order.

### RULES ###
1. For text, number, email, url, etc → output a realistic string or number.
2. For multiple_choice:
    - Use the data from the row to select options.
    - Output the **indexes** corresponding to selected choices, starting at 0, as comma-separated values (example: 0,2).
3. For dropdown:
    - Output the single most suitable option index (1-based).
4. If a row column is missing or empty:
    - Generate a relevant, realistic answer based on other available data.
    - Answers must be contextually consistent with existing row values.
5. Never output placeholders like "sa", "Tell us", "How about no?".

### TYPEFORM FIELDS (in order) ###
{json.dumps(fields, indent=2)}

### SUPABASE ROW ###
{json.dumps(row, indent=2)}

Return ONLY a JSON object in this format:
{{
  "Question 1 title": "value",
  "Question 2 title": "value",
  ...
}}
"""

    response = openai.ChatCompletion.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )

    return json.loads(response.choices[0].message["content"])

def fill_and_submit_form(url: str, fields: list[dict], answers: dict):
    """
    Use Playwright to open the Typeform, fill fields, and submit.
    Dynamically inputs data from 'answers' dict.
    """
    print("Step 4: Filling and submitting the form with Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(25000)
        page.goto(url)
        page.wait_for_load_state("networkidle")

        # Try to click a start button if present
        try:
            start_btn = page.get_by_role("button", name="Start", exact=False)
            if start_btn.count() > 0:
                try:
                    start_btn.first.click(timeout=8000)
                    page.wait_for_timeout(700)
                except Exception:
                    # fallback JS
                    try:
                        eh = start_btn.first.element_handle()
                        if eh:
                            page.evaluate("(el) => el.click()", eh)
                            page.wait_for_timeout(700)
                    except Exception:
                        pass
        except Exception:
            pass

        def safe_press_enter():
            try:
                page.keyboard.press("Enter")
                time.sleep(2)
            except Exception:
                pass

        for idx, field in enumerate(fields):
            time.sleep(1)
            page.wait_for_timeout(500)
            q_type = field.get("type", "")
            q_ref = field.get("ref")
            provided_answer = answers.get(field.get("title")) or answers.get(q_ref)  # check by title first

            print(f"\n→ Handling ({idx+1}): {q_type}  (ref={q_ref})")

            time.sleep(random.uniform(0.6, 1.4))

            try:
                if q_type in ["short_text", "email", "number", "website", "text", "long_text"]:
                    # type text
                    answer = str(provided_answer) if provided_answer else "N/A"
                    page.keyboard.type(answer)
                    time.sleep(0.5)
                    safe_press_enter()
                
                elif q_type in ["multiple_choice", "picture_choice", "checkboxes"]:
                    # ensure we have a string
                    if provided_answer is not None:
                        ans_str = str(provided_answer)
                        for key in ans_str.replace(" ", "").split(","):
                            key = key.lower()
                            # convert numeric index to letter (0 -> a, 1 -> b, etc)
                            if key.isdigit():
                                key = chr(int(key) + ord('a'))
                            page.keyboard.press(key)
                            time.sleep(0.3)
                    time.sleep(0.5)
                    safe_press_enter()

                elif q_type == "dropdown":
                    page.keyboard.press('Tab')
                    time.sleep(2)
                    safe_press_enter()
                    time.sleep(2)
                    # answer is a single index (1-based)
                    if provided_answer:
                        try:
                            index = int(provided_answer)
                        except:
                            index = 1
                        for _ in range(index):
                            page.keyboard.press("ArrowDown")
                            time.sleep(0.3)
                    time.sleep(0.5)
                    safe_press_enter()

                elif q_type == "file_upload":
                    # download file from URL and upload
                    file_path = PITCH_DECK_PATH
                    if provided_answer and provided_answer.startswith("http"):
                        resp = requests.get(provided_answer)
                        resp.raise_for_status()
                        tmp_file = tempfile.NamedTemporaryFile(delete=False)
                        tmp_file.write(resp.content)
                        tmp_file.close()
                        file_path = tmp_file.name

                    upload_input = page.locator('input[type="file"]')
                    upload_input.set_input_files(file_path)
                    print(f"✅ Uploaded file: {file_path}")
                    time.sleep(6)
                    safe_press_enter()

                else:
                    print(f"⚠️ Unknown field type '{q_type}', attempting to skip.")
                    safe_press_enter()

                time.sleep(random.uniform(0.8, 1.5))

            except Exception as e:
                print(f"!!! Exception while handling field {q_ref}: {e}")
                safe_press_enter()

        # Try final submission
        print("\nAttempting final submission...")
        try:
            page.keyboard.press("Control+Enter")
            try:
                page.wait_for_selector("text=Thank you", timeout=20000)
                print("✅ Submission appears successful (found Thank you).")
            except Exception:
                print("⚠️ Couldn't detect a Thank you message — submission may still have gone through.")
        except Exception as e:
            print(f"Final submission attempt raised: {e}")

        browser.close()
        print("Done.")


# --- Main Execution ---
if __name__ == "__main__":
    # Fetch rows from Supabase
    rows = get_rows()
    
    # Fetch Typeform fields
    fields = get_form_fields(TYPEFORM_FORM_ID)
    
    for index, row in enumerate(rows):
        mapping = map_row_to_typeform(fields, row)
        # Debug: show mapping
        print(mapping)
        fill_and_submit_form(TYPEFORM_URL, fields, mapping)
