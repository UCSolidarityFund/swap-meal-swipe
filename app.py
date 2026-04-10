"""
UConn Solidarity Fund - Meal Swipe Swap
SMS-based mutual aid meal swipe matching system via Twilio.
"""

import os
import re
import logging
from datetime import datetime, time, timedelta
from flask import Flask, request, Response, session, redirect, url_for
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client
import database as db
import matching

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Session signing key — generate a random one with: python3 -c "import secrets; print(secrets.token_hex(32))"
# Must be set in production env or sessions won't survive restarts.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
if not os.environ.get("SECRET_KEY"):
    logger.warning("SECRET_KEY not set — sessions will reset on every restart")

# Simulator gate — set a long random string in the env to enable the test UI.
# Leave unset (or empty) in production to disable it entirely.
SIMULATOR_KEY = os.environ.get("SIMULATOR_KEY", "")

# ---------------------------------------------------------------------------
# Twilio client (credentials from env)
# ---------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE       = os.environ.get("TWILIO_PHONE", "")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WELCOME_MSG = (
    "Welcome to the @UConnSolidarityFund meal swipe swap program!\n\n"
    "To volunteer to swipe students in, text 'Solidarity'.\n"
    "To request a flex swipe, text a dining hall name followed by a meet time "
    "using 15-minute increments; ex: McMahon 9am, South 8:15pm, Putnam 1:45pm.\n"
    "For dining hall hours, text 'Hours'.\n"
    "For our mission statement, text 'Mission statement'.\n"
    "To stay up to date with Solidarity Fund events, text 'Updates' or follow us "
    "on Instagram! @UConnSolidarityFund"
)

MISSION_MSG = (
    "The UConn Solidarity Fund (UCSF) is a student-run initiative operating on a "
    "premise of mutual aid, a practice where the community voluntarily and "
    "unconditionally collaborates to exchange resources and services for the common "
    "benefit. According to UConn's 2025 report on food insecurity, 37% of students "
    "report experiencing food insecurity, with the top reasons for not using existing "
    "resources being accessibility regarding time and location. We started the meal "
    "swipe swap program to empower ourselves as students and as a community to provide "
    "for each other in ways our institutions refuse to. We have the tools in our hands; "
    "even the cheapest meal plan offers 75 guest/flex passes per semester, which can be "
    "used at dining halls all around campus anytime from 7am-10pm. The U.S. produces "
    "over 3800 calories per person, per day, nearly twice the nutritional requirement. "
    "Hunger is a policy choice. Together, let's choose otherwise."
)

UPDATES_MSG = (
    "Great! We will occasionally send text updates on relevant demonstrations as well "
    "as our mutual aid and education events/initiatives. For more updates, check out our "
    "Instagram. Text 'cancel' to stop these messages."
)

HOURS_MSG = (
    "=== FALL & SPRING SEMESTER HOURS ===\n\n"
    "MON-FRI:\n"
    "Connecticut Hall & Putnam\n"
    "  Breakfast 7-10:45am | Lunch 11am-2:30pm | Dinner 4-7:15pm\n"
    "  Putnam Grab & Go (Mon-Thu) 4-10pm | (Fri) 4-8pm\n\n"
    "McMahon\n"
    "  Breakfast 7-10:45am | Lunch 11am-2pm | Dinner 3:30-7:15pm\n\n"
    "North, South, Gelfenbien, Whitney\n"
    "  Breakfast 7-10:45am | Lunch 11am-3pm | Dinner 4:30-7:15pm\n"
    "  Gelfenbien Grab & Go (Mon-Thu) 4:30-10pm | (Fri) 4:30-8pm\n\n"
    "Northwest\n"
    "  Breakfast 7-10:45am | Lunch 11am-2:15pm | Dinner 3:45-7:15pm\n\n"
    "*Late Night: South & Northwest open until 10pm Sun-Thu\n\n"
    "WEEKENDS:\n"
    "South: Sat Breakfast 7-9:30am | Sun 8-9:30am | Brunch 9:30am-3pm | Dinner 4:30-7:15pm\n"
    "Gelfenbien: Brunch 9:30am-3pm | Dinner 4:30-7:15pm\n"
    "Putnam: Brunch 9:30am-2:30pm | Dinner 4-7:15pm\n"
    "Connecticut Hall: Brunch 10:30am-2:30pm | Dinner 4-7:15pm\n"
    "North & Whitney: Brunch 10:30am-3pm | Dinner 4:30-7:15pm\n"
    "Northwest: Brunch 10:30am-2:15pm | Dinner 3:45-7:15pm\n"
    "McMahon: Brunch 10:30am-2pm | Dinner 3:30-7:15pm\n\n"
    "KOSHER (Nosh at Gelfenbien): Mon-Thu all meals | Fri Breakfast & Lunch only | Sat closed | Sun Brunch & Dinner\n"
    "HALAL: Gelfenbien & South — Lunch & Dinner Mon-Fri; Brunch & Dinner weekends"
)

