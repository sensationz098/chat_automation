import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

MAX_HISTORY_MESSAGES = 30  # how many recent messages to feed back to the AI as context


def save_message(phone: str, role: str, message: str):
    """
    role must be 'user' or 'assistant'. Call this for BOTH the
    customer's message and the bot's reply, so the full back-and-forth
    is captured — this is what lets a human agent read the whole
    conversation later, and what lets the AI understand follow-ups.
    """
    supabase.table("chat_history").insert(
        {"phone": phone, "role": role, "message": message}
    ).execute()


def get_recent_history(phone: str, limit: int = MAX_HISTORY_MESSAGES):
    """
    Returns the most recent messages for this phone number, oldest
    first, in the format LangChain's message classes expect:
    [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    """
    result = (
        supabase.table("chat_history")
        .select("role, message")
        .eq("phone", phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    # Supabase gives us newest-first; reverse so the AI sees them in
    # actual chronological order, which is what makes "add 2 more"
    # correctly refer back to whatever came right before it.
    messages = list(reversed(result.data))
    return [{"role": m["role"], "content": m["message"]} for m in messages]


def get_full_history_for_agent(phone: str):
    """
    Returns the ENTIRE conversation (not capped at MAX_HISTORY_MESSAGES)
    for a human agent to review — e.g. when they pick up a handed-off
    chat and need the full context, not just the AI's short window.
    """
    result = (
        supabase.table("chat_history")
        .select("role, message, created_at")
        .eq("phone", phone)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data