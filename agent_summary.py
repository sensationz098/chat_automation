import os
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from interakt import send_text_message_async
from chat_history import get_recent_history
from chat_state import get_user_state

load_dotenv()

# Agent number pool -- index-aligned with AGENT_POOL emails in tasks.py
# AGENT_POOL[0] = PRIORITY_AGENT_EMAIL_ANOTHER_1 -> AGENT_NUMBER_POOL[0] = PRIORITY_AGENT_NUMBER_ANOTHER_1
# AGENT_POOL[1] = PRIORITY_AGENT_EMAIL_ANOTHER_2 -> AGENT_NUMBER_POOL[1] = PRIORITY_AGENT_NUMBER_ANOTHER_2
AGENT_NUMBER_POOL = [
    e for e in [
        os.getenv("PRIORITY_AGENT_NUMBER_ANOTHER_1"),
        os.getenv("PRIORITY_AGENT_NUMBER_ANOTHER_2"),
    ] if e
]

_llm = ChatOpenAI(
    model="gpt-5.6-luna",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0.5,
    timeout=20,
    max_retries=1,
)

_SUMMARY_SYSTEM = """Tu ek helpful assistant hai jo Sensationz Yoga ke agent ko customer handoff summary bhejta hai.

Tujhe last kuch messages dekh ke ek SHORT summary banana hai -- sirf 3-4 bullet points mein, Hinglish mein.

Summary mein yeh include kar:
- Customer ka main concern / sawaal kya tha
- Enrollment mein kahan tak pahunche (timing/package select hua ya nahi)
- Customer ka mood/interest level (interested / confused / disinterested)
- Agent ko kya karna chahiye (next step / follow-up action)

Format:
*Customer Summary*

- [point 1]
- [point 2]
- [point 3]
- [point 4]

Rules:
- Sirf Hinglish mein likho (Hindi + English mix)
- Bilkul short rakho -- agent busy hai
- Phone number mat likho
- Koi extra explanation mat do, seedha bullet points
"""


async def _generate_summary_llm(history: list, state: dict, escalation_reason: str) -> str:
    """Uses LLM to generate a 3-4 point Hinglish summary of the conversation."""
    chat_lines = []
    for turn in history[-10:]:
        role = "Customer" if turn.get("role") == "user" else "Bot"
        content = (turn.get("content") or "")[:300]
        chat_lines.append(f"{role}: {content}")
    chat_text = "\n".join(chat_lines) if chat_lines else "No conversation history."

    stage = state.get("stage", "NEW")
    timing = state.get("timing") or "Not selected"
    package = state.get("package") or "Not selected"
    fee = state.get("fee") or "N/A"
    app_installed = "Yes" if state.get("app_installed") else "No"
    profile_created = "Yes" if state.get("profile_created") else "No"

    user_prompt = f"""Escalation reason: {escalation_reason}

Funnel Stage: {stage}
Timing Selected: {timing}
Package Selected: {package} ({fee})
App Installed: {app_installed}
Profile Created: {profile_created}

Recent Conversation:
{chat_text}

Ab 3-4 bullet points mein Hinglish summary do."""

    try:
        response = await _llm.ainvoke([
            SystemMessage(content=_SUMMARY_SYSTEM),
            HumanMessage(content=user_prompt),
        ])
        return response.content.strip()
    except Exception as e:
        print(f"[agent_summary] LLM summary failed: {e}")
        return (
            f"*Customer Summary*\n\n"
            f"- Funnel Stage: {stage}\n"
            f"- Timing: {timing} | Package: {package} ({fee})\n"
            f"- App Installed: {app_installed} | Profile: {profile_created}\n"
            f"- Escalation reason: {escalation_reason}"
        )