DONOR_CONFIRMED_MSG = (
    "You will receive texts during your availability with a dining hall name and a time "
    "with the choice to accept or deny. If you accept, the recipient will meet you at "
    "the dining hall entrance to be swiped in. You can identify each other by holding "
    "up a fist and/or saying 'Solidarity'."
)

# ---------------------------------------------------------------------------
# Outbound SMS helper
# ---------------------------------------------------------------------------
def send_sms(to: str, body: str):
    """Send an outbound SMS via Twilio, or capture in simulator mode."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            sim_list = g.get("sim_outbound", None)
            if sim_list is not None:
                sim_list.append({"to": to, "body": body})
                logger.info("SIM captured SMS to %s", to)
                return
    except RuntimeError:
        pass

    if twilio_client and TWILIO_PHONE:
        try:
            twilio_client.messages.create(to=to, from_=TWILIO_PHONE, body=body)
            logger.info("SMS sent to %s", to)
        except Exception as e:
            logger.error("Failed to send SMS to %s: %s", to, e)
    else:
        logger.warning("Twilio not configured — would send to %s: %s", to, body)

# ---------------------------------------------------------------------------
# State-machine dispatcher
# ---------------------------------------------------------------------------
@app.route("/sms", methods=["POST"])
def sms_webhook():
    # Validate the request actually came from Twilio.
    if TWILIO_AUTH_TOKEN:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        url       = request.url
        params    = request.form.to_dict()
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, params, signature):
            logger.warning("Rejected request with invalid Twilio signature")
            return Response("Forbidden", status=403)

    from_number = request.form.get("From", "").strip()
    body        = request.form.get("Body", "").strip()
    resp        = MessagingResponse()
    reply       = handle_message(from_number, body)
    if reply:
        resp.message(reply)
    return Response(str(resp), mimetype="text/xml")

def handle_message(phone: str, body: str) -> str | None:
    """Route inbound message to the correct handler. Returns reply string or None."""
    db.ensure_user(phone)
    state = db.get_state(phone)
    text  = body.strip()
    lower = text.lower()

    # ---- Global keywords (any state) ----
    if lower in ("solidarity fund", "") or db.is_new_user(phone):
        db.mark_seen(phone)
        return WELCOME_MSG

    if lower in ("stop", "stopall", "unsubscribe", "quit"):
        db.set_state(phone, "idle")
        db.remove_from_updates(phone)
        return (
            "You have been unsubscribed from all messages. "
            "Text 'Solidarity Fund' at any time to re-enroll."
        )

    if lower == "help":
        return (
            "UConn Solidarity Fund Meal Swipe Swap\n"
            "Text 'Solidarity Fund' to see all options.\n"
            "Text STOP to unsubscribe.\n"
            "For support: @UConnSolidarityFund on Instagram or "
            "email [your email here].\n"
            "Msg & data rates may apply."
        )

    if lower == "hours":
        return HOURS_MSG

    if lower == "mission statement":
        return MISSION_MSG

    if lower == "updates":
        db.add_to_updates(phone)
        return UPDATES_MSG

    if lower == "solidarity":
        db.set_state(phone, "awaiting_availability")
        return (
            "Thanks for volunteering! Please share your weekly availability and preferred "
            "dining hall(s) — e.g. 'Mon-Fri 11am-2pm, McMahon and South' or describe "
            "your schedule. You can also just say your open days/times and we'll match "
            "from there. Text 'Availability' at any time to update."
        )

    if lower == "availability":
        db.set_state(phone, "awaiting_availability")
        return "Please share your updated weekly availability and preferred dining hall(s)."

    # ---- State-specific handlers ----
    if state == "awaiting_availability":
        return handle_availability_input(phone, text)

    if state and state.startswith("awaiting_cancel_confirm"):
        return handle_cancel_confirm(phone, lower)

    # ---- Cancel keyword ----
    if lower == "cancel":
        return handle_cancel(phone)

    # ---- Y/N from a donor ----
    if lower in ("y", "n", "yes", "no"):
        return handle_donor_response(phone, lower)

    # ---- Receiver request: dining hall + time ----
    result = matching.parse_request(text)
    if result:
        err = result.get("error")
        if err == "missing_space":
            return "Error: entry missing space."
        if err == "unknown_hall":
            return "Error: dining hall name not recognized."
        if err == "invalid_time":
            return "Error: invalid time."
        return handle_receiver_request(phone, result)

    return (
        "Sorry, I didn't understand that. Text 'Solidarity Fund' to see all options."
    )

# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------
def handle_availability_input(phone: str, text: str) -> str:
    """Store raw availability text; a human admin can review or we parse it."""
    db.save_availability(phone, text)
    db.set_state(phone, "donor_idle")
    db.set_role(phone, "donor")
    return DONOR_CONFIRMED_MSG


def handle_receiver_request(phone: str, parsed: dict) -> str:
    hall      = parsed["hall"]
    req_time  = parsed["time"]   # datetime.time object
    req_day   = parsed.get("day", datetime.now().strftime("%A"))

    # Validate hours
    if not matching.is_open(hall, req_time, req_day):
        return f"Error: request is outside of dining hall hours for {hall}."

    # Create pending request
    request_id = db.create_request(phone, hall, req_time, req_day)

    # Start matching async (send first donor text)
    matching.try_next_donor(request_id, send_sms)

    db.set_state(phone, f"receiver_waiting:{request_id}")
    return (
        "Currently waiting for a response; may take up to 30 minutes to get a response. "
        "Text 'cancel' at any time to cancel your request."
    )


def handle_donor_response(phone: str, answer: str) -> str:
    accepted = answer in ("y", "yes")
    pending  = db.get_pending_donor_offer(phone)
    if not pending:
        return "No active request found for your response."

    request_id = pending["request_id"]
    req        = db.get_request(request_id)
    if not req:
        return "That request is no longer active."

    if not accepted:
        db.mark_donor_declined(request_id, phone)
        matching.try_next_donor(request_id, send_sms)
        return "Got it, thanks for letting us know!"

    # Donor accepted
    db.fulfill_request(request_id, phone)

    receiver = req["receiver_phone"]
    hall     = req["hall"]
    t        = req["req_time"]

    # Notify receiver
    send_sms(receiver,
        f"Request accepted! Meet at {hall} at {t}. A UCSF member will meet you at the "
        f"dining hall entrance to be swiped in. You can identify each other by holding "
        f"up a fist and/or saying 'Solidarity'."
    )

    # Notify any other pending donors this request is gone
    matching.notify_others_fulfilled(request_id, phone, send_sms)

    db.set_state(phone, f"donor_confirmed:{request_id}")
    return (
        f"Your meeting for {hall} at {t} has been confirmed. The recipient will meet you "
        f"at the dining hall entrance. You can identify each other by holding up a fist "
        f"and/or saying 'Solidarity'. Text 'cancel' at any time to cancel."
    )


def handle_cancel(phone: str) -> str:
    state = db.get_state(phone) or ""

    if state.startswith("receiver_waiting:"):
        request_id = state.split(":")[1]
        req = db.get_request(request_id)
        if req and req["status"] == "pending":
            db.cancel_request(request_id)
            matching.notify_pending_donors_canceled(request_id, send_sms)
            db.set_state(phone, "idle")
            return "Your request has been canceled."

        if req and req["status"] == "fulfilled":
            db.set_state(phone, f"awaiting_cancel_confirm:{request_id}")
            return (
                "Are you sure? Someone has already accepted your request. "
                "Type 'yes' to confirm cancellation."
            )

    if state.startswith("donor_confirmed:"):
        request_id = state.split(":")[1]
        req = db.get_request(request_id)
        if req:
            receiver = req["receiver_phone"]
            send_sms(receiver,
                "The other party has canceled the meeting, we apologize for the inconvenience."
            )
        db.cancel_request(request_id)
        db.set_state(phone, "donor_idle")
        return "Meeting canceled. We've notified the other party."

    if state == "on_updates_list":
        db.remove_from_updates(phone)
        db.set_state(phone, "idle")
        return "You've been removed from the updates list."

    return "Nothing to cancel right now."


def handle_cancel_confirm(phone: str, answer: str) -> str:
    state = db.get_state(phone) or ""
    if ":" in state:
        request_id = state.split(":")[1]
    else:
        db.set_state(phone, "idle")
        return "Cancellation aborted."

    if answer == "yes":
        req = db.get_request(request_id)
        if req and req["donor_phone"]:
            send_sms(req["donor_phone"],
                "The other party has canceled the meeting, we apologize for the inconvenience."
            )
        db.cancel_request(request_id)
        db.set_state(phone, "idle")
        return "Your request has been canceled."
    else:
        # Restore previous state
        db.set_state(phone, f"receiver_waiting:{request_id}")
        return "Cancellation aborted. Your request is still active."

# ---------------------------------------------------------------------------
# Scheduler: semester availability pings + 10-min retry loop
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler()

def ping_donors_availability(period_label: str):
    donors = db.get_all_donors()
    for d in donors:
        send_sms(d["phone"],
            f"Would you like to update your meal swipe swap availability for the "
            f"{period_label}? If so, text 'Availability'."
        )

scheduler.add_job(
    lambda: ping_donors_availability("break"),
    "cron", month=12, day=19, hour=10, minute=0
)
scheduler.add_job(
    lambda: ping_donors_availability("break"),
    "cron", month=5, day=15, hour=10, minute=0
)
scheduler.add_job(
    lambda: ping_donors_availability("semester"),
    "cron", month=1, day=18, hour=10, minute=0
)
scheduler.add_job(
    lambda: ping_donors_availability("semester"),
    "cron", month=8, day=23, hour=10, minute=0
)

# 10-minute retry poller: check for stalled requests
def retry_stalled_requests():
    import matching as m
    stalled = db.get_stalled_requests(timeout_minutes=10)
    for req in stalled:
        m.try_next_donor(req["id"], send_sms)

scheduler.add_job(retry_stalled_requests, "interval", minutes=2)

scheduler.start()

# ---------------------------------------------------------------------------
# Simulator UI (dev/testing only — remove in production or gate behind env flag)
# ---------------------------------------------------------------------------
import json

SIMULATOR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Swipe Swap Simulator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #f0f0f0; height: 100vh; display: flex; flex-direction: column; }
  header { background: #c00; color: white; padding: 12px 20px; font-weight: bold; font-size: 1.1rem; }
  header span { font-size: 0.85rem; font-weight: normal; opacity: 0.85; margin-left: 12px; }
  .panels { display: flex; flex: 1; overflow: hidden; gap: 1px; background: #ccc; }
  .panel { flex: 1; display: flex; flex-direction: column; background: white; }
  .panel-header { padding: 10px 14px; background: #222; color: white; font-size: 0.85rem; display: flex; align-items: center; gap: 8px; }
  .panel-header .badge { background: #c00; border-radius: 99px; padding: 2px 8px; font-size: 0.75rem; }
  .messages { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 0.88rem; line-height: 1.4; white-space: pre-wrap; }
  .msg.sent { align-self: flex-end; background: #0b7dda; color: white; border-bottom-right-radius: 3px; }
  .msg.recv  { align-self: flex-start; background: #e8e8e8; color: #111; border-bottom-left-radius: 3px; }
  .msg .meta { font-size: 0.7rem; opacity: 0.65; margin-top: 3px; }
  .input-row { display: flex; border-top: 1px solid #ddd; padding: 10px; gap: 8px; }
  .input-row input { flex: 1; padding: 8px 12px; border: 1px solid #ccc; border-radius: 20px; font-size: 0.9rem; }
  .input-row button { background: #c00; color: white; border: none; border-radius: 20px; padding: 8px 16px; cursor: pointer; font-size: 0.9rem; }
  .input-row button:hover { background: #a00; }
  .quick-btns { padding: 6px 10px; display: flex; flex-wrap: wrap; gap: 4px; border-top: 1px solid #eee; background: #fafafa; }
  .quick-btns button { background: #eee; border: 1px solid #ccc; border-radius: 12px; padding: 3px 10px; font-size: 0.78rem; cursor: pointer; }
  .quick-btns button:hover { background: #ddd; }
  .status { text-align: center; color: #888; font-size: 0.78rem; padding: 4px; }
</style>
</head>
<body>
<header>UConn Swipe Swap — SMS Simulator <span>Test both donor &amp; receiver flows below</span></header>
<div class="panels">
  <!-- Receiver panel -->
  <div class="panel" id="panel-a">
    <div class="panel-header">
      <span class="badge">Receiver</span>
      Phone: +1-555-000-0001 &nbsp;·&nbsp; <em id="state-a">state: new</em>
    </div>
    <div class="messages" id="msgs-a"></div>
    <div class="quick-btns" id="quick-a">
      <button onclick="qs('a','Solidarity Fund')">Solidarity Fund</button>
      <button onclick="qs('a','Hours')">Hours</button>
      <button onclick="qs('a','Mission statement')">Mission statement</button>
      <button onclick="qs('a','Updates')">Updates</button>
      <button onclick="qs('a','South 12pm')">South 12pm</button>
      <button onclick="qs('a','McMahon 11:30am')">McMahon 11:30am</button>
      <button onclick="qs('a','North 5pm')">North 5pm</button>
      <button onclick="qs('a','cancel')">cancel</button>
      <button onclick="qs('a','yes')">yes</button>
    </div>
    <div class="input-row">
      <input id="inp-a" placeholder="Type a message…" onkeydown="if(event.key==='Enter')send('a')">
      <button onclick="send('a')">Send</button>
    </div>
  </div>
  <!-- Donor panel -->
  <div class="panel" id="panel-b">
    <div class="panel-header">
      <span class="badge" style="background:#277">Donor</span>
      Phone: +1-555-000-0002 &nbsp;·&nbsp; <em id="state-b">state: new</em>
    </div>
    <div class="messages" id="msgs-b"></div>
    <div class="quick-btns" id="quick-b">
      <button onclick="qs('b','Solidarity Fund')">Solidarity Fund</button>
      <button onclick="qs('b','Solidarity')">Solidarity</button>
      <button onclick="qs('b','Mon-Fri 10am-2pm, South and McMahon')">Set availability</button>
      <button onclick="qs('b','Y')">Y (accept)</button>
      <button onclick="qs('b','N')">N (deny)</button>
      <button onclick="qs('b','Availability')">Availability</button>
      <button onclick="qs('b','cancel')">cancel</button>
    </div>
    <div class="input-row">
      <input id="inp-b" placeholder="Type a message…" onkeydown="if(event.key==='Enter')send('b')">
      <button onclick="send('b')">Send</button>
    </div>
  </div>
</div>
<script>
const phones = { a: '+15550000001', b: '+15550000002' };

async function send(panel) {
  const input = document.getElementById('inp-' + panel);
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMsg(panel, text, 'sent');
  const res = await fetch('/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone: phones[panel], message: text })
  });
  const data = await res.json();
  if (data.reply) addMsg(panel, data.reply, 'recv');
  if (data.outbound) {
    data.outbound.forEach(o => {
      const target = Object.keys(phones).find(k => phones[k] === o.to);
      if (target) addMsg(target, o.body, 'recv', '(outbound from bot)');
    });
  }
  if (data.state_a !== undefined) document.getElementById('state-a').textContent = 'state: ' + (data.state_a || '?');
  if (data.state_b !== undefined) document.getElementById('state-b').textContent = 'state: ' + (data.state_b || '?');
}

function qs(panel, text) {
  document.getElementById('inp-' + panel).value = text;
  send(panel);
}

function addMsg(panel, text, cls, note) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  if (note) {
    const m = document.createElement('div');
    m.className = 'meta'; m.textContent = note;
    div.appendChild(m);
  }
  const box = document.getElementById('msgs-' + panel);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
</script>
</body>
</html>
"""

