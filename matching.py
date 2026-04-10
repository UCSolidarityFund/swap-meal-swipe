"""
matching.py — Request parsing, dining hall hours validation, donor matching.
"""

import re
import logging
from datetime import datetime, time, timedelta
from dateutil import parser as dateparser

import database as db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dining hall aliases → canonical names
# ---------------------------------------------------------------------------
HALL_ALIASES: dict[str, str] = {
    # Connecticut Hall
    "connecticut":      "Connecticut Hall",
    "connecticut hall": "Connecticut Hall",
    "conn":             "Connecticut Hall",
    "ct hall":          "Connecticut Hall",
    # Putnam
    "putnam":           "Putnam",
    # McMahon
    "mcmahon":          "McMahon",
    "mc mahon":         "McMahon",
    # North
    "north":            "North",
    "north dining":     "North",
    # South
    "south":            "South",
    "south dining":     "South",
    # Gelfenbien
    "gelfenbien":       "Gelfenbien",
    "gelf":             "Gelfenbien",
    "kosher":           "Gelfenbien",
    # Whitney
    "whitney":          "Whitney",
    # Northwest
    "northwest":        "Northwest",
    "nw":               "Northwest",
}

KNOWN_HALLS = set(HALL_ALIASES.values())

# ---------------------------------------------------------------------------
# Hours definition
# Hours are stored as (open_time, close_time) tuples of datetime.time.
# Keys: (hall_canonical, day_type) where day_type in {'weekday','saturday','sunday'}
# ---------------------------------------------------------------------------
def t(h, m=0) -> time:
    return time(h, m)

HOURS: dict[tuple, list[tuple[time, time]]] = {
    # --- Connecticut Hall ---
    ("Connecticut Hall", "weekday"): [
        (t(7), t(10, 45)), (t(11), t(14, 30)), (t(16), t(19, 15))
    ],
    ("Connecticut Hall", "saturday"): [
        (t(10, 30), t(14, 30)), (t(16), t(19, 15))
    ],
    ("Connecticut Hall", "sunday"): [
        (t(10, 30), t(14, 30)), (t(16), t(19, 15))
    ],
    # --- Putnam ---
    ("Putnam", "weekday"): [
        (t(7), t(10, 45)), (t(11), t(14, 30)), (t(16), t(19, 15)),
        (t(16), t(22)),   # Grab & Go Mon-Thu (we approximate; Fri closes 20:00)
    ],
    ("Putnam", "saturday"): [(t(9, 30), t(14, 30)), (t(16), t(19, 15))],
    ("Putnam", "sunday"):   [(t(9, 30), t(14, 30)), (t(16), t(19, 15))],
    # --- McMahon ---
    ("McMahon", "weekday"):  [
        (t(7), t(10, 45)), (t(11), t(14)), (t(15, 30), t(19, 15))
    ],
    ("McMahon", "saturday"): [(t(10, 30), t(14)), (t(15, 30), t(19, 15))],
    ("McMahon", "sunday"):   [(t(10, 30), t(14)), (t(15, 30), t(19, 15))],
    # --- North ---
    ("North", "weekday"):  [(t(7), t(10, 45)), (t(11), t(15)), (t(16, 30), t(19, 15))],
    ("North", "saturday"): [(t(10, 30), t(15)), (t(16, 30), t(19, 15))],
    ("North", "sunday"):   [(t(10, 30), t(15)), (t(16, 30), t(19, 15))],
    # --- South ---
    ("South", "weekday"):  [
        (t(7), t(10, 45)), (t(11), t(15)), (t(16, 30), t(22))   # late night to 22:00
    ],
    ("South", "saturday"): [(t(7), t(9, 30)), (t(9, 30), t(15)), (t(16, 30), t(19, 15))],
    ("South", "sunday"):   [(t(8), t(9, 30)), (t(9, 30), t(15)), (t(16, 30), t(22))],
    # --- Gelfenbien ---
    ("Gelfenbien", "weekday"):  [
        (t(7), t(10, 45)), (t(11), t(15)), (t(16, 30), t(19, 15)),
        (t(16, 30), t(22)),   # Grab & Go
    ],
    ("Gelfenbien", "saturday"): [(t(9, 30), t(15)), (t(16, 30), t(19, 15))],
    ("Gelfenbien", "sunday"):   [(t(9, 30), t(15)), (t(16, 30), t(19, 15))],
    # --- Whitney ---
    ("Whitney", "weekday"):  [(t(7), t(10, 45)), (t(11), t(15)), (t(16, 30), t(19, 15))],
    ("Whitney", "saturday"): [(t(10, 30), t(15)), (t(16, 30), t(19, 15))],
    ("Whitney", "sunday"):   [(t(10, 30), t(15)), (t(16, 30), t(19, 15))],
    # --- Northwest ---
    ("Northwest", "weekday"):  [
        (t(7), t(10, 45)), (t(11), t(14, 15)), (t(15, 45), t(22))  # late night
    ],
    ("Northwest", "saturday"): [(t(10, 30), t(14, 15)), (t(15, 45), t(19, 15))],
    ("Northwest", "sunday"):   [(t(10, 30), t(14, 15)), (t(15, 45), t(22))],
}

