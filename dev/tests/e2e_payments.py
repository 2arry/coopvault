"""End-to-end API test for CoopVault on the staging bench (fake Paystack on :8899)."""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from datetime import date

import requests

HOST = "http://127.0.0.1:8100"
BASE = HOST + "/api/method/coopvault.api."
FAKE = "http://127.0.0.1:8899"
SECRET = "sk_test_fake_secret"
BENCH = "/home/ubuntu/start-paymnet/backend"
RUN = secrets.token_hex(3)
results = []


def check(name, cond, detail=""):
	results.append(bool(cond))
	print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))


def call(path, token=None, method="POST", expect_error=False, **data):
	h = {"Accept": "application/json", "Host": "coopvault.test"}
	if token:
		h["Authorization"] = "Session " + token
	if method == "POST":
		r = requests.post(BASE + path, json=data, headers=h, timeout=30)
	else:
		r = requests.get(BASE + path, params=data, headers=h, timeout=30)
	body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
	if expect_error:
		msgs = body.get("_server_messages")
		msg = json.loads(json.loads(msgs)[0])["message"] if msgs else body.get("exception")
		check(f"{path} rejected", r.status_code != 200, msg)
		return msg
	if r.status_code != 200:
		print("FAILED", path, r.status_code, json.dumps(body)[:1500])
		sys.exit(1)
	return body.get("message")


def fake(path, **data):
	return requests.post(FAKE + path, json=data, timeout=10).json()


def webhook(event, data, secret=SECRET):
	payload = json.dumps({"event": event, "data": data}).encode()
	sig = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
	return requests.post(BASE + "payments.paystack_webhook", data=payload,
		headers={"x-paystack-signature": sig, "Content-Type": "application/json", "Host": "coopvault.test"}, timeout=30)


def bench_exec(method, **kwargs):
	out = subprocess.run(["bench", "--site", "coopvault.test", "execute", method, "--kwargs", json.dumps(kwargs)],
		cwd=BENCH, capture_output=True, text=True)
	if out.returncode:
		print(out.stdout[-2000:], out.stderr[-2000:])
		sys.exit(1)
	return out.stdout.strip()


def fake_paid(reference, **kw):
	return fake("/_test/pay", reference=reference, **kw)


if call("payments.config", method="GET")["public_key"] != "pk_test_fake_public":
	sys.exit("ABORT: not the staging site")
_db = json.load(open(f"{BENCH}/sites/coopvault.test/site_config.json"))["db_name"]
subprocess.run(f"redis-cli -p 13000 --scan --pattern '{_db}|rl:*' | xargs -r -d '\n' redis-cli -p 13000 del", shell=True, capture_output=True)
PWA, PWB = "Passw0rd!x-coop", "Passw0rd!y-coop"

# --- Accounts & cooperative -------------------------------------------------------
phone = lambda n: "080" + str(int(RUN, 16) % 10**7).zfill(7) + str(n)
a = call("auth.signup", full_name="Ada Admin", email=f"ada.{RUN}@example.com", password=PWA, phone=phone(1))
b = call("auth.signup", full_name="Tunde Bakare", email=f"tunde.{RUN}@example.com", password=PWB, phone=phone(2))
c = call("auth.signup", full_name="Chioma Okafor", email=f"chioma.{RUN}@example.com", password="Passw0rd!z-coop")
A, B, C = a["token"], b["token"], c["token"]
check("login by phone gives a new session", call("auth.login", email=phone(2), password=PWB)["token"] not in (B, None))

coop = call("coop.create_cooperative", A, cooperative_name=f"Unity Savings {RUN}", interest_rate=10,
	contribution_amount=10000, contribution_frequency="Monthly", fee_bearer="Member", start_date=date.today().replace(day=1).isoformat())
CO = coop["name"]
import harness  # noqa: E402  (a platform operator approves the group)
harness.call("platform.review_group", harness.operator(), name=CO, approve=1)
call("coop.set_bank_account", A, cooperative=CO, bank_code="058", account_number="1111111111", password=PWA, expect_error=True)
coop_info = call("coop.set_bank_account", A, cooperative=CO, bank_code="058", account_number="0123456789", password=PWA)
check("bank verified by name", coop_info["account_name"] == "UNITY SAVINGS CIRCLE" and coop_info["account_verified"] == 1,
	coop_info["account_name"])
