import requests

WEBHOOK_URL = "http://localhost:8000/test-webhook"

TARGET_CAMPAIGN_ID = 120212206990640010
TARGET_AD_ID = 120247632705360010
TARGET_TEXT = "hello! can i get more info on yoga classes?"

# Both numbers now start with '919800' to pass the ALLOWED_TEST_NUMBERS check
payload_organic = {
    "type": "message_received",
    "data": {
        "customer": {
            "country_code": "91",
            "phone_number": "919800000001"
        },
        "message": {
            "message": "What are your class timings?"
        }
    }
}

payload_ad_lead = {
    "type": "message_received",
    "data": {
        "customer": {
            "country_code": "91",
            "phone_number": "919800000002"
        },
        "message": {
            "message": TARGET_TEXT,
            "referral": {
                "source_id": TARGET_AD_ID,
                "source_type": "ad",
                "source_url": f"https://fb.me/12345?selected_campaign_ids={TARGET_CAMPAIGN_ID}&selected_ad_ids={TARGET_AD_ID}",
                "headline": "Special Yoga Offer"
            }
        }
    }
}

if __name__ == "__main__":
    print("Sending Standard Organic Message...")
    r1 = requests.post(WEBHOOK_URL, json=payload_organic)
    print("Response:", r1.json())

    print("\nSending Target FB Ad Message...")
    r2 = requests.post(WEBHOOK_URL, json=payload_ad_lead)
    print("Response:", r2.json())