def day_type(day_name: str) -> str:
    d = day_name.strip().lower()
    if d == "saturday":
        return "saturday"
    if d == "sunday":
        return "sunday"
    return "weekday"

def is_open(hall: str, req_time: time, day_name: str) -> bool:
    """Return True if hall is open at req_time on day_name."""
    dt = day_type(day_name)
    slots = HOURS.get((hall, dt), [])
    for (open_t, close_t) in slots:
        if open_t <= req_time <= close_t:
            return True
    return False

# ---------------------------------------------------------------------------
# Request parser
# ---------------------------------------------------------------------------
# Matches patterns like: "South 8:15pm", "McMahon 9am", "Putnam 1:45pm"
_TIME_RE  = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE
)
_HALL_RE  = re.compile(
    r'(' + '|'.join(re.escape(k) for k in sorted(HALL_ALIASES, key=len, reverse=True)) + r')',
    re.IGNORECASE
)

def parse_request(text: str) -> dict | None:
    """
    Parse a receiver request like 'South 8:15pm' or 'McMahon 9am'.
    Returns dict with keys: hall, time (datetime.time), raw_text
    or None if not parseable.

    Also detects missing-space error and unrecognized hall errors.
    Returns dict with 'error' key for error conditions.
    """
    text = text.strip()

    # Check for missing space (digit immediately after letters, no space)
    # e.g. "McMahon9am"
    if re.search(r'[a-zA-Z]\d', text):
        return {"error": "missing_space"}

    hall_match = _HALL_RE.search(text)
    time_match = _TIME_RE.search(text)

    if not time_match:
        return None   # Not a request at all

    if not hall_match:
        # Has a time but no recognized hall
        return {"error": "unknown_hall"}

    hall_key  = hall_match.group(1).lower()
    hall_name = HALL_ALIASES[hall_key]

    hour   = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    ampm   = time_match.group(3).lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    # Snap to 15-minute increment
    snapped_minute = round(minute / 15) * 15
    if snapped_minute == 60:
        hour += 1
        snapped_minute = 0

    try:
        req_time = time(hour % 24, snapped_minute)
    except ValueError:
        return {"error": "invalid_time"}

    return {
        "hall": hall_name,
        "time": req_time,
        "day":  datetime.now().strftime("%A"),
        "raw":  text,
    }

# ---------------------------------------------------------------------------
# Donor matching
# ---------------------------------------------------------------------------
def try_next_donor(request_id: int, send_sms_fn):
    """
    Send a match request to the next available donor for request_id.
    Skips donors already contacted. Expires request after 30 min.
    """
    req = db.get_request(request_id)
    if not req or req["status"] != "pending":
        return

    # Check expiry
    expires_at = datetime.fromisoformat(req["expires_at"])
    if datetime.utcnow() > expires_at:
        _expire_request(request_id, send_sms_fn, req["receiver_phone"])
        return

    already_contacted = db.get_already_contacted(request_id)
    candidates        = db.get_available_donors()

    # Filter out already-contacted
    untried = [
        c for c in candidates
        if c["phone"] not in already_contacted
    ]

    if not untried:
        # No more donors to try — if none pending either, expire
        pending_offers = db.get_pending_donor_offers(request_id)
        if not pending_offers:
            _expire_request(request_id, send_sms_fn, req["receiver_phone"])
        return

    # Pick first candidate (FIFO; could randomize or score by availability match)
    donor = untried[0]
    donor_phone = donor["phone"]

    db.record_donor_offer(request_id, donor_phone)
    send_sms_fn(
        donor_phone,
        f"Request for {req['hall']} at {req['req_time']}. "
        f"Text Y to accept or N to deny. "
        f"To change your availability, text 'Availability'."
    )
    logger.info("Offer sent to %s for request %d", donor_phone, request_id)


def _expire_request(request_id: int, send_sms_fn, receiver_phone: str):
    db.cancel_request(request_id)
    # Notify any still-pending donors
    pending = db.get_pending_donor_offers(request_id)
    for offer in pending:
        send_sms_fn(offer["donor_phone"], "Request expired.")
    # Notify receiver
    send_sms_fn(
        receiver_phone,
        "We are experiencing a high volume of requests, please try again later "
        "or with a different time and/or dining hall."
    )


def notify_others_fulfilled(request_id: int, accepting_donor: str, send_sms_fn):
    """Notify all other pending donors that the request has been filled."""
    pending = db.get_pending_donor_offers(request_id)
    for offer in pending:
        if offer["donor_phone"] != accepting_donor:
            send_sms_fn(
                offer["donor_phone"],
                "Request fulfilled by another UCSF member! Thank you for your willingness to help."
            )


def notify_pending_donors_canceled(request_id: int, send_sms_fn):
    """Notify all pending donors that the receiver canceled."""
    pending = db.get_pending_donor_offers(request_id)
    for offer in pending:
        send_sms_fn(
            offer["donor_phone"],
            "The request has been canceled."
        )
