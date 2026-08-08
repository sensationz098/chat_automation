import os
import re
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = None
try:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if url and key:
        supabase = create_client(url, key)
except Exception as e:
    print(f"[chat_state] Supabase init error: {e}")

# In-memory session store for fast fallback and resilient state management
_memory_sessions: dict[str, dict] = {}


def mark_escalated(phone: str):
    """Marks this phone as handed off to a human — bot stops replying to it."""
    if supabase:
        try:
            supabase.table("escalated_chats").upsert({"phone": phone}).execute()
        except Exception as e:
            print(f"[chat_state] mark_escalated DB error: {e}")
    # Also track in memory
    state = get_user_state(phone)
    state["is_escalated"] = True


def is_escalated(phone: str) -> bool:
    if supabase:
        try:
            result = (
                supabase.table("escalated_chats")
                .select("phone")
                .eq("phone", phone)
                .limit(1)
                .execute()
            )
            if len(result.data) > 0:
                return True
        except Exception as e:
            print(f"[chat_state] is_escalated DB error: {e}")
    
    state = get_user_state(phone)
    return state.get("is_escalated", False)


def clear_escalation(phone: str):
    if supabase:
        try:
            supabase.table("escalated_chats").delete().eq("phone", phone).execute()
        except Exception as e:
            print(f"[chat_state] clear_escalation DB error: {e}")
    state = get_user_state(phone)
    state["is_escalated"] = False


# --- Session State Management ---

def get_default_state(phone: str) -> dict:
    return {
        "phone": phone,
        "stage": "NEW",  # NEW, TIMING_SELECTED, PACKAGE_SELECTED, READY_FOR_APP_LINK, APP_LINK_SENT, PROFILE_COMPLETED, COUPON_SENT
        "timing": None,
        "package": None,
        "fee": None,
        "app_installed": False,
        "profile_created": False,
        "coupon_sent": False,
        "is_escalated": False
    }


def get_user_state(phone: str) -> dict:
    if phone in _memory_sessions:
        return _memory_sessions[phone]

    state = get_default_state(phone)
    if supabase:
        try:
            res = supabase.table("user_session_state").select("*").eq("phone", phone).limit(1).execute()
            if res.data and len(res.data) > 0:
                data = res.data[0]
                state.update(data)
        except Exception as e:
            # Table might not exist yet; gracefully fallback to memory
            pass

    _memory_sessions[phone] = state
    return state


def save_user_state(phone: str, state: dict):
    _memory_sessions[phone] = state
    if supabase:
        try:
            supabase.table("user_session_state").upsert(state).execute()
        except Exception as e:
            # Silently fallback to in-memory if DB table not set up
            pass


def is_user_asking_question(text: str) -> bool:
    """
    Returns True if user input is an informational question rather than
    a simple procedural step.
    """
    txt = text.lower().strip()
    if "?" in txt:
        return True
    
    question_keywords = [
        "who", "what", "where", "how", "why", "which", "when",
        "kitne", "kitna", "kya", "kaun", "kaunsa", "kaunsi", "kaise",
        "teacher", "teachers", "faculty", "student", "students",
        "discount", "costly", "expensive", "syllabus", "demo", "trial",
        "hindi", "english", "classes", "fees"
    ]
    
    words = txt.split()
    if len(words) >= 3 and any(kw in txt for kw in question_keywords):
        return True
        
    return False


