import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def set_agent_online(email: str, is_online: bool):
    """
    Explicitly marks an agent online or offline in Supabase. Call this
    directly (via the /agent-status endpoint, or a small toggle page)
    whenever an agent starts/ends their shift.
    """
    result = (
        supabase.table("agents")
        .update({"is_online": is_online})
        .eq("email", email)
        .execute()
    )
    print(f"[agent_status] Set {email} -> is_online={is_online}. Rows affected: {len(result.data)}")
    return result.data


def get_next_online_agent(priority_email: str = None):
    """
    Picks who to assign a chat to, based purely on the is_online flag
    in Supabase:
    1. Get every agent currently marked is_online = True.
    2. Among those, pick whoever waited longest since their last
       assignment (round-robin) — no special preference for
       priority_email, they're just one candidate like anyone else.
    3. Only if NOBODY is online at all -> fall back to priority_email
       anyway, so a chat is never left unassigned.
       Returns None if nobody is online AND no priority_email given.
    """
    online_agents = (
        supabase.table("agents")
        .select("email, last_assigned_at")
        .eq("is_online", True)
        .order("last_assigned_at", desc=False)
        .execute()
        .data
    )

    if online_agents:
        chosen_email = online_agents[0]["email"]
        print(f"[agent_status] {chosen_email} is ONLINE (is_online=True in Supabase) — picked "
              f"(round-robin among {len(online_agents)} online agent(s)).")
        _touch_last_assigned(chosen_email)
        return chosen_email

    if priority_email:
        print(f"[agent_status] NOBODY marked online in Supabase. Falling back to "
              f"{priority_email} anyway (guaranteed fallback).")
        _touch_last_assigned(priority_email)
        return priority_email

    print("[agent_status] Nobody online and no priority_email set — returning None.")
    return None


def _touch_last_assigned(email: str):
    supabase.table("agents").update(
        {"last_assigned_at": datetime.now(timezone.utc).isoformat()}
    ).eq("email", email).execute()