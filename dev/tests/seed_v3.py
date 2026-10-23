"""Seed the Unity Savings Circle demo under the online-only model.

usage: seed_v3.py stage|prod PASSWORD [part]
  part 1 (default): people, cooperative, opening balances, loans sent and confirmed
  part 2: requests that need the online payments from part 1 first (Ngozi's loan, Amaka's withdrawal)
On staging, part 1 also makes the online payments through the fake Paystack.
"""
import json
import os
import subprocess
import sys

import requests

TARGET, PASSWORD = sys.argv[1], sys.argv[2]
PART = sys.argv[3] if len(sys.argv) > 3 else "1"
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")  # git-ignored
os.makedirs(S, exist_ok=True)
BENCH = "/home/ubuntu/start-paymnet/backend"
if TARGET == "stage":
	BASE, SITE, OUT = "http://127.0.0.1:8100/api/method/coopvault.api.", "coopvault.test", f"{S}/demo_stage.json"
	H = {"Accept": "application/json", "Host": SITE}
elif TARGET == "prod":
	BASE, SITE, OUT = "https://api-payment.secureestate.app/api/method/coopvault.api.", "api-payment.secureestate.app", f"{S}/demo_prod.json"
	H = {"Accept": "application/json", "Origin": "https://pay-frappe.web.app"}
else:
	sys.exit("target must be stage or prod")

PEOPLE = [
	# key, full name, phone, joined_on
	("amaka", "Amaka Eze", "08031000001", "2026-08-01"),
	("tunde", "Tunde Bakare", "08031000002", "2026-08-01"),
	("chioma", "Chioma Okafor", "08031000003", "2026-08-05"),
	("ify", "Ify Okeke", "08031000004", "2026-09-10"),
	("musa", "Musa Bello", "08031000005", "2026-08-02"),
	("ngozi", "Ngozi Adeyemi", "08031000006", "2026-08-15"),
	("bisi", "Bisi Lawal", "08031000007", None),
]


def raw(path, token=None, method="POST", **data):
	h = dict(H)
	if token:
		h["Authorization"] = "Session " + token
	if method == "POST":
		return requests.post(BASE + path, json=data, headers=h, timeout=60)
	return requests.get(BASE + path, params=data, headers=h, timeout=60)


def call(path, token=None, method="POST", **data):
	r = raw(path, token, method, **data)
	if r.status_code != 200:
		print("FAILED", path, r.status_code, r.text[:1200])
		sys.exit(1)
	return r.json().get("message")


def bench_set(doctype, name, values):
	code = f"import frappe; frappe.db.set_value({doctype!r}, {name!r}, {values!r}, update_modified=False); frappe.db.commit()"
	out = subprocess.run(["bench", "--site", SITE, "console"], input=code, cwd=BENCH, capture_output=True, text=True)
	if out.returncode or "Error" in out.stdout:
		print(out.stdout[-1500:], out.stderr[-1500:])
		sys.exit(1)


def session(key, name, phone):
	r = raw("auth.login", email=f"{key}@example.com", password=PASSWORD)
	if r.status_code == 200:
		return r.json()["message"]["token"]
	return call("auth.signup", full_name=name, email=f"{key}@example.com", password=PASSWORD, phone=phone)["token"]


def fake_pay(token, coop, purpose, amount, loan=None):
	init = call("payments.initialize", token, cooperative=coop, purpose=purpose, amount=amount, loan=loan)
	requests.post("http://127.0.0.1:8899/_test/pay", json={"reference": init["reference"]}, timeout=30).raise_for_status()
	return call("payments.verify", token, reference=init["reference"])


tok = {key: session(key, name, phone) for key, name, phone, _ in PEOPLE}
A, T = tok["amaka"], tok["tunde"]

