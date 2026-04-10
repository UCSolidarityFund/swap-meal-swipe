"""
tests.py — Unit tests for matching logic (no Twilio/DB required).
Run with: python tests.py
"""
import sys
from datetime import time
import matching

def ok(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        sys.exit(1)

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------
print("\n=== Request parser ===")

r = matching.parse_request("South 8:15pm")
ok("South 8:15pm → South", r and r.get("hall") == "South")
ok("South 8:15pm → 20:15", r and r.get("time") == time(20, 15))

r = matching.parse_request("McMahon 9am")
ok("McMahon 9am → McMahon", r and r.get("hall") == "McMahon")
ok("McMahon 9am → 09:00", r and r.get("time") == time(9, 0))

r = matching.parse_request("Putnam 1:45pm")
ok("Putnam 1:45pm → 13:45", r and r.get("time") == time(13, 45))

r = matching.parse_request("northwest 7pm")
ok("northwest aliases → Northwest", r and r.get("hall") == "Northwest")

r = matching.parse_request("McMahon9am")
ok("Missing space → error", r and r.get("error") == "missing_space")

r = matching.parse_request("blahblah 9am")
ok("Unknown hall → error", r and r.get("error") == "unknown_hall")

r = matching.parse_request("hello world")
ok("No time → None", r is None)

# ---------------------------------------------------------------------------
# Hours tests
# ---------------------------------------------------------------------------
print("\n=== Hours validation ===")

ok("South open weekday 8pm (late night)", matching.is_open("South", time(20, 0), "Monday"))
ok("South closed weekday 3am",           not matching.is_open("South", time(3, 0), "Monday"))
ok("McMahon open weekday 11:30am",       matching.is_open("McMahon", time(11, 30), "Wednesday"))
ok("McMahon closed weekday 2:30pm",      not matching.is_open("McMahon", time(14, 30), "Wednesday"))
ok("Putnam open Saturday brunch",        matching.is_open("Putnam", time(10, 0), "Saturday"))
ok("North closed Sunday 4am",            not matching.is_open("North", time(4, 0), "Sunday"))
ok("Northwest open Sunday late night",   matching.is_open("Northwest", time(21, 0), "Sunday"))

print("\nAll tests passed ✓")