sub = [l for l in requests.get(FAKE + "/_test/log").json() if l["path"] == "/subaccount"][-1]["body"]
check("subaccount created with 0% platform share", sub["settlement_bank"] == "058" and sub["percentage_charge"] == 0, sub)
call("coop.set_bank_account", B, cooperative=CO, bank_code="058", account_number="0123456789", password=PWB, expect_error=True)

for t in (B, C):
	call("coop.join_cooperative", t, join_code=coop["join_code"])
pend = call("finance.pending_reviews", A, method="GET", cooperative=CO)["members"]
MB = [m["name"] for m in pend if m["full_name"] == "Tunde Bakare"][0]
MC = [m["name"] for m in pend if m["full_name"] == "Chioma Okafor"][0]
for m in (MB, MC):
	call("coop.update_member", A, member=m, status="Active")
first = date.today().replace(day=1).isoformat()
for m in [MB, MC] + [x["name"] for x in call("coop.list_members", A, method="GET", cooperative=CO) if x["full_name"] == "Ada Admin"]:
	bench_exec("frappe.client.set_value", doctype="Coop Member", name=m, fieldname="joined_on", value=first)

# --- Contribution standing ---------------------------------------------------------
d = call("coop.get_dashboard", B, method="GET", cooperative=CO)
st = d["standing"]
check("new member owes this month", st["state"] == "due" and st["due_now"] == 10000, st["state"])
check("dashboard exposes payment config", d["payments"]["online"] and d["payments"]["settles_to_cooperative"])

c1 = call("finance.propose_opening_balance", A, cooperative=CO, member=MB, amount=20000)
call("finance.respond_opening_balance", B, name=c1["name"], accept=1)
st = call("coop.get_dashboard", B, method="GET", cooperative=CO)["standing"]
check("paid two months ahead", st["state"] == "up_to_date" and st["periods_covered"] == 2, st)

tr = call("coop.contribution_tracker", A, method="GET", cooperative=CO)
states = {r["full_name"]: r["standing"]["state"] for r in tr["members"]}
check("tracker shows who owes", states.get("Chioma Okafor") == "due" and states.get("Tunde Bakare") == "up_to_date", states)
check("tracker sorts owing first", tr["members"][0]["standing"]["state"] in ("due", "behind"))
check("period summary", tr["period"]["collected"] == 20000 and tr["period"]["members"] == 3, tr["period"])
call("coop.contribution_tracker", B, method="GET", cooperative=CO, expect_error=True)

# --- Loans & schedule --------------------------------------------------------------
el = call("finance.loan_eligibility", B, method="GET", cooperative=CO)
check("borrow limit = savings", el["borrow_limit"] == 20000, el["borrow_limit"])
call("finance.apply_loan", B, cooperative=CO, amount=25000, tenure_months=2, expect_error=True)
prof = call("auth.set_payout_account", B, bank_code="058", account_number="0987654321", password=PWB)
check("payout account verified", prof["payout_account"]["payout_account_name"] == "TUNDE BAKARE")
loan = call("finance.apply_loan", B, cooperative=CO, amount=20000, tenure_months=2, purpose="School fees")
call("finance.review_loan", A, name=loan["name"], action="approve", transfer_reference="GTB-1")
L = call("finance.confirm_loan_received", B, name=loan["name"])
check("loan schedule two instalments", L["schedule"]["instalments"] == 2 and L["schedule"]["next_amount"] == 11000,
	L["schedule"])
el = call("finance.loan_eligibility", B, method="GET", cooperative=CO)
check("limit uses balance owed (round numbers)", el["borrow_limit"] == 0, el["borrow_limit"])

# --- Payout account ----------------------------------------------------------------
L = call("finance.get_loan", A, method="GET", name=loan["name"])
check("treasurer sees payout account", L["payout_account"]["payout_account_number"] == "0987654321")

