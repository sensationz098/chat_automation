# import requests
# import os

# from dotenv import load_dotenv


# load_dotenv()


# TOKEN = os.getenv(
#     "WHATSAPP_TOKEN"
# )


# PHONE_ID = os.getenv(
#     "PHONE_NUMBER_ID"
# )



# def send_message(
#     phone,
#     message
# ):


#     url = (
#         f"https://graph.facebook.com/v20.0/"
#         f"{PHONE_ID}/messages"
#     )


#     headers = {

#         "Authorization":
#         f"Bearer {TOKEN}",

#         "Content-Type":
#         "application/json"

#     }



#     data = {

#         "messaging_product":
#         "whatsapp",

#         "to":
#         phone,

#         "type":
#         "text",

#         "text":
#         {
#             "body":message
#         }

#     }


#     response = requests.post(
#         url,
#         headers=headers,
#         json=data
#     )


#     print(response.json())

import requests
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_ID = os.getenv("PHONE_NUMBER_ID")


def send_message(phone, message):
    url = f"https://graph.facebook.com/v25.0/{PHONE_ID}/messages"

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