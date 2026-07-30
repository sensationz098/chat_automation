# import requests
# import os
# from dotenv import load_dotenv

# load_dotenv()

# TOKEN = os.getenv("WHATSAPP_TOKEN")
# PHONE_ID = os.getenv("PHONE_NUMBER_ID")


# def send_message(phone, message):
#     url = f"https://graph.facebook.com/v25.0/{PHONE_ID}/messages"

#     headers = {
#         "Authorization": f"Bearer {TOKEN}",
#         "Content-Type": "application/json"
#     }

#     payload = {
#         "messaging_product": "whatsapp",
#         "to": phone,
#         "type": "text",
#         "text": {
#             "body": message
#         }
#     }

#     response = requests.post(url, headers=headers, json=payload)

#     print("Status:", response.status_code)
#     print("Response:", response.text)


import requests
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_ID = os.getenv("PHONE_NUMBER_ID")

# v25.0 may not exist yet — use a confirmed current version.
# Check developers.facebook.com/docs/graph-api/changelog for the latest
# if you want to bump this later.
GRAPH_API_VERSION = "v25.0"


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