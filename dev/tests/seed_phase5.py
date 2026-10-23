"""Add savings products, a secretary and meetings to the Unity Savings Circle demo (after seed_officers.py).

usage: seed_phase5.py stage|prod PASSWORD
Adds "Christmas savings" (withdraw any time, doesn't count for borrowing) and "Retirement fund"
(only when leaving); Tunde records offline payments into them and Amaka approves; Sade becomes the
Secretary; there's an upcoming meeting with replies and a past one with published minutes. Extra
money from transfers goes to Christmas savings. Safe to re-run: existing records are reused.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TARGET, PASSWORD = sys.argv[1], sys.argv[2]
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")  # git-ignored
BENCH = "/home/ubuntu/start-paymnet/backend"
if TARGET == "stage":
	BASE, STATE, SITE = "http://127.0.0.1:8100/api/method/coopvault.api.", f"{S}/demo_stage.json", "coopvault.test"
	H = {"Accept": "application/json", "Host": "coopvault.test"}
else:
	BASE, STATE, SITE = ("https://api-payment.secureestate.app/api/method/coopvault.api.", f"{S}/demo_prod.json",
		"api-payment.secureestate.app")
	H = {"Accept": "application/json", "Origin": "https://pay-frappe.web.app"}
state = json.load(open(STATE))
CO = state["coop"]
LAGOS = ZoneInfo("Africa/Lagos")


def call(path, token=None, verb="POST", **data):
	h = dict(H)
	if token:
		h["Authorization"] = "Session " + token
	r = requests.post(BASE + path, json=data, headers=h, timeout=60) if verb == "POST" else \
		requests.get(BASE + path, params=data, headers=h, timeout=60)
	if r.status_code != 200:
		print("FAILED", path, r.status_code, r.text[:1000])
		sys.exit(1)
	return r.json().get("message")


def session(key, name=None, phone=None):
	r = requests.post(BASE + "auth.login", json={"email": f"{key}@example.com", "password": PASSWORD}, headers=H, timeout=60)
	if r.status_code == 200:
		return r.json()["message"]["token"]
	return call("auth.signup", full_name=name, email=f"{key}@example.com", password=PASSWORD, phone=phone)["token"]


def console(code):
	out = subprocess.run(["bench", "--site", SITE, "console"], input=code + "\nfrappe.db.commit()\n", cwd=BENCH,
		capture_output=True, text=True)
	if out.returncode or "Error" in out.stdout:
		print(out.stdout[-2000:], out.stderr[-2000:])
		sys.exit(1)


A, T = session("amaka"), session("tunde")
M = state["members"]

# --- Savings products, and where extra money from transfers goes --------------------------------
products = call("finance.admin_savings_products", A, "GET", cooperative=CO)
by_name = {p["product_name"]: p for p in products}
if "Christmas savings" not in by_name:
	products = call("finance.save_savings_product", A, cooperative=CO, product_name="Christmas savings",
		description="Put a little aside for December: food, gifts and travel home.", withdrawal_rule="Any time",
		counts_for_borrowing=0)
if "Retirement fund" not in {p["product_name"] for p in products}:
	products = call("finance.save_savings_product", A, cooperative=CO, product_name="Retirement fund",
		description="Long-term savings, paid out when you leave the group. Counts towards what you can borrow.",
		withdrawal_rule="Only when leaving", counts_for_borrowing=1)
by_name = {p["product_name"]: p for p in products}
XMAS, RET = by_name["Christmas savings"]["name"], by_name["Retirement fund"]["name"]
call("coop.update_cooperative", A, cooperative=CO, auto_allocate=1, extra_savings_product=XMAS)

# Tunde records two transfers into the new products; Amaka checks and approves them
for key, product, amount, ref in (("chioma", XMAS, 15000, "XMAS-CHI-1"), ("ngozi", RET, 25000, "RET-NGO-1"),
		("musa", XMAS, 5000, "XMAS-MUS-1")):
	st = call("coop.get_statement", A, "GET", cooperative=CO, member=M[key])
	if any(e.get("product") for e in st["entries"]):
		continue
	rec = call("finance.record_offline_payment", T, cooperative=CO, member=M[key], amount=amount,
		payment_date=date.today().isoformat(), reference=ref, savings_product=product, note="Bank transfer")
	ap = call("approvals.for_reference", A, reference_doctype=rec["doctype"], reference_name=rec["name"])
	call("approvals.decide", A, name=ap["name"], approve=1)

# --- Sade, the secretary --------------------------------------------------------------------------
sade = session("sade", "Sade Bello", "08031000011")
members = {m["user"]: m for m in call("coop.list_members", A, "GET", cooperative=CO)}
m = members.get("sade@example.com")
if not m:
	call("coop.join_cooperative", sade, join_code=state["join_code"])
	m = next(x for x in call("coop.list_members", A, "GET", cooperative=CO) if x["user"] == "sade@example.com")
if m["status"] == "Pending":
	ap = call("approvals.for_reference", A, reference_doctype="Coop Member", reference_name=m["name"])
	call("approvals.decide", A, name=ap["name"], approve=1)
if m["role"] != "Secretary":
	call("coop.update_member", A, member=m["name"], role="Secretary")
M["sade"] = m["name"]

# --- Meetings ---------------------------------------------------------------------------------------
meetings = call("meetings.list_meetings", sade, "GET", cooperative=CO)
if not any(x["title"] == "October general meeting" for x in meetings["past"] + meetings["upcoming"]):
	soon = (datetime.now(LAGOS) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
	mt = call("meetings.save_meeting", sade, cooperative=CO, title="October general meeting", kind="General meeting",
		starts_at=soon, venue="St. Paul's parish hall, Surulere",
		agenda="1. Opening prayer\n2. Treasurer's report\n3. Loan requests\n4. Christmas savings\n5. Any other business")
	present = [M[k] for k in ("amaka", "tunde", "chioma", "ify", "musa", "ngozi", "kemi", "sade")]
	call("meetings.record_attendance", sade, name=mt["name"], present=present)
	call("meetings.save_minutes", sade, name=mt["name"], publish=1,
		minutes=("The chairman opened the meeting at 10:05am with a prayer by Mrs Okafor.\n\n"
			"The treasurer reported that every member is now on Cooperative360 and that contributions for September "
			"were collected in full. Two loans were repaid early.\n\n"
			"Members agreed to start a Christmas savings fund, separate from regular contributions, that can be "
			"withdrawn from 1 December.\n\n"
			"The meeting closed at 11:40am."),
		resolutions=[
			{"resolution": "Accept the treasurer's report for September", "outcome": "Passed", "votes_for": 8,
				"votes_against": 0, "abstained": 0},
			{"resolution": "Open a Christmas savings fund for members who want one", "outcome": "Passed",
				"votes_for": 7, "votes_against": 0, "abstained": 1},
			{"resolution": "Raise monthly dues from January", "outcome": "Deferred"},
		])
	past = (datetime.now(LAGOS) - timedelta(days=20)).replace(hour=10, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
	published = (datetime.now(LAGOS) - timedelta(days=19)).strftime("%Y-%m-%d %H:%M:%S")
	console(f"frappe.db.set_value('Coop Meeting', '{mt['name']}', {{'starts_at': '{past}', 'published_on': '{published}'}},"
		" update_modified=False)")
if not any(x["title"] == "November general meeting" for x in meetings["upcoming"]):
	nxt = (datetime.now(LAGOS) + timedelta(days=9)).replace(hour=10, minute=0).strftime("%Y-%m-%dT%H:%M")
	mt = call("meetings.save_meeting", sade, cooperative=CO, title="November general meeting", kind="General meeting",
		starts_at=nxt, venue="St. Paul's parish hall, Surulere",
		agenda="1. Treasurer's report\n2. Christmas savings: how much and when\n3. Year-end share-out\n4. Any other business")
	for key, response in (("amaka", "Coming"), ("tunde", "Coming"), ("chioma", "Coming"), ("ify", "Apology")):
		call("meetings.reply", session(key), name=mt["name"], response=response)
	state["meeting_next"] = mt["name"]

json.dump(state, open(STATE, "w"))
print(json.dumps({"coop": CO, "christmas": XMAS, "retirement": RET, "meeting_next": state.get("meeting_next")}))
