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
        "aliases": ["1 month", "1m", "1", "one month", "monthly", "1 mahina"],
    },
    "3 Months": {
        "code": "YOGA600",
        "base_price": 1750,
        "offer_price": 600,
        "currency": "₹",
        "aliases": ["3 months", "3 month", "3m", "3", "three months", "quarterly", "3 mahine"],
    },
    "6 Months": {
        "code": "YOGA1000",
        "base_price": 3200,
        "offer_price": 1000,
        "currency": "₹",
        "aliases": ["6 months", "6 month", "6m", "6", "six months", "half yearly", "6 mahine"],
    },
    "1 Year": {
        "code": "YOGA1800",
        "base_price": 5000,
        "offer_price": 1800,
        "currency": "₹",
        "aliases": ["1 year", "12 months", "12 month", "1y", "1yr", "12", "yearly", "annual", "1 saal"],
    },
}

# Mapping of normalized package names to formatted fee strings
VALID_PACKAGES: Dict[str, str] = {
    pkg: f"{data['currency']}{data['base_price']:,} (Offer Price: {data['currency']}{data['offer_price']:,})"
    for pkg, data in COUPON_REGISTRY.items()
}


def normalize_package_name(text: str) -> Optional[str]:
    """Finds the matching canonical package name from any string or alias."""
    if not text:
        return None
    t = text.strip().lower()
    for canonical_name, data in COUPON_REGISTRY.items():
        if t == canonical_name.lower():
            return canonical_name
        for alias in data.get("aliases", []):
            if t == alias or alias in t:
                return canonical_name
    return None


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
    """
    details = get_coupon_details(package_name)
    if details:
        canonical = normalize_package_name(package_name)
        return f"*{details['code']}* (Strictly for {canonical} duration)"
    
    # If package not yet selected, show all available coupon codes dynamically
    summary_parts = [
        f"{pkg}: *{data['code']}*"
        for pkg, data in COUPON_REGISTRY.items()
    ]
    return " | ".join(summary_parts)


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
