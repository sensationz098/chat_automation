"""
Standalone test: sends one WhatsApp message directly, no webhook involved.
Use this to confirm your WHATSAPP_TOKEN and PHONE_NUMBER_ID actually work,
separate from anything related to receiving messages or RAG.

Usage:
    python test_send.py
"""

import os
from dotenv import load_dotenv
from whatsapp import send_message

load_dotenv()

TO_NUMBER = "+917004642721"

if __name__ == "__main__":
    if TO_NUMBER == "PUT_YOUR_TEST_NUMBER_HERE":
        print("Edit TO_NUMBER in this file first — set it to your verified "
              "test recipient number, e.g. '919876543210'.")
    else:
        response = send_message(TO_NUMBER, "Hello! This is a direct test message from InstaKart.")
        if response.status_code == 200:
            print("Sent successfully — check your WhatsApp.")
        else:
            print("Send failed — see Status/Response above for the reason.")

print("TOKEN loaded:", bool(os.getenv("WHATSAPP_TOKEN")))
print("PHONE_NUMBER_ID:", os.getenv("PHONE_NUMBER_ID"))