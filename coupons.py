"""
coupons.py — Centralized, Dynamic Coupon and Package Configuration Registry.
Provides single source of truth for packages, base fees, offer prices, coupon codes,
and dynamic formatting across the entire chatbot application.
"""

from typing import Optional, Dict, Any, List
import re

# Central registry for all course packages, pricing, and associated discount coupons
COUPON_REGISTRY: Dict[str, Dict[str, Any]] = {
    "1 Month": {
        "code": "YOGA300",
        "base_price": 700,
        "offer_price": 300,
        "currency": "₹",
        "aliases": ["1 month", "1m", "one month", "monthly", "1 mahina", "yoga300"],
    },
    "3 Months": {
        "code": "YOGA600",
        "base_price": 1750,
        "offer_price": 600,
        "currency": "₹",
        "aliases": ["3 months", "3 month", "3m", "three months", "quarterly", "3 mahine", "yoga600"],
    },
    "6 Months": {
        "code": "YOGA1000",
        "base_price": 3200,
        "offer_price": 1000,
        "currency": "₹",
        "aliases": ["6 months", "6 month", "6m", "six months", "half yearly", "6 mahine", "yoga1000"],
    },
    "1 Year": {
        "code": "YOGA1800",
        "base_price": 5000,
        "offer_price": 1800,
        "currency": "₹",
        "aliases": ["1 year", "12 months", "12 month", "1y", "1yr", "yearly", "annual", "1 saal", "12 mahine", "yoga1800"],
    },
}

# Mapping of normalized package names to formatted fee strings
VALID_PACKAGES: Dict[str, str] = {
    pkg: f"{data['currency']}{data['base_price']:,} (Offer Price: {data['currency']}{data['offer_price']:,})"
    for pkg, data in COUPON_REGISTRY.items()
}


def detect_package_from_text(text: Optional[str]) -> Optional[str]:
    """
    Robustly identifies a course package duration (1 Month, 3 Months, 6 Months, 1 Year)
    from user text, handling English, Hinglish, slang, coupon requests, and price mentions.
    Guards against misinterpreting class timings (e.g. '6:00 AM', '1:00 PM', '10:00 AM').
    """
    if not text:
        return None
    # Normalize whitespaces and newlines
    t = re.sub(r"\s+", " ", text.strip().lower())

    # 1. Check exact canonical match first
    for canonical_name in COUPON_REGISTRY.keys():
        if t == canonical_name.lower():
            return canonical_name

    # 2. Check 1 Year / 12 Months (Checked first to prevent '1' or '12' collision with '1 Month')
    if re.search(r"\b(?:12\s*(?:months?|month|m|mahine?)|1\s*(?:years?|year|yr|yrs|y|saal)|one\s*year|ek\s*saal|yearly|annual|annually|yoga1800|₹\s*5,?000|₹\s*1,?800)\b", t):
        return "1 Year"
    if re.search(r"\b(?:5000|1800)\s*(?:rs|rupees|ka|wali|wala|package|plan)?\b", t) and len(t) <= 25:
        return "1 Year"
    if re.match(r"^(?:12|12m|1y|1yr|1 year)$", t):
        return "1 Year"

    # 3. Check 6 Months
    if re.search(r"\b(?:6\s*(?:months?|month|m|mahine?)|six\s*months?|half\s*yearly|semi\s*annual|semi\s*annually|yoga1000|₹\s*3,?200|₹\s*1,?000)\b", t):
        return "6 Months"
    if re.search(r"\b(?:3200|1000)\s*(?:rs|rupees|ka|wali|wala|package|plan)?\b", t) and len(t) <= 25:
        return "6 Months"
    if re.match(r"^(?:6|6m|6 month|6 months)$", t):
        return "6 Months"

    # 4. Check 3 Months
    if re.search(r"\b(?:3\s*(?:months?|month|m|mahine?)|three\s*months?|quarterly|yoga600|₹\s*1,?750|₹\s*600)\b", t):
        return "3 Months"
    if re.search(r"\b(?:1750|600)\s*(?:rs|rupees|ka|wali|wala|package|plan)?\b", t) and len(t) <= 25:
        return "3 Months"
    if re.match(r"^(?:3|3m|3 month|3 months)$", t):
        return "3 Months"

    # 5. Check 1 Month (Guard against '1:00 pm', '1 pm', '10:00 am', '12:00 pm')
    if not re.search(r"\b1(?::00)?\s*(?:pm|am)\b", t):
        if re.search(r"\b(?:1\s*(?:months?|month|m|mahina|mahine)|one\s*month|monthly|yoga300|₹\s*700|₹\s*300)\b", t):
            return "1 Month"
        if re.search(r"\b(?:700|300)\s*(?:rs|rupees|ka|wali|wala|package|plan)?\b", t) and len(t) <= 25:
            return "1 Month"
        if re.match(r"^(?:1|1m|1 month)$", t):
            return "1 Month"

    return None


