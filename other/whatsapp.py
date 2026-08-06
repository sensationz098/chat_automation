import requests
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_ID = os.getenv("PHONE_NUMBER_ID")
GRAPH_API_VERSION = "v23.0"

def show_typing(message_id: str):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    response = requests.post(url, headers=headers, json=payload)
    print("Typing indicator status:", response.status_code, response.text)
    return response


def send_call_button(phone: str, body_text: str, button_label: str, call_number: str):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": button_label[:20],
                    "url": f"tel:{call_number}",
                },
            },
        },
    }

    response = requests.post(url, headers=headers, json=payload)
    print("Call button status:", response.status_code, response.text)
    return response


def send_button_message(phone: str, body_text: str, buttons: list):

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": btn_id, "title": title[:20]}}
                    for btn_id, title in buttons[:3]  # WhatsApp allows max 3 buttons
                ]
            },
        },
    }

    response = requests.post(url, headers=headers, json=payload)
    print("Button message status:", response.status_code, response.text)
    return response


def send_message(phone: str, message: str):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {
            "body": message
        }
    }

    response = requests.post(url, headers=headers, json=payload)

    print("Status:", response.status_code)
    print("Response:", response.text)

    if response.status_code != 200:
        print(
            "[whatsapp.py] Send failed — check WHATSAPP_TOKEN, "
            "PHONE_NUMBER_ID, and that the recipient is a verified "
            "test number."
        )

    return response