async def send_agent_summary_async(
    customer_phone: str,
    agent_index: int,
    escalation_reason: str = "User requested agent",
):
    """
    Generates a Hinglish summary of the customer conversation
    and sends it to the matching agent WhatsApp number.

    agent_index: 0 -> PRIORITY_AGENT_NUMBER_ANOTHER_1
                 1 -> PRIORITY_AGENT_NUMBER_ANOTHER_2
    """
    if not AGENT_NUMBER_POOL:
        print("[agent_summary] No agent numbers configured -- skipping summary")
        return

    idx = agent_index % len(AGENT_NUMBER_POOL)
    agent_number = AGENT_NUMBER_POOL[idx]

    try:
        history = get_recent_history(customer_phone, limit=10)
        state = get_user_state(customer_phone)
    except Exception as e:
        print(f"[agent_summary] Failed to fetch history/state: {e}")
        history = []
        state = {}

    summary = await _generate_summary_llm(history, state, escalation_reason)

    message = (
        f"*New Customer Assigned*\n"
        f"Customer: +{customer_phone}\n\n"
        f"{summary}\n\n"
        f"Interakt mein open karo aur reply karo."
        f"This just for AI testing purpose"
    )
    print("All send to my agent", message)
    print(f"[agent_summary] Sending summary to agent {agent_number} (index={idx})")
    try:
        await send_text_message_async(agent_number, message)
    except Exception as e:
        print(f"[agent_summary] Failed to send summary to agent: {e}")

# """
# agent_summary.py -- Generates a short Hinglish conversation summary
# and emails it to the assigned agent's Gmail at handoff time.

# Called from:
#   - tasks.py -> handle_agent_handoff_async()  (user typed "agent")
#   - follow_up_worker.py -> count==1 escalation (2 unanswered follow-ups)

# Required .env variables:
#   GMAIL_SENDER_EMAIL    = your Gmail address (sends FROM this)
#   GMAIL_APP_PASSWORD    = Gmail App Password (not your normal password)
#   PRIORITY_AGENT_EMAIL_ANOTHER_1 = agent 1 Gmail (receives summary)
#   PRIORITY_AGENT_EMAIL_ANOTHER_2 = agent 2 Gmail (receives summary)
# """

# import os
# import asyncio
# import smtplib
# from email.mime.text import MIMEText
# from email.mime.multipart import MIMEMultipart
# from dotenv import load_dotenv
# from langchain_openai import ChatOpenAI
# from langchain_core.messages import SystemMessage, HumanMessage

# from chat_history import get_recent_history
# from chat_state import get_user_state

# load_dotenv()

# # Agent email pool -- index-aligned with AGENT_POOL in tasks.py
# # AGENT_POOL[0] = PRIORITY_AGENT_EMAIL_ANOTHER_1  (assigned email = summary recipient)
# # AGENT_POOL[1] = PRIORITY_AGENT_EMAIL_ANOTHER_2
# AGENT_EMAIL_POOL = [
#     e for e in [
#         os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_1"),
#         os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_2"),
#     ] if e
# ]

# GMAIL_SENDER = os.getenv("GMAIL_SENDER_EMAIL")
# GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# _llm = ChatOpenAI(
#     model="gpt-5.6-luna",
#     api_key=os.getenv("OPENAI_API_KEY"),
#     temperature=0.3,
#     timeout=20,
#     max_retries=1,
# )

# _SUMMARY_SYSTEM = """Tu ek helpful assistant hai jo Sensationz Yoga ke agent ko customer handoff summary bhejta hai.

# Tujhe last kuch messages dekh ke ek SHORT summary banana hai -- sirf 3-4 bullet points mein, Hinglish mein.

# Summary mein yeh include kar:
# - Customer ka main concern / sawaal kya tha
# - Enrollment mein kahan tak pahunche (timing/package select hua ya nahi)
# - Customer ka mood/interest level (interested / confused / disinterested)
# - Agent ko kya karna chahiye (next step / follow-up action)

# Format:
# Customer Summary

# - [point 1]
# - [point 2]
# - [point 3]
# - [point 4]

# Rules:
# - Sirf Hinglish mein likho (Hindi + English mix)
# - Bilkul short rakho -- agent busy hai
# - Phone number mat likho
# - Koi extra explanation mat do, seedha bullet points
# """


# async def _generate_summary_llm(history: list, state: dict, escalation_reason: str) -> str:
#     """Uses LLM to generate a 3-4 point Hinglish summary of the conversation."""
#     chat_lines = []
#     for turn in history[-10:]:
#         role = "Customer" if turn.get("role") == "user" else "Bot"
#         content = (turn.get("content") or "")[:300]
#         chat_lines.append(f"{role}: {content}")
#     chat_text = "\n".join(chat_lines) if chat_lines else "No conversation history."

