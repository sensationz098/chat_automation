"""
intent_classifier.py — Hybrid Intent Classifier (Regex Fast-Path + LLM Fallback).

PURPOSE:
Permanently solves the problem of Hindi/Hinglish misspellings breaking coupon
and profile-completion detection. Instead of endlessly adding regex patterns,
this module uses a two-tier approach:

  Tier 1 (Fast Path): Existing regex detectors run first (zero cost, zero latency).
  Tier 2 (LLM Fallback): If regex misses but the message contains coupon-like or
    profile-like words (detected via fuzzy matching), a lightweight LLM call
    classifies the intent. This catches ANY misspelling or creative phrasing.

USAGE:
    from intent_classifier import classify_coupon_and_profile_intent
    result = await classify_coupon_and_profile_intent(text, state)
    # result = {"is_coupon_request": bool, "is_profile_done": bool, "is_coupon_info": bool}
"""

import re
import json
import os
from rapidfuzz import fuzz
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv

load_dotenv()

# ── Lazy LLM singleton (avoids circular imports with rag.py) ────────────────
_classifier_llm = None

def _get_classifier_llm():
    """Lazy-init a lightweight LLM for intent classification."""
    global _classifier_llm
    if _classifier_llm is None:
        from langchain_openai import ChatOpenAI
        _classifier_llm = ChatOpenAI(
            model="gpt-4.1-nano",
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=10,
            max_retries=1,
        )
    return _classifier_llm


# ── Fuzzy Pre-Filter ────────────────────────────────────────────────────────
# These words/stems trigger an LLM call when the regex misses.

_COUPON_CANONICAL = "coupon"
_COUPON_FUZZY_THRESHOLD = 60  # fuzz.ratio >= 60 → probably means "coupon"
_COUPON_REGEX = re.compile(r"\b[ckq][aeou]{1,3}p[aeou]{0,2}n[a-z]{0,4}\b", re.IGNORECASE)

# Short stems that indicate profile/completion context
_PROFILE_STEMS = [
    "profile", "account", "signup", "register", "login",
    "done", "ready", "complete", "created", "downloaded", "installed",
    "bna", "bana", "banali", "bnali", "banadi", "bnadi",
    "kardi", "krdi", "kardiya", "hogaya", "hogya", "bangaya", "bangya",
    "krlia", "krliya", "karliya", "karlia", "karli", "krli", "kardia", "krdia",
    "nipat", "nipta", "khatam", "kaam", "dono", "finish", "setup",
]

_COMPLETION_VERBS_HINDI = [
    "le hai", "le h", "le he",
    "liya hai", "liya h", "li hai", "li h",
    "chuka", "chuki", "gaya", "gya", "gayi", "gai",
    "diya", "di hai", "di h",
]


def _has_fuzzy_coupon_word(text: str) -> bool:
    """Check if any word in text fuzzy-matches 'coupon' or follows phonetic coupon variations."""
    words = re.findall(r"\w+", text.lower())
    for w in words:
        if _COUPON_REGEX.search(w):
            return True
        if len(w) >= 4 and (fuzz.ratio(w, _COUPON_CANONICAL) >= _COUPON_FUZZY_THRESHOLD or fuzz.partial_ratio(_COUPON_CANONICAL, w) >= 70):
            return True
    return False


def _has_profile_like_content(text: str) -> bool:
    """Check if text contains profile/completion-related words."""
    t = text.lower()
    # Check stems
    if any(stem in t for stem in _PROFILE_STEMS):
        return True
    # Check Hindi completion verb phrases
    if any(verb in t for verb in _COMPLETION_VERBS_HINDI):
        return True
    return False


# ── LLM Classification Prompt ──────────────────────────────────────────────