def extract_and_update_slots(phone: str, text: str) -> dict:
    """
    Analyzes incoming user message and updates slot values & funnel stage.
    """
    state = get_user_state(phone)
    text_lower = text.lower().strip()
    is_q = is_user_asking_question(text)

    # --- 1. Detect Batch Timing Slot ---
    # Allow user input to set/update timing dynamically
    if any(t in text_lower for t in ["10-11am", "10-11 am", "10 to 11", "10:00", "10 am", "10am"]):
        state["timing"] = "10:00–11:00 AM"
    elif any(t in text_lower for t in ["12-1pm", "12-1 pm", "12 to 1", "12:00", "12 pm", "12pm"]):
        state["timing"] = "12:00–1:00 PM"
    elif any(t in text_lower for t in ["6-7am", "6-7 am", "6 to 7 am", "6:00 am", "6:00am"]):
        state["timing"] = "6:00–7:00 AM"
    elif any(t in text_lower for t in ["7-8am", "7-8 am", "7 to 8 am", "7:00 am", "7:00am"]):
        state["timing"] = "7:00–8:00 AM"
    elif any(t in text_lower for t in ["8-9am", "8-9 am", "8 to 9 am", "8:00 am", "8:00am"]):
        state["timing"] = "8:00–9:00 AM"
    elif any(t in text_lower for t in ["4-5pm", "4-5 pm", "4 to 5", "4:00 pm", "4:00pm"]):
        state["timing"] = "4:00–5:00 PM"
    elif any(t in text_lower for t in ["5-6pm", "5-6 pm", "5 to 6", "5:00 pm", "5:00pm"]):
        state["timing"] = "5:00–6:00 PM"
    elif any(t in text_lower for t in ["6-7pm", "6-7 pm", "6 to 7 pm", "6:00 pm", "6:00pm"]):
        state["timing"] = "6:00–7:00 PM"
    elif any(t in text_lower for t in ["7-8pm", "7-8 pm", "7 to 8 pm", "7:00 pm", "7:00pm"]):
        state["timing"] = "7:00–8:00 PM"
    elif "6-7" in text_lower and not is_q:
        state["timing"] = "6:00–7:00 AM"
    elif "7-8" in text_lower and not is_q:
        state["timing"] = "7:00–8:00 AM"
    elif "8-9" in text_lower and not is_q:
        state["timing"] = "8:00–9:00 AM"
    elif "10-11" in text_lower and not is_q:
        state["timing"] = "10:00–11:00 AM"
    elif "12-1" in text_lower and not is_q:
        state["timing"] = "12:00–1:00 PM"

    # --- 2. Detect Package / Duration Slot ---
    if any(p in text_lower for p in ["1 year", "yearly", "1yr", "1 y", "5000", "₹5000", "5,000", "₹5,000"]):
        state["package"] = "1 Year"
        state["fee"] = "₹5,000"
    elif any(p in text_lower for p in ["6 month", "6m", "3200", "₹3200", "3,200", "₹3,200"]):
        state["package"] = "6 Months"
        state["fee"] = "₹3,200"
    elif any(p in text_lower for p in ["3 month", "3m", "1750", "₹1750", "1,750", "₹1,750"]):
        state["package"] = "3 Months"
        state["fee"] = "₹1,750"
    elif any(p in text_lower for p in ["1 month", "1m", "700", "₹700"]):
        state["package"] = "1 Month"
        state["fee"] = "₹700"

    # --- 3. Determine Stage Transition ---
    if state["timing"] and state["package"]:
        if not state["profile_created"] and not is_q:
            if state["stage"] in ["NEW", "TIMING_SELECTED", "PACKAGE_SELECTED", "APP_LINK_SENT"]:
                if any(w in text_lower for w in ["yes", "enroll", "join", "confirm", "proceed", "link", "app", "ok", "sure"]) or state["stage"] != "APP_LINK_SENT":
                    state["stage"] = "READY_FOR_APP_LINK"
    elif state["timing"] and not state["package"]:
        if state["stage"] == "NEW":
            state["stage"] = "TIMING_SELECTED"
    elif state["package"] and not state["timing"]:
        if state["stage"] == "NEW":
            state["stage"] = "PACKAGE_SELECTED"

    # --- 4. Detect App Install & Profile Completion ---
    if state["stage"] in ["APP_LINK_SENT", "READY_FOR_APP_LINK"]:
        if any(w in text_lower for w in ["profile created", "both done", "profile done", "profile complete", "created profile"]):
            state["app_installed"] = True
            state["profile_created"] = True
            state["stage"] = "PROFILE_COMPLETED"
        elif any(w in text_lower for w in ["installed", "downloaded", "done app", "app done"]) and not state["profile_created"]:
            state["app_installed"] = True

    save_user_state(phone, state)
    return state