#     stage = state.get("stage", "NEW")
#     timing = state.get("timing") or "Not selected"
#     package = state.get("package") or "Not selected"
#     fee = state.get("fee") or "N/A"
#     app_installed = "Yes" if state.get("app_installed") else "No"
#     profile_created = "Yes" if state.get("profile_created") else "No"

#     user_prompt = f"""Escalation reason: {escalation_reason}

# Funnel Stage: {stage}
# Timing Selected: {timing}
# Package Selected: {package} ({fee})
# App Installed: {app_installed}
# Profile Created: {profile_created}

# Recent Conversation:
# {chat_text}

# Ab 3-4 bullet points mein Hinglish summary do."""

#     try:
#         response = await _llm.ainvoke([
#             SystemMessage(content=_SUMMARY_SYSTEM),
#             HumanMessage(content=user_prompt),
#         ])
#         return response.content.strip()
#     except Exception as e:
#         print(f"[agent_summary] LLM summary failed: {e}")
#         # Fallback plain text summary
#         return (
#             f"Customer Summary\n\n"
#             f"- Funnel Stage: {stage}\n"
#             f"- Timing: {timing} | Package: {package} ({fee})\n"
#             f"- App Installed: {app_installed} | Profile: {profile_created}\n"
#             f"- Escalation reason: {escalation_reason}"
#         )


# def _send_email_sync(to_email: str, customer_phone: str, summary: str, escalation_reason: str):
#     """Sends the summary email via Gmail SMTP (runs in thread pool to stay non-blocking)."""
#     if not GMAIL_SENDER or not GMAIL_APP_PASSWORD:
#         print("[agent_summary] GMAIL_SENDER_EMAIL or GMAIL_APP_PASSWORD not set — skipping email")
#         return

#     subject = f"🔔 New Customer Assigned — +{customer_phone}"

#     body = f"""Namaskar!

# Ek naya customer aapko assign kiya gaya hai. Neeche unka short summary hai:

# Customer Phone: +{customer_phone}
# Escalation Reason: {escalation_reason}

# ---

# {summary}

# ---

# Interakt mein login karke is customer ka chat open karein aur reply karein.

# — Sensationz AI Bot
# """

#     msg = MIMEMultipart()
#     msg["From"] = GMAIL_SENDER
#     msg["To"] = to_email
#     msg["Subject"] = subject
#     msg.attach(MIMEText(body, "plain", "utf-8"))

#     try:
#         with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
#             server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
#             server.sendmail(GMAIL_SENDER, to_email, msg.as_string())
#         print(f"[agent_summary] Email sent to {to_email}")
#     except Exception as e:
#         print(f"[agent_summary] Failed to send email to {to_email}: {e}")


# async def send_agent_summary_async(
#     customer_phone: str,
#     agent_index: int,
#     escalation_reason: str = "User requested agent",
# ):
#     """
#     Generates a Hinglish summary and emails it to the assigned agent.

#     agent_index: 0 -> PRIORITY_AGENT_EMAIL_ANOTHER_1
#                  1 -> PRIORITY_AGENT_EMAIL_ANOTHER_2
#     """
#     if not AGENT_EMAIL_POOL:
#         print("[agent_summary] No agent emails configured — skipping summary")
#         return

#     idx = agent_index % len(AGENT_EMAIL_POOL)
#     agent_email = AGENT_EMAIL_POOL[idx]

#     try:
#         history = get_recent_history(customer_phone, limit=10)
#         state = get_user_state(customer_phone)
#     except Exception as e:
#         print(f"[agent_summary] Failed to fetch history/state: {e}")
#         history = []
#         state = {}

#     summary = await _generate_summary_llm(history, state, escalation_reason)

#     print(f"[agent_summary] Sending email summary to {agent_email} (index={idx})")

#     # Run SMTP (blocking) in a thread so it doesn't block the event loop
#     loop = asyncio.get_event_loop()
#     await loop.run_in_executor(
#         None,
#         _send_email_sync,
#         agent_email,
#         customer_phone,
#         summary,
#         escalation_reason,
#     )

