import os
import time
from dotenv import load_dotenv
from rag import stream_rag, split_into_chunks
from whatsapp import send_message, show_typing, send_button_message, send_call_button
from clients import check_cache, save_to_cache

load_dotenv()

SUPPORT_NOTIFY_NUMBER = os.getenv("SUPPORT_NOTIFY_NUMBER")
AGENT_CALL_NUMBER = os.getenv("AGENT_CALL_NUMBER")
AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]


def process_message_job(user_phone: str, user_text: str, message_id: str):
    show_typing(message_id)

    if any(word in user_text.lower() for word in AGENT_TRIGGER_WORDS):
        send_button_message(
            user_phone,
            "Would you like to talk to a human agent?",
            [("talk_agent", "Talk to Agent"), ("continue_bot", "Continue with Bot")]
        )
        return

    cached_answer = check_cache(user_text)
    if cached_answer:
        for i, chunk_text in enumerate(split_into_chunks(cached_answer)):
            if i > 0:
                time.sleep(min(0.8 + len(chunk_text) / 150, 2.5))
                show_typing(message_id)
            send_message(user_phone, chunk_text)
        return

    try:
        full_answer_parts = []
        for i, chunk_text in enumerate(stream_rag(user_text)):
            if i > 0:
                time.sleep(min(0.8 + len(chunk_text) / 150, 2.5))
                show_typing(message_id)
            send_message(user_phone, chunk_text)
            full_answer_parts.append(chunk_text)

        save_to_cache(user_text, " ".join(full_answer_parts))

    except Exception:
        import traceback
        traceback.print_exc()
        send_message(user_phone, "Sorry, I'm having trouble right now — please try again shortly.")


def handle_button_tap_job(user_phone: str, button_id: str, message_id: str):
    show_typing(message_id)

    if button_id == "talk_agent":
        if AGENT_CALL_NUMBER:
            send_call_button(
                user_phone,
                "No problem! You can call our support team directly by tapping below, "
                "or someone will also message you here shortly.",
                "Call Now",
                AGENT_CALL_NUMBER,
            )
        else:
            send_message(
                user_phone,
                "No problem — I've notified a member of our team. Someone will message you here shortly!"
            )
        if SUPPORT_NOTIFY_NUMBER:
            send_message(SUPPORT_NOTIFY_NUMBER, f"Customer {user_phone} has requested to speak with an agent.")
    else:
        send_message(user_phone, "Got it! Let me know what you're looking for.")