def _sim_enabled():
    return bool(SIMULATOR_KEY)

def _sim_authed():
    return session.get("sim_authed") is True

_LOGIN_HTML_TMPL = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    "<title>Simulator Login</title><style>"
    "body{font-family:system-ui,sans-serif;display:flex;align-items:center;"
    "justify-content:center;height:100vh;margin:0;background:#f0f0f0}"
    "form{background:white;padding:2rem;border-radius:8px;"
    "box-shadow:0 2px 8px rgba(0,0,0,.15);display:flex;flex-direction:column;"
    "gap:1rem;min-width:280px}"
    "h2{margin:0;color:#c00;font-size:1.1rem}"
    "input{padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-size:1rem}"
    "button{background:#c00;color:white;border:none;padding:10px;"
    "border-radius:4px;cursor:pointer;font-size:1rem}"
    ".err{color:#c00;font-size:.85rem}"
    '</style></head><body>'
    '<form method="POST" action="/sim-login">'
    "<h2>Swipe Swap &mdash; Simulator</h2>"
    '<input name="key" type="password" placeholder="Simulator key" autofocus required>'
    "<button type=\"submit\">Enter</button>"
    "<!--ERROR_SLOT-->"
    "</form></body></html>"
)

def _login_page(error=""):
    return _LOGIN_HTML_TMPL.replace(
        "<!--ERROR_SLOT-->",
        f'<p class="err">{error}</p>' if error else ""
    )

