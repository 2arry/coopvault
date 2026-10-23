"""Add officers and approval examples to the Unity Savings Circle demo (run after seed_v3.py).

usage: seed_officers.py stage|prod PASSWORD
Adds a Chairman, an Accountant and a Loan Officer; makes loans from NGN 20,000 need the
Chairman's sign-off; leaves an offline cash payment waiting for a second officer; posts an
approved expense journal. Safe to re-run: existing people and records are reused.
"""
import json
import os
import sys
from datetime import date

import requests

TARGET, PASSWORD = sys.argv[1], sys.argv[2]
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")  # git-ignored
os.makedirs(S, exist_ok=True)
if TARGET == "stage":
	BASE, STATE = "http://127.0.0.1:8100/api/method/coopvault.api.", f"{S}/demo_stage.json"
	H = {"Accept": "application/json", "Host": "coopvault.test"}
else:
	BASE, STATE = "https://api-payment.secureestate.app/api/method/coopvault.api.", f"{S}/demo_prod.json"
	H = {"Accept": "application/json", "Origin": "https://pay-frappe.web.app"}
state = json.load(open(STATE))
CO = state["coop"]

OFFICERS = [
	("kemi", "Kemi Adebayo", "08031000008", "Chairman"),
	("femi", "Femi Okon", "08031000009", "Accountant"),
	("lola", "Lola Sanni", "08031000010", "Loan Officer"),
]


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


A, T = session("amaka"), session("tunde")
tok = {k: session(k, n, p) for k, n, p, _r in OFFICERS}
members = {m["user"]: m for m in call("coop.list_members", A, "GET", cooperative=CO)}
join_code = state["join_code"]
for key, name, _p, role in OFFICERS:
	m = members.get(f"{key}@example.com")
	if not m:
		call("coop.join_cooperative", tok[key], join_code=join_code)
		m = next(x for x in call("coop.list_members", A, "GET", cooperative=CO) if x["user"] == f"{key}@example.com")
	if m["status"] == "Pending":
		call("coop.update_member", A, member=m["name"], status="Active")
	if m["role"] != role:
		call("coop.update_member", A, member=m["name"], role=role)
	state.setdefault("members", {})[key] = m["name"]

# Loans from NGN 20,000 need the Chairman too (the default is NGN 500,000), so the demo shows two stages
rules = call("approvals.rules", A, "GET", cooperative=CO)["rules"]
if not any(r["action"] == "Loan" and r["min_amount"] == 20000 for r in rules):
	for r in rules:
		if r["action"] == "Loan" and r["min_amount"] == 500000:
			call("approvals.delete_rule", A, name=r["name"])
	call("approvals.save_rule", A, cooperative=CO, action="Loan", min_amount=20000,
		stages=[{"label": "Review", "roles": ["Loan Officer", "Treasurer", "Admin"]},
			{"label": "Final approval", "roles": ["Chairman", "Admin"]}])

# Femi (Accountant) records Ify's cash contribution; it waits for another officer to approve
ify = state["members"]["ify"]
if not any(a["action"] == "Offline payment" for a in call("approvals.pending", A, "GET", cooperative=CO)):
	call("finance.record_offline_payment", tok["femi"], cooperative=CO, member=ify, amount=10000,
		payment_date=date.today().isoformat(), reference="", payment_method="Cash",
		note="Paid in cash at the October meeting")

# Femi prepares the hall rent; Tunde approves it
if not call("accounts.journals", A, "GET", cooperative=CO, voucher_type="Manual Journal"):
	acct = {a["account_type"]: a["name"] for a in call("accounts.chart", A, "GET", cooperative=CO)["accounts"]}
	j = call("accounts.create_journal", tok["femi"], cooperative=CO, posting_date=date.today().isoformat(),
		remarks="Hall rent for the October meeting",
		lines=[{"account": acct["Operating Expense"], "debit": 2500}, {"account": acct["Bank"], "credit": 2500}])
	call("accounts.approve_journal", T, name=j["name"])

# A second loan product that needs a guarantor; Musa asks Ngozi to guarantee one
products = call("finance.admin_loan_products", A, "GET", cooperative=CO)
emergency = next((p for p in products if p["product_name"] == "Emergency loan"), None)
if not emergency:
	products = call("finance.save_loan_product", A, cooperative=CO, product_name="Emergency loan",
		description="For the unexpected: hospital bills, urgent repairs. Needs one member to guarantee half of it.",
		interest_method="Reducing balance", interest_rate=2, rate_period="Per month", min_tenure_months=2, max_tenure_months=6,
		processing_fee_percent=1, penalty_percent=5, grace_days=3, guarantors_required=1, guarantee_percent=50,
		max_active_loans=2, require_up_to_date=0)
	emergency = next(p for p in products if p["product_name"] == "Emergency loan")
musa = session("musa")
if not any(l.get("product") == emergency["name"] for l in call("finance.list_loans", musa, "GET", cooperative=CO)):
	call("finance.apply_loan", musa, cooperative=CO, amount=8000, tenure_months=4, product=emergency["name"],
		purpose="Fix the shop's freezer", guarantors=[{"member": state["members"]["ngozi"], "amount": 4000}])

json.dump(state, open(STATE, "w"))
print(json.dumps({k: v for k, v in state.items() if k != "members"}))
