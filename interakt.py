"""
interakt.py — every direct interaction with the Interakt API lives
here. main.py never talks to Interakt's endpoints directly; it only
calls functions from this file. Keeping it this way means if Interakt
ever changes their API, you only touch this one file.
"""

import os
import hmac
from hashlib import sha256
import requests
from dotenv import load_dotenv

load_dotenv()

INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY")
INTERAKT_WEBHOOK_SECRET = os.getenv("INTERAKT_WEBHOOK_SECRET")

BASE_URL = "https://api.interakt.ai/v1/public"


def _headers():
    return {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json",
    }


def _split_phone(phone_with_country_code: str):
    """
    Splits a number like "917003705584" (as Interakt sends it in
    data.customer.channel_phone_number) into country code + number.
    NOTE: assumes a 2-digit country code -- adjust if you support
    countries with 1 or 3-digit codes.
    """
    country_code = "+" + phone_with_country_code[:2]
    number = phone_with_country_code[2:]
    return country_code, number


def send_text_message(phone: str, message: str):
    """phone: e.g. "917003705584" (country code + number, no '+')."""
    country_code, number = _split_phone(phone)
    payload = {
        "countryCode": country_code,
        "phoneNumber": number,
        "type": "Text",
        "data": {"message": message},
    }
    response = requests.post(f"{BASE_URL}/message/", headers=_headers(), json=payload)
    print("[interakt] send_text_message:", response.status_code, response.text)
    return response


def send_image_message(phone: str, media_url: str, caption: str = ""):
    """
    Sends an image with an optional caption -- e.g. a payment QR code,
    a product photo, or a receipt.
    phone: e.g. "917003705584"
    media_url: a publicly reachable HTTPS URL to the image
    """
    country_code, number = _split_phone(phone)
    payload = {
        "countryCode": country_code,
        "phoneNumber": number,
        "type": "Image",
        "data": {
            "message": caption,
            "mediaUrl": media_url,
        },
    }
    response = requests.post(f"{BASE_URL}/message/", headers=_headers(), json=payload)
    print("[interakt] send_image_message:", response.status_code, response.text)
    return response


def assign_chat_to_agent(phone: str, agent_email: str) -> bool:
    """
    Returns True if the chat ends up assigned to this agent -- either
    the call succeeded, or it was already assigned to them (Interakt
    returns a 400 for that specific case, which isn't a real failure).
    """
    payload = {"user_phone_number": phone, "agent_email": agent_email}
    response = requests.post(f"{BASE_URL}/assignment/", headers=_headers(), json=payload)
    print("[interakt] assign_chat_to_agent:", response.status_code, response.text)

    if response.status_code == 200:
        return True
    if response.status_code == 400 and "already assigned to same agent" in response.text.lower():
        print(f"[interakt] {agent_email} was already assigned -- treating as success.")
        return True

    print(f"[interakt] Assignment genuinely failed: {response.status_code} {response.text}")
    return False


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verified against Interakt's own sample code for webhook security."""
    if not INTERAKT_WEBHOOK_SECRET:
        return True   # signature checking disabled if no secret configured
    computed = "sha256=" + hmac.new(INTERAKT_WEBHOOK_SECRET.encode(), payload, sha256).hexdigest()
    return computed == signature