if PART == "1":
	coop = call("coop.create_cooperative", A, cooperative_name="Unity Savings Circle", contribution_amount=10000,
		contribution_frequency="Monthly", start_date="2026-08-01", interest_rate=5, max_loan_tenure_months=6,
		fee_bearer="Member",
		description="Monthly savings for traders at Ikeja market. We meet on the last Saturday of every month.")
	CO = coop["name"]
	for key in ("tunde", "chioma", "ify", "musa", "ngozi", "bisi"):
		call("coop.join_cooperative", tok[key], join_code=coop["join_code"])
	members = {m["full_name"].split()[0].lower(): m["name"] for m in call("coop.list_members", A, method="GET", cooperative=CO)}
	for key in ("tunde", "chioma", "ify", "musa", "ngozi"):
		call("coop.update_member", A, member=members[key], status="Active")
	call("coop.update_member", A, member=members["tunde"], role="Treasurer")
	for key, _, _, joined in PEOPLE:
		if joined:
			bench_set("Coop Member", members[key], {"joined_on": joined})

	# Settlement account and payout accounts on Paystack's test bank; backdated so they don't look freshly changed
	call("coop.set_bank_account", A, cooperative=CO, bank_code="001", account_number="0099887766", password=PASSWORD)
	for i, key in enumerate(("amaka", "tunde", "chioma", "musa", "ngozi")):
		call("auth.set_payout_account", tok[key], bank_code="001", account_number=f"00310000{i + 10}", password=PASSWORD)
		bench_set("Coop Member", members[key], {"payout_changed_on": "2026-10-01 10:00:00"})

	# What each member had saved before CoopVault, brought over by the treasurer (Amaka does Tunde's)
	opening = {"amaka": (20000, "2026-09-30"), "tunde": (30000, "2026-10-01"), "chioma": (10000, "2026-09-30"),
		"musa": (40000, "2026-10-02"), "ngozi": (10000, "2026-09-18")}
	for key, (amount, as_of) in opening.items():
		row = call("finance.propose_opening_balance", A if key == "tunde" else T, cooperative=CO, member=members[key],
			amount=amount, as_of=as_of, note="From the savings notebook")
		if key != "chioma":  # Chioma hasn't confirmed hers yet
			call("finance.respond_opening_balance", tok[key], name=row["name"], accept=1)

	# Musa: 3-month loan, sent by Tunde in September and confirmed, first instalment now due
	loan = call("finance.apply_loan", tok["musa"], cooperative=CO, amount=30000, tenure_months=3,
		purpose="Restock the shop for December")
	call("finance.review_loan", T, name=loan["name"], action="approve", transfer_reference="TRF-0919-MUSA")
	call("finance.confirm_loan_received", tok["musa"], name=loan["name"])
	# Backdate it to September, and lay its instalments out from then
	out = subprocess.run(["bench", "--site", SITE, "console"], cwd=BENCH, capture_output=True, text=True, input=f"""
from coopvault import loans
d = frappe.get_doc("Coop Loan", "{loan['name']}")
d.disbursed_on, d.received_on, d.application_date = "2026-09-19", "2026-09-20", "2026-09-18"
loans.start_schedule(d)
d.flags.ignore_validate = True
d.save(ignore_permissions=True)
loans.refresh(d.name)
frappe.db.commit()
""")
	if out.returncode:
		print(out.stdout[-1500:], out.stderr[-1500:])
		sys.exit(1)

	# Tunde: his own loan has to be sent by Amaka; it's on its way and waiting for him to confirm
	loan_t = call("finance.apply_loan", T, cooperative=CO, amount=20000, tenure_months=2, purpose="Generator repair")
	call("finance.review_loan", A, name=loan_t["name"], action="approve", transfer_reference="TRF-1021-TUNDE")

	call("coop.post_announcement", A, cooperative=CO, title="October meeting: Saturday 31st, 4pm",
		message="We meet at the community hall as usual. Please clear your October contribution before the meeting. "
		"Pay in the app with your card, a bank transfer or USSD.", pinned=1)
	call("coop.post_announcement", A, cooperative=CO, title="Welcome to CoopVault",
		message="From this month we track all contributions and loans here instead of the WhatsApp group. "
		"Please check the opening balance we brought over for you and confirm it. Turn on Autosave so you never miss a month.")

	state = {"coop": CO, "join_code": coop["join_code"], "members": members, "loan_musa": loan["name"], "loan_tunde": loan_t["name"]}
	if TARGET == "stage":
		fake_pay(A, CO, "Contribution", 10000)
		fake_pay(tok["ngozi"], CO, "Contribution", 10000)
		fake_pay(tok["musa"], CO, "Loan Repayment", 10500, loan=loan["name"])
	json.dump(state, open(OUT, "w"))
	print(json.dumps(state))

if PART == "2" or TARGET == "stage":
	state = json.load(open(OUT))
	CO, members = state["coop"], state["members"]
	loan_n = call("finance.apply_loan", tok["ngozi"], cooperative=CO, amount=15000, tenure_months=2,
		purpose="School fees for my daughter")
	wd = call("finance.request_withdrawal", A, cooperative=CO, amount=5000, reason="Hospital bill")
	state.update(loan_ngozi=loan_n["name"], withdrawal_amaka=wd["name"])
	json.dump(state, open(OUT, "w"))
	print(json.dumps(state))
