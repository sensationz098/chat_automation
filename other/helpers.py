import os
import requests
from dotenv import load_dotenv

load_dotenv()

INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY")


def assign_chat_to_agent(user_phone_number: str, agent_email: str):
    """
    Matches the verified working request:
    POST https://api.interakt.ai/v1/public/assignment/
    {
      "user_phone_number": "919876543210",
      "agent_email": "test.agent@interakt.ai"
    }

    Returns True if the chat ends up assigned to this agent — either
    because the call succeeded, OR because it was already assigned to
    them (Interakt returns a 400 for that case, but it's not actually
    a failure from our side — the desired end state is already true).
    """
    url = "https://api.interakt.ai/v1/public/assignment/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "user_phone_number": user_phone_number,
        "agent_email": agent_email,
    }

    response = requests.post(url, headers=headers, json=payload)
    print("Assignment status:", response.status_code, response.text)

    if response.status_code == 200:
        return True

    if response.status_code == 400 and "already assigned to same agent" in response.text.lower():
        print(f"{agent_email} was already assigned to this chat — no action needed, not a failure.")
        return True

    print(f"Assignment genuinely failed: {response.status_code} {response.text}")
    return False