def normalize_package_name(text: Optional[str]) -> Optional[str]:
    """Finds the matching canonical package name from any string, alias, or sentence."""
    if not text:
        return None
    t = text.strip()
    for canonical_name in COUPON_REGISTRY.keys():
        if t.lower() == canonical_name.lower():
            return canonical_name
    return detect_package_from_text(t)


def get_coupon_details(package_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Returns the coupon details dict for a given package name."""
    if not package_name:
        return None
    canonical = normalize_package_name(package_name)
    if canonical and canonical in COUPON_REGISTRY:
        return COUPON_REGISTRY[canonical]
    return None


def get_all_coupon_codes() -> List[str]:
    """Returns a list of all active coupon codes in the registry."""
    return [data["code"] for data in COUPON_REGISTRY.values() if data.get("code")]


def get_coupon_code_for_package(package_name: Optional[str]) -> Optional[str]:
    """Returns the coupon code string (e.g. 'YOGA300') for the specified package."""
    details = get_coupon_details(package_name)
    return details["code"] if details else None


def format_coupon_prompt_string(package_name: Optional[str]) -> str:
    """
    Constructs the dynamic coupon instruction string for system prompts in prompt.py.
    Provides the active package code AND lists all available codes so the LLM never
    claims other packages don't have coupons.
    """
    all_codes_summary = ", ".join(f"{pkg}: *{data['code']}* (Offer: {data['currency']}{data['offer_price']:,})" for pkg, data in COUPON_REGISTRY.items())
    details = get_coupon_details(package_name)
    if details:
        canonical = normalize_package_name(package_name)
        return (
            f"Active Selected Package: {canonical} -> Code *{details['code']}* (Offer Price: {details['currency']}{details['offer_price']:,}).\n"
            f"   (ALL Package Coupon Codes: {all_codes_summary}.\n"
            f"   RULE: If the customer asks about or chooses any different package duration, ALWAYS give the exact coupon code and offer price corresponding to that package!)"
        )
    return all_codes_summary


def format_coupon_banner(
    package_name: Optional[str],
    language: str = "hinglish",
    is_frustrated: bool = False
) -> str:
    """
    Generates the dynamic welcome coupon delivery message.
    """
    details = get_coupon_details(package_name)
    canonical = normalize_package_name(package_name)

    if details:
        code = details["code"]
        code_details_hi = f"✨ Coupon Code: *{code}* ({canonical} package ke liye)"
        code_details_en = f"✨ Coupon Code: *{code}* (for {canonical} package)"
    else:
        # Fallback list of all packages
        lines = [f"• {pkg} duration: *{data['code']}*" for pkg, data in COUPON_REGISTRY.items()]
        code_details_hi = "\n".join(lines)
        code_details_en = code_details_hi

    if is_frustrated:
        return (
            "Bilkul denge! Hum daily live yoga classes conduct karte hain 😊\n\n"
            "Ye raha aapka special welcome discount coupon code 🎁\n\n"
            f"{code_details_hi}\n\n"
            "Isko Sensationz App ya website checkout par enter karke apply karein. Class mein milte hain! 🧘‍♀️"
        )
    elif language in ["hindi", "hinglish"]:
        return (
            "🎉 Sensationz family mein aapka swagat hai! 🌸\n"
            "Aapka welcome discount coupon code ye raha 🎁\n\n"
            f"{code_details_hi}\n\n"
            "Isko Sensationz App ya website checkout par enter karke apply karein aur offer price activate karein. Class mein milte hain! 🧘‍♀️✨"
        )
    else:
        return (
            "🎉 Welcome to the Sensationz family! 🌸\n"
            "Here is your welcome discount coupon code 🎁\n\n"
            f"{code_details_en}\n\n"
            "Please enter this code during checkout in the Sensationz App or website to activate your discount. See you in class! 🧘‍♀️✨"
        )
