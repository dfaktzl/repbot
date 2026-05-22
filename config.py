"""
Shared configuration for Reputation Bot v3.1
All constants, thresholds, keyword lists, and regex patterns live here.
"""

import os
import re
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

# ─── Bot Token ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ─── Admin IDs (parsed once at startup) ───────────────────────────────────────
ADMIN_IDS: frozenset[int] = frozenset()
_raw = os.getenv("ADMIN_IDS", "")
if _raw:
    ADMIN_IDS = frozenset(
        int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()
    )

# ─── Log Channel ──────────────────────────────────────────────────────────────
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-1003817851175"))

# ─── Gatekeeper Bot Cross-Reference & Enforcement ─────────────────────────────
GATEKEEPER_DB_PATH = os.getenv("GATEKEEPER_DB_PATH", "")
GATEKEEPER_CHANNEL_ID = int(os.getenv("GATEKEEPER_CHANNEL_ID", "-1003885954803"))
SOCIAL_GROUP_ID = int(os.getenv("SOCIAL_GROUP_ID", "-1003769928131"))

# ─── Vouch Rules ──────────────────────────────────────────────────────────────
MIN_ACCOUNT_AGE_HOURS = 48
DAILY_VOUCH_LIMIT = 2
PER_USER_COOLDOWN_HOURS = 36

# ─── Scam Detection ──────────────────────────────────────────────────────────
LONG_MSG_CHARS = 1500
LONG_MSG_COUNT = 3
LONG_MSG_WINDOW_HOURS = 6

# ─── Auto-Scammer Thresholds ─────────────────────────────────────────────────
SCAMMER_STRIKE_LIMIT = 3
SCAMMER_STRIKE_WINDOW = timedelta(hours=24)

# ─── Sentiment Detection ─────────────────────────────────────────────────────
SENTIMENT_MIN_SCORE = 2
SENTIMENT_MIN_WORDS = 3

# If a +vouch comment contains ≥ this many unique negative keywords, flip it to -1
NEGATIVE_CONTENT_OVERRIDE_SCORE = 2

# ─── Admin Panel ──────────────────────────────────────────────────────────────
PANEL_PAGE_SIZE = 5

# ═══════════════════════════════════════════════════════════════════════════════
#  KEYWORD LISTS
# ═══════════════════════════════════════════════════════════════════════════════

BLACKLIST_TERMS = [
    # Drugs
    "cocaine", "heroin", "meth", "methamphetamine", "mdma", "ecstasy", "molly",
    "lsd", "acid", "ketamine", "fentanyl", "xanax", "alprazolam",
    "oxy", "oxycodone", "percs", "percocet", "codeine", "morphine", "opium",
    "tramadol", "hydrocodone", "vicodin", "adderall", "ritalin",
    "shrooms", "psilocybin", "dmt", "pcp", "ghb", "rohypnol",
    "crack", "coke", "speed", "ice", "crystal",
    "weed", "marijuana", "cannabis", "thc", "edibles",
    # Weapons
    "firearm", "handgun", "pistol", "rifle", "shotgun", "ammo", "ammunition",
    # Fraud / Financial Crime
    "carding", "fullz", "dumps", "cvv", "cashout", "counterfeit",
    # Slang / Evasion attempts
    "plug", "vendor", "wickr", "signal drop", "dead drop",
]

POSITIVE_KEYWORDS = [
    "vouch", "legit", "trusted", "reliable", "recommend", "genuine",
    "smooth", "safe", "fast", "quick", "delivered", "confirmed", "verified",
    "excellent", "perfect", "amazing", "solid", "consistent", "professional", "quality",
    "real deal", "top notch", "on time", "came through", "no issues", "all good",
    "thumbs up", "big ups", "massive vouch", "fat vouch", "huge vouch",
    "big vouch", "honest",
]

NEGATIVE_KEYWORDS = [
    "scam", "scammer", "scammed", "fraud", "fraudster", "fake",
    "liar", "thief", "stole", "ripped", "ripoff", "rip off", "ripped off",
    "sketchy", "shady", "dodgy", "ghosted", "blocked",
    "never delivered", "took my money", "didn't deliver", "didnt deliver",
    "avoid", "warning", "beware", "don't trust", "dont trust", "not legit",
    "ran off", "disappeared", "selective scammer",
]

# ═══════════════════════════════════════════════════════════════════════════════
#  PRE-COMPILED PATTERNS
# ═══════════════════════════════════════════════════════════════════════════════

BLACKLIST_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(t) for t in BLACKLIST_TERMS) + r')\b',
    re.IGNORECASE,
)

POS_PATTERN = re.compile(
    r'\b(' + '|'.join(
        re.escape(k) for k in sorted(POSITIVE_KEYWORDS, key=len, reverse=True)
    ) + r')\b',
    re.IGNORECASE,
)

NEG_PATTERN = re.compile(
    r'\b(' + '|'.join(
        re.escape(k) for k in sorted(NEGATIVE_KEYWORDS, key=len, reverse=True)
    ) + r')\b',
    re.IGNORECASE,
)

VOUCH_TRIGGER_RE = re.compile(
    r'^(?:(?:\+|-)(?:vouch|rep|1\b)|(?:vouch|rep)(?:\+|-)|1(?:\+|-))',
    re.IGNORECASE,
)

# All trigger words that should be stripped from vouch comments
VOUCH_TRIGGER_WORDS = [
    # Standard
    '+vouch', '-vouch', 'vouch+', 'vouch-',
    '+rep',   '-rep',   'rep+',   'rep-',
    '/vouch', '+1', '-1', '1+', '1-',
    # Spaced variants (e.g. "+ vouch", "- rep")
    '+ vouch', '- vouch', '+ rep', '- rep',
    '+ 1', '- 1',
]
