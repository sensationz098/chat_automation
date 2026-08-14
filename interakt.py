"""
interakt.py — every direct interaction with the Interakt API lives
here. main.py never talks to Interakt's endpoints directly; it only
calls functions from this file. Keeping it this way means if Interakt
ever changes their API, you only touch this one file.
"""

import os
import hmac
import time
from hashlib import sha256
import httpx
from dotenv import load_dotenv

load_dotenv()

INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY")
INTERAKT_WEBHOOK_SECRET = os.getenv("INTERAKT_WEBHOOK_SECRET")

BASE_URL = "https://api.interakt.ai/v1/public"

# Module-level async HTTP client with connection pooling for performance
_async_client = None

def _get_async_client() -> httpx.AsyncClient:
    """Returns a singleton async HTTP client with connection pooling."""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _async_client


def _headers():
    return {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json",
    }


def _split_phone(phone_with_country_code: str):
    """
    Splits a number like "917003705584" into country code + number.
    NOTE: assumes a 2-digit country code.
    """
    country_code = "+" + phone_with_country_code[:2]
    number = phone_with_country_code[2:]
    return country_code, number


def send_text_message(phone: str, message: str):
    """
    Synchronous text message sender (used from sync contexts).
    phone: e.g. "917003705584" (country code + number, no '+').
    """
    country_code, number = _split_phone(phone)
    payload = {
        "countryCode": country_code,
        "phoneNumber": number,
        "type": "Text",
        "data": {"message": message},
    }
    t0 = time.perf_counter()
    try:
        response = httpx.post(
            f"{BASE_URL}/message/",
            headers=_headers(),
            json=payload,
            timeout=10.0,
        )
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message: {response.status_code} ({elapsed:.2f}s)")
        return response
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message TIMEOUT after {elapsed:.2f}s")
        return None
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message error ({elapsed:.2f}s): {e}")
        return None


async def send_text_message_async(phone: str, message: str):
    """
    Async text message sender — non-blocking, uses connection pooling.
    """
    country_code, number = _split_phone(phone)
    payload = {
        "countryCode": country_code,
        "phoneNumber": number,
        "type": "Text",
        "data": {"message": message},
    }
    t0 = time.perf_counter()
    try:
        client = _get_async_client()
        response = await client.post(
            f"{BASE_URL}/message/",
            headers=_headers(),
            json=payload,
        )
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message_async: {response.status_code} ({elapsed:.2f}s)")
        return response
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message_async TIMEOUT after {elapsed:.2f}s")
        return None
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] send_text_message_async error ({elapsed:.2f}s): {e}")
        return None


def send_image_message(phone: str, media_url: str, caption: str = ""):
    """Sends an image with an optional caption."""
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
    try:
        response = httpx.post(
            f"{BASE_URL}/message/",
            headers=_headers(),
            json=payload,
            timeout=10.0,
        )
        print(f"[interakt] send_image_message: {response.status_code}")
        return response
    except Exception as e:
        print(f"[interakt] send_image_message error: {e}")
        return None


def assign_chat_to_agent(phone: str, agent_email: str) -> bool:
    """
    Returns True if the chat ends up assigned to this agent — either
    the call succeeded, or it was already assigned to them.
    """
    payload = {"user_phone_number": phone, "agent_email": agent_email}
    t0 = time.perf_counter()
    try:
        response = httpx.post(
            f"{BASE_URL}/assignment/",
            headers=_headers(),
            json=payload,
            timeout=10.0,
        )
        elapsed = time.perf_counter() - t0
        print(f"[interakt] assign_chat_to_agent: {response.status_code} ({elapsed:.2f}s)")

        if response.status_code == 200:
            return True
        if response.status_code == 400 and "already assigned to same agent" in response.text.lower():
            print(f"[interakt] {agent_email} was already assigned -- treating as success.")
            return True

        print(f"[interakt] Assignment genuinely failed: {response.status_code} {response.text}")
        return False
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] assign_chat_to_agent error ({elapsed:.2f}s): {e}")
        return False


async def assign_chat_to_agent_async(phone: str, agent_email: str) -> bool:
    """
    Async version — non-blocking, uses connection pooling.
    Gracefully handles 400 "Customer matching query does not exist".
    """
    payload = {"user_phone_number": phone, "agent_email": agent_email}
    t0 = time.perf_counter()
    try:
        client = _get_async_client()
        response = await client.post(
            f"{BASE_URL}/assignment/",
            headers=_headers(),
            json=payload,
        )
        elapsed = time.perf_counter() - t0
        print(f"[interakt] assign_chat_to_agent_async({agent_email}): {response.status_code} ({elapsed:.2f}s)")

        if response.status_code == 200:
            return True
        if response.status_code == 400:
            body = response.text.lower()
            if "already assigned to same agent" in body:
                print(f"[interakt] {agent_email} was already assigned -- treating as success.")
                return True
            if "customer matching query does not exist" in body:
                print(f"[interakt] Customer {phone} not yet in Interakt -- assignment skipped (non-fatal).")
                return False
            print(f"[interakt] Assignment 400 error: {response.text}")
            return False

        print(f"[interakt] Assignment failed: {response.status_code} {response.text}")
        return False
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] assign_chat_to_agent_async TIMEOUT after {elapsed:.2f}s")
        return False
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"[interakt] assign_chat_to_agent_async error ({elapsed:.2f}s): {e}")
        return False


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verifies Interakt webhook signature for security."""
    if not INTERAKT_WEBHOOK_SECRET:
        return True   # signature checking disabled if no secret configured
    computed = "sha256=" + hmac.HMAC(
        INTERAKT_WEBHOOK_SECRET.encode(), payload, sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