# --- Checkout: repayment with member-paid fee --------------------------------------
q = call("payments.quote", B, method="GET", cooperative=CO, amount=11000)
check("fee passed to member", q["fee"] == 269.04 and q["total"] == 11269.04, q)
small = call("payments.quote", B, method="GET", cooperative=CO, amount=2000)
check("flat fee waived for small amounts", small["fee"] == 30.46, small)
init = call("payments.initialize", B, cooperative=CO, purpose="Loan Repayment", amount=11000, loan=loan["name"])
check("initialize returns access code", init["access_code"].startswith("ac_"))
ftx = requests.get(FAKE + "/_test/tx", params={"reference": init["reference"]}).json()
check("charge goes to cooperative subaccount", (ftx["subaccount"] or "").startswith("ACCT_"), ftx["subaccount"])
check("charged amount includes fee", ftx["amount"] == 1126904 and ftx["bearer"] == "subaccount", ftx["amount"])
v = call("payments.verify", B, reference=init["reference"])
check("unpaid checkout not recorded", v["status"] in ("Initiated", "Abandoned"), v["status"])
fake_paid(init["reference"])
v = call("payments.verify", B, reference=init["reference"])
check("paid checkout recorded", v["status"] == "Success" and v["document_type"] == "Coop Loan Repayment", v)
L = call("finance.get_loan", B, method="GET", name=loan["name"])
check("loan balance after online repayment", L["balance"] == 11000, L["balance"])
call("payments.verify", C, reference=init["reference"], expect_error=True)

# Overpayment: a second payment lands while another was still open
init2 = call("payments.initialize", B, cooperative=CO, purpose="Loan Repayment", amount=11000, loan=loan["name"])
init2b = call("payments.initialize", B, cooperative=CO, purpose="Loan Repayment", amount=5000, loan=loan["name"])
fake_paid(init2b["reference"])
call("payments.verify", B, reference=init2b["reference"])
fake_paid(init2["reference"])
call("payments.verify", B, reference=init2["reference"])
L = call("finance.get_loan", B, method="GET", name=loan["name"])
check("loan cleared by two online payments", L["status"] == "Repaid", L["status"])
contribs = call("finance.list_contributions", B, method="GET", cooperative=CO)
check("overpayment saved, not lost", any(x["amount"] == 5000 and "Overpayment" in (x["notes"] or "") for x in contribs),
	[(x["amount"], x["notes"]) for x in contribs])

# --- Webhook ------------------------------------------------------------------------
init3 = call("payments.initialize", C, cooperative=CO, purpose="Contribution", amount=10000)
paid = fake_paid(init3["reference"])["data"]
r = webhook("charge.success", paid, secret="wrong")
check("webhook rejects bad signature", r.status_code == 401, r.status_code)
r = webhook("charge.success", paid)
r2 = webhook("charge.success", paid)
contribs = call("finance.list_contributions", C, method="GET", cooperative=CO)
check("webhook records once (idempotent)", r.status_code == 200 and r2.status_code == 200 and len(contribs) == 1,
	len(contribs))

init4 = call("payments.initialize", C, cooperative=CO, purpose="Contribution", amount=5000)
fake_paid(init4["reference"], amount=100000)
v = call("payments.verify", C, reference=init4["reference"])
check("underpayment rejected", v["status"] == "Failed", v)

# --- Saved card + Autosave ----------------------------------------------------------
init5 = call("payments.initialize", C, cooperative=CO, purpose="Contribution", amount=10000, setup_autosave=1)
ftx = requests.get(FAKE + "/_test/tx", params={"reference": init5["reference"]}).json()
check("autosave checkout is card-only", ftx["channels"] == ["card"], ftx["channels"])
fake_paid(init5["reference"])
v = call("payments.verify", C, reference=init5["reference"])
auto = call("payments.get_autosave", C, method="GET", cooperative=CO)
check("card saved and autosave on", auto["autosave"]["enabled"] and len(auto["methods"]) == 1, auto["autosave"])
check("autosave starts next period", auto["autosave"]["next_date"] > date.today().isoformat(), auto["autosave"]["next_date"])

call("payments.setup_autosave", C, cooperative=CO, amount=10000, authorization=auto["methods"][0]["name"],
	start_date=date.today().isoformat())
before = len(call("finance.list_contributions", C, method="GET", cooperative=CO))
bench_exec("coopvault.api.payments.run_autosave")
after = call("finance.list_contributions", C, method="GET", cooperative=CO)
auto = call("payments.get_autosave", C, method="GET", cooperative=CO)
check("scheduler charged autosave", len(after) == before + 1 and after[0]["payment_method"] == "Paystack", len(after))
check("autosave moved to next month", auto["autosave"]["next_date"] > date.today().isoformat(), auto["autosave"]["next_date"])
call("payments.setup_autosave", B, cooperative=CO, amount=10000, authorization=auto["methods"][0]["name"], expect_error=True)

# Declined card stops after 3 failures
uc = c["user"]["name"]
bench_exec("frappe.client.insert", doc={"doctype": "Coop Payment Authorization", "user": uc, "label": "Bad card",
	"channel": "card", "status": "Active", "authorization_code": "AUTH_DECLINE", "email": uc})