_CLASSIFIER_SYSTEM_PROMPT = """You classify WhatsApp messages for a yoga studio bot. The user may write in Hindi, Hinglish, or English with typos and slang.
Context: Users are instructed to download the app and create their profile to unlock a welcome coupon.

Return ONLY strict JSON with these boolean fields:
{"is_coupon_request": bool, "is_profile_done": bool, "is_coupon_info": bool}

Definitions:
- is_coupon_request: The user WANTS to receive, see, or ask for the coupon code for a package duration. Examples: "coupen bhej do", "mujhe code chahiye", "coupon do", "send my coupon", "1y k liye konsa coupon rhga", "1 year ka coupon kya hai", "which coupon for 1y", "bhai cupanwa dedo", "1y ka coupon bjhdo".
- is_profile_done: The user is STATING they have ALREADY completed their profile/registration/app download/signup or tasks. Examples: "profile bna le hai", "done ho gaya", "banadi h", "created my account", "dono kaam nipat gaye", "sab kar diya". NOT asking how to create.
- is_coupon_info: The user is asking a GENERAL INFORMATIONAL question about coupons (how to apply, GST on coupons, validity, expiry, refund, where to enter code, etc.). NOT asking for the code itself.

IMPORTANT: A message can have MULTIPLE true values (e.g. "profile bna le hai ab coupen bhej do" → is_coupon_request=true AND is_profile_done=true)."""


async def _llm_classify(text: str) -> dict:
    """Call the LLM to classify intent. Returns dict with boolean fields."""
    try:
        llm = _get_classifier_llm()
        resp = await llm.ainvoke([
            SystemMessage(content=_CLASSIFIER_SYSTEM_PROMPT),
            HumanMessage(content=f'Message: "{text}"'),
        ])
        raw = resp.content.strip().strip("`").replace("json\n", "").strip()
        data = json.loads(raw)
        return {
            "is_coupon_request": bool(data.get("is_coupon_request", False)),
            "is_profile_done": bool(data.get("is_profile_done", False)),
            "is_coupon_info": bool(data.get("is_coupon_info", False)),
        }
    except Exception as e:
        print(f"[intent_classifier] LLM fallback error: {e}")
        # On failure, return all False — safe fallback (message goes to RAG)
        return {"is_coupon_request": False, "is_profile_done": False, "is_coupon_info": False}


# ── Public API ──────────────────────────────────────────────────────────────

async def classify_coupon_and_profile_intent(
    text: str,
    state: dict,
    regex_coupon_result: bool,
    regex_profile_result: bool,
    regex_coupon_info_result: bool = False,
) -> dict:
    """
    Hybrid intent classifier. Accepts the EXISTING regex results and enhances
    them with LLM fallback only when needed.

    Args:
        text: The user's raw message text.
        state: The user's conversation state dict.
        regex_coupon_result: Result from existing is_coupon_request().
        regex_profile_result: Result from existing is_profile_completed_signal().
        regex_coupon_info_result: Result from existing is_coupon_info_question().

    Returns:
        {"is_coupon_request": bool, "is_profile_done": bool, "is_coupon_info": bool}
    """
    # ── Tier 1: Check fuzzy hints ──
    has_coupon_hint = _has_fuzzy_coupon_word(text)
    has_profile_hint = _has_profile_like_content(text)

    # If neither coupon nor profile was hinted, and regex found nothing -> fast exit
    if not regex_coupon_result and not regex_profile_result and not regex_coupon_info_result:
        if not has_coupon_hint and not has_profile_hint:
            return {
                "is_coupon_request": False,
                "is_profile_done": False,
                "is_coupon_info": False,
            }

    # ── Tier 2: Check if regex fully satisfied all hinted intents ──
    # We only need LLM if:
    # 1. Coupon was hinted, but regex did not detect coupon request or coupon info
    # 2. Profile was hinted, but regex did not detect profile done
    needs_coupon_llm = has_coupon_hint and (not regex_coupon_result) and (not regex_coupon_info_result)
    needs_profile_llm = has_profile_hint and (not regex_profile_result)

    if not needs_coupon_llm and not needs_profile_llm:
        # Fast path: regex resolved everything that was hinted (zero latency, zero cost)
        return {
            "is_coupon_request": regex_coupon_result,
            "is_profile_done": regex_profile_result,
            "is_coupon_info": regex_coupon_info_result,
        }

    # ── Tier 3: LLM fallback for unresolved hints ──
    print(f"[intent_classifier] LLM fallback triggered for: '{text[:80]}' "
          f"(needs_coupon_llm={needs_coupon_llm}, needs_profile_llm={needs_profile_llm})")
    llm_result = await _llm_classify(text)

    # Combine regex hits with LLM classifications so no detected signal is lost
    return {
        "is_coupon_request": regex_coupon_result or llm_result["is_coupon_request"],
        "is_profile_done": regex_profile_result or llm_result["is_profile_done"],
        "is_coupon_info": regex_coupon_info_result or llm_result["is_coupon_info"],
    }
