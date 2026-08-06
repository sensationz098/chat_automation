import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# def mark_escalated(phone: str):
#     """Marks this phone as handed off to a human — bot stops replying to it."""
#     supabase.table("escalated_chats").upsert({"phone": phone}).execute()


# def is_escalated(phone: str) -> bool:
#     result = (
#         supabase.table("escalated_chats")
#         .select("phone")
#         .eq("phone", phone)
#         .limit(1)
#         .execute()
#     )
#     return len(result.data) > 0

def mark_escalated(phone: str):
    result = supabase.table("escalated_chats").upsert({"phone": phone}).execute()
    print("[chat_state] mark_escalated:", phone, "->", result.data)


def is_escalated(phone: str) -> bool:
    result = (
        supabase.table("escalated_chats")
        .select("phone")
        .eq("phone", phone)
        .limit(1)
        .execute()
    )
    print("[chat_state] is_escalated:", phone, "->", result.data)
    return len(result.data) > 0


def clear_escalation(phone: str):
    """
    Call this once the human agent has resolved the conversation, to
    hand control back to the bot for this customer's future messages.
    """
    supabase.table("escalated_chats").delete().eq("phone", phone).execute()