"""
save_users_to_csv.py

Simple standalone script — no Redis, no queue.
Fetches ALL contacts from Interakt (auto-paginated) and saves their
name, phone number, and email to a CSV file.

Setup:
    pip install requests python-dotenv

Before running, set your API key (either as an environment variable
or directly below where INTERAKT_API_KEY is defined):

    export INTERAKT_API_KEY=your_real_api_key_here

Run:
    python save_users_to_csv.py

Output:
    interakt_users.csv
"""

import os
import csv
import base64
import requests

# ---- Config ----
INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY")
BASE_URL = "https://api.interakt.ai/v1/public/track/users/"
OUTPUT_FILE = "interakt_users.csv"
PAGE_SIZE = 100


def get_headers():
    token = base64.b64encode(f"{INTERAKT_API_KEY}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def fetch_page(offset: int, limit: int = PAGE_SIZE):
    url = f"{BASE_URL}?offset={offset}&limit={limit}"
    resp = requests.post(url, json={}, headers=get_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    offset = 0
    rows_written = 0

    with open(OUTPUT_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "phone_number", "country_code", "email"])

        while True:
            data = fetch_page(offset)

            # Interakt's field name for the contact list can be "result" or "data"
            # depending on account/version — print(data) once if this list is empty.
            users = data.get("result") or data.get("data") or []

            for user in users:
                traits = user.get("traits", {})
                name = traits.get("name", "")
                email = traits.get("email", "")
                phone_number = user.get("phoneNumber", "")
                country_code = user.get("countryCode", "")

                writer.writerow([name, phone_number, country_code, email])
                rows_written += 1

            if not data.get("has_next_page"):
                break
            offset += PAGE_SIZE

    print(f"Done. Saved {rows_written} contacts to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