@app.route("/")
def simulator_ui():
    if not _sim_enabled():
        return Response("Not found", status=404)
    if not _sim_authed():
        return _login_page(), 200, {"Content-Type": "text/html"}
    return SIMULATOR_HTML

@app.route("/sim-login", methods=["POST"])
def sim_login():
    if not _sim_enabled():
        return Response("Not found", status=404)
    key = request.form.get("key", "")
    if key == SIMULATOR_KEY:
        session["sim_authed"] = True
        return redirect(url_for("simulator_ui"), 303)
    return _login_page("Wrong key."), 401, {"Content-Type": "text/html"}

@app.route("/sim-logout")
def sim_logout():
    session.clear()
    return redirect(url_for("simulator_ui"))

@app.route("/simulate", methods=["POST"])
def simulate():
    if not _sim_enabled() or not _sim_authed():
        return Response("Forbidden", status=403)

    from flask import g
    data    = request.get_json()
    phone   = data.get("phone", "").strip()
    message = data.get("message", "").strip()

    g.sim_outbound = []
    reply = handle_message(phone, message)

    state_a = db.get_state("+15550000001")
    state_b = db.get_state("+15550000002")

    return json.dumps({
        "reply":    reply,
        "outbound": g.sim_outbound,
        "state_a":  state_a,
        "state_b":  state_b,
    }), 200, {"Content-Type": "application/json"}

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    db.init_db()
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