methods = call("payments.list_methods", C, method="GET")
bad = [m for m in methods if m["label"] == "Bad card"][0]["name"]
for i in range(3):
	call("payments.setup_autosave", C, cooperative=CO, amount=10000, authorization=bad, start_date=date.today().isoformat()) if i == 0 else None
	bench_exec("coopvault.api.payments.run_autosave")
auto = call("payments.get_autosave", C, method="GET", cooperative=CO)
check("autosave stops after 3 declines", not auto["autosave"]["enabled"] and "3 failed" in (auto["autosave"]["last_error"] or ""),
	auto["autosave"])

# --- Direct debit mandate ----------------------------------------------------------
md = call("payments.start_mandate", B, cooperative=CO)
check("mandate redirect", md["redirect_url"].startswith("https://link.paystack.com/"))
m1 = call("payments.check_mandate", B, reference=md["reference"])
check("mandate pending until bank approves", m1["status"] == "Pending")
fake("/_test/mandate", reference=md["reference"])
m1 = call("payments.check_mandate", B, reference=md["reference"])
check("mandate active after approval", m1["status"] == "Active" and m1["channel"] == "direct_debit", m1)
md2 = call("payments.start_mandate", B, cooperative=CO)
webhook("direct_debit.authorization.active", {"authorization_code": "AUTH_DDhook", "active": True, "bank": "Access Bank",
	"reference": md2["reference"], "customer": {"code": "CUS_x", "email": b["user"]["email"]}})
labels = [m["label"] for m in call("payments.list_methods", B, method="GET")]
check("mandate activated by webhook", "Access Bank (direct debit)" in labels, labels)

# --- Dedicated virtual account ------------------------------------------------------
call("payments.request_dedicated_account", C, cooperative=CO, expect_error=True)  # no phone
dva = call("payments.request_dedicated_account", B, cooperative=CO)
check("dedicated account requested", dva["status"] == "Requested")
assign = [l for l in requests.get(FAKE + "/_test/log").json() if l["path"] == "/dedicated_account/assign"][-1]["body"]
check("dva split to cooperative + uses payout account", assign.get("subaccount") and assign["preferred_bank"] == "test-bank",
	assign)
acct = "99" + str(int(RUN, 16) % 10**8).zfill(8)
webhook("dedicatedaccount.assign.success", {"customer": {"email": b["user"]["email"], "customer_code": "CUS_tb"},
	"dedicated_account": {"account_number": acct, "account_name": "PAYSTACK/Tunde Bakare", "bank": {"name": "Test Bank"}},
	"identification": {"status": "success"}})
d = call("coop.get_dashboard", B, method="GET", cooperative=CO)
check("dedicated account assigned", d["dedicated_account"]["account_number"] == acct, d["dedicated_account"])
before = d["balances"]["total_contributed"]
tdata = fake("/_test/transfer", reference=f"DVA-{RUN}", amount=1500000, email=b["user"]["email"], account_number=acct)["data"]
webhook("charge.success", tdata)
webhook("charge.success", tdata)
d = call("coop.get_dashboard", B, method="GET", cooperative=CO)
# Members bear the transfer fee here, so they're credited what arrived (the fake keeps 1.5%)
check("transfer to dedicated account saved once", d["balances"]["total_contributed"] == before + 15000 - (15000 * 0.015 + 100),
	d["balances"]["total_contributed"] - before)

# --- Receipts & misc ----------------------------------------------------------------
rec = call("finance.get_receipt", B, method="GET", doctype="Coop Contribution", name=c1["name"])
check("receipt", rec["amount"] == 20000 and rec["cooperative_name"].startswith("Unity") and rec["kind"] == "Opening balance",
	rec)
call("finance.get_receipt", C, method="GET", doctype="Coop Contribution", name=c1["name"], expect_error=True)
st = call("coop.get_statement", A, method="GET", cooperative=CO, member=MB)
check("member statement has standing", st["standing"]["tracked"] and st["member"]["payout_account_name"] == "TUNDE BAKARE")
r = requests.options(BASE + "auth.me", headers={"Host": "coopvault.test", "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
	"Access-Control-Request-Headers": "authorization"}, timeout=30)
check("CORS preflight", r.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173")

print(f"\n{sum(results)}/{len(results)} passed (run {RUN}, coop {CO})")
sys.exit(0 if all(results) else 1)
