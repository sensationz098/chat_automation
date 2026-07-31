from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse

import os
import time

from dotenv import load_dotenv

from rag import ask_rag, stream_rag
from whatsapp import send_message, show_typing, send_button_message, send_call_button


load_dotenv()

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

SUPPORT_NOTIFY_NUMBER = os.getenv("SUPPORT_NOTIFY_NUMBER")

AGENT_CALL_NUMBER = os.getenv("AGENT_CALL_NUMBER")

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """
    Simple privacy policy page, required by Meta before an app can be
    switched to Live mode. Replace the placeholder details below with
    your actual company/contact info before using this for a real launch.
    """
    return """
    <html>
      <head><title>Privacy Policy - abc.com</title></head>
      <body style="font-family: sans-serif; max-width: 700px; margin: 40px auto; line-height: 1.6;">
        <h1>Privacy Policy</h1>
        <p>abc.com ("we", "us") operates a WhatsApp-based ordering
        assistant. This policy explains what information we collect and
        how we use it.</p>

        <h2>Information We Collect</h2>
        <p>When you message our WhatsApp number, we receive your phone
        number and the content of your messages, in order to respond to
        your requests, look up products, and process orders.</p>

        <h2>How We Use Your Information</h2>
        <p>We use this information solely to respond to your messages,
        manage your cart and orders, and improve our service. We do not
        sell your information to third parties.</p>

        <h2>Data Retention</h2>
        <p>Message and order data is retained only as long as necessary
        to provide the service.</p>

        <h2>Contact</h2>
        <p>For questions about this policy, contact us at
        support@abc.com.</p>
      </body>
    </html>
    """


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=None),
    hub_challenge: str = Query(alias="hub.challenge", default=None),
    hub_verify_token: str = Query(alias="hub.verify_token", default=None),
):
    """
    Meta calls this once when you click 'Verify and Save' in the
    Configuration tab. Without this route, webhook verification
    fails with no useful error message.
    """
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    return PlainTextResponse(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    print("Webhook received!")
    print(data)

    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]

        user_phone = message["from"]
        message_id = message["id"]
        msg_type = message.get("type")

        if msg_type == "interactive":
            button_id = message["interactive"]["button_reply"]["id"]
            print("Phone:", user_phone, "| Button tapped:", button_id)
            background_tasks.add_task(handle_button_tap, user_phone, button_id, message_id)
        else:
            user_text = message["text"]["body"]
            print("Phone:", user_phone, "| Message:", user_text)
            background_tasks.add_task(process_message, user_phone, user_text, message_id)

    except Exception:
        import traceback
        traceback.print_exc()

    return {"status": "ok"}


def handle_button_tap(user_phone: str, button_id: str, message_id: str):
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
                "No problem — I've notified a member of our team. "
                "Someone will message you here shortly!"
            )
        if SUPPORT_NOTIFY_NUMBER:
            send_message(
                SUPPORT_NOTIFY_NUMBER,
                f"Customer {user_phone} has requested to speak with an agent."
            )
    else:
        send_message(user_phone, "Got it! Let me know what you're looking for.")


def process_message(user_phone: str, user_text: str, message_id: str):
    """
    Runs in the background, after the webhook has already returned.
    Any failure here won't affect Meta's view of your webhook at all.
    """
    try:
        show_typing(message_id)

        if any(word in user_text.lower() for word in AGENT_TRIGGER_WORDS):
            send_button_message(
                user_phone,
                "Would you like to talk to a human agent?",
                [("talk_agent", "Talk to Agent"), ("continue_bot", "Continue with Bot")]
            )
            return

        try:
            chunk_count = 0
            for chunk_text in stream_rag(user_text):
                # Small pause before each bubble so it reads like someone
                # typing separate messages, not a burst-fire spam of texts.
                if chunk_count > 0:
                    time.sleep(min(0.8 + len(chunk_text) / 150, 2.5))
                    show_typing(message_id)
                send_message(user_phone, chunk_text)
                chunk_count += 1

        except Exception:
            import traceback
            traceback.print_exc()
            send_message(user_phone, "Hello from FastAPI! (RAG unavailable right now)")

    except Exception:
        import traceback
        traceback.print_exc()