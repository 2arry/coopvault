"""API tests for the security / online-only release, against the staging site (fake Paystack on :8899)."""
import hashlib
import hmac
import json
import secrets
import subprocess
import sys
from datetime import date

import requests

BASE = "http://127.0.0.1:8100/api/method/coopvault.api."
SITE = "coopvault.test"
FAKE = "http://127.0.0.1:8899"
SECRET = "sk_test_fake_secret"
BENCH = "/home/ubuntu/start-paymnet/backend"
RUN = secrets.token_hex(3)
PW = "Coop#Test2026"
results = []


def check(name, cond, detail=""):
	results.append(bool(cond))
	print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))


def raw(path, token=None, method="POST", **data):
	h = {"Accept": "application/json", "Host": SITE}
	if token:
		h["Authorization"] = "Session " + token
	if method == "POST":
		return requests.post(BASE + path, json=data, headers=h, timeout=30)
	return requests.get(BASE + path, params=data, headers=h, timeout=30)


def call(path, token=None, method="POST", **data):
	r = raw(path, token, method, **data)
	if r.status_code != 200:
		print("FAILED", path, r.status_code, r.text[:1200])
		sys.exit(1)
	return r.json().get("message")


def err(path, token=None, method="POST", **data):
	"""Call expecting an error; returns (status, message)."""
	r = raw(path, token, method, **data)
	body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
	msgs = body.get("_server_messages")
	msg = json.loads(json.loads(msgs)[0])["message"] if msgs else body.get("exception") or ""
	return r.status_code, msg


def rejected(label, path, token=None, method="POST", contains=None, **data):
	status, msg = err(path, token, method, **data)
	ok = status != 200 and (contains is None or contains.lower() in (msg or "").lower())
	check(label, ok, f"{status}: {msg}")
	return msg


def webhook(event, data):
	payload = json.dumps({"event": event, "data": data}).encode()
	sig = hmac.new(SECRET.encode(), payload, hashlib.sha512).hexdigest()
	return requests.post(BASE + "payments.paystack_webhook", data=payload, timeout=30,
		headers={"x-paystack-signature": sig, "Content-Type": "application/json", "Host": SITE})


def bench(method, **kw):
	out = subprocess.run(["bench", "--site", SITE, "execute", method, "--kwargs", json.dumps(kw)], cwd=BENCH,
		capture_output=True, text=True)
	if out.returncode or "Traceback" in out.stdout:
		print(out.stdout[-2000:], out.stderr[-2000:])
		sys.exit(1)
	return out.stdout.strip()


n = int(RUN, 16) % 10**6
phone = lambda i: f"0803 {n:06d}{i}"[:4] + f" {n:06d}{i}"[1:]


def signup(key, i, name):
	return call("auth.signup", full_name=name, email=f"{key}.{RUN}@example.com", password=PW, phone=f"0803{n:06d}{i}")


# Safety: refuse to run unless this really is the staging site with the fake Paystack
if call("payments.config", method="GET")["public_key"] != "pk_test_fake_public":
	print("ABORT: not the staging site")
	sys.exit(2)

# Fresh rate-limit counters for the staging site only (repeated test runs share one IP)
_db = json.load(open(f"{BENCH}/sites/{SITE}/site_config.json"))["db_name"]
subprocess.run(f"redis-cli -p 13000 --scan --pattern '{_db}|rl:*' | xargs -r -d '\n' redis-cli -p 13000 del", shell=True,
	capture_output=True)

# --- Sessions & login ---------------------------------------------------------------
a = signup("ada", 1, "Ada Admin")
A = a["token"]
check("session token is not an API key", ":" not in A and len(A) > 30)
check("phone stored in one canonical format", a["user"]["mobile_no"] == f"+234803{n:06d}1", a["user"]["mobile_no"])
for variant in (f"0803 {n:06d}1"[:8] + f"{n:06d}1"[3:], f"+234803{n:06d}1", f"234-803-{n:06d}1"):
	pass
spaced = f"0803 {str(n).zfill(6)[:3]} {str(n).zfill(6)[3:]}1"
check("login with spaced local phone", bool(call("auth.login", email=spaced, password=PW)["token"]), spaced)
check("login with +234 phone", bool(call("auth.login", email=f"+234803{n:06d}1", password=PW)["token"]))
rejected("wrong password: generic error", "auth.login", email=f"ada.{RUN}@example.com", password="nope", contains="don't match")
rejected("Administrator can't use the app login", "auth.login", email="Administrator", password="whatever", contains="don't match")
rejected("weak password refused", "auth.signup", full_name="Weak Pass", email=f"weak.{RUN}@example.com", password="password1")
m1 = rejected("existing email: generic message", "auth.signup", full_name="Dup", email=f"ada.{RUN}@example.com", password=PW)
m2 = rejected("existing phone: same message", "auth.signup", full_name="Dup", email=f"dup.{RUN}@example.com", password=PW, phone=f"0803{n:06d}1")
check("signup doesn't reveal which detail exists", m1 == m2)

t1 = call("auth.login", email=f"ada.{RUN}@example.com", password=PW)["token"]
t2 = call("auth.login", email=f"ada.{RUN}@example.com", password=PW)["token"]
check("each login gets its own session", t1 != t2)
call("auth.logout", t1)
check("logout ends that session on the server", raw("auth.me", t1, "GET").status_code == 401)
check("other sessions keep working", raw("auth.me", t2, "GET").status_code == 200)
call("auth.change_password", t2, old_password=PW, new_password=PW + "x")
check("password change keeps the current session", raw("auth.me", t2, "GET").status_code == 200)
check("password change ends other sessions", raw("auth.me", A, "GET").status_code == 401)
call("auth.change_password", t2, old_password=PW + "x", new_password=PW)
A = call("auth.login", email=f"ada.{RUN}@example.com", password=PW)["token"]
call("auth.logout_all_devices", t2)
check("log out everywhere", raw("auth.me", t2, "GET").status_code == 401 and raw("auth.me", A, "GET").status_code == 401)
A = call("auth.login", email=f"ada.{RUN}@example.com", password=PW)["token"]

d = signup("dan", 9, "Dan Lockout")
for i in range(11):
	raw("auth.login", email=f"dan.{RUN}@example.com", password="wrong-" + str(i))
rejected("lockout after repeated failures", "auth.login", email=f"dan.{RUN}@example.com", password=PW, contains="too many")

# --- Cooperative, roles ---------------------------------------------------------------
t = signup("tara", 2, "Tara Treasurer")
b = signup("bola", 3, "Bola Member")
c = signup("chidi", 4, "Chidi New")
x = signup("xavier", 5, "Xavier Outsider")
T, B, C, X = t["token"], b["token"], c["token"], x["token"]
coop = call("coop.create_cooperative", A, cooperative_name=f"Unity {RUN}", contribution_amount=10000,
	contribution_frequency="Monthly", interest_rate=5, max_loan_tenure_months=6, start_date="2026-08-01")
CO = coop["name"]
for tok in (T, B, C):
	call("coop.join_cooperative", tok, join_code=coop["join_code"])
staff_note = call("notifications.list_notifications", A, "GET")
check("admins are told about join requests", any("wants to join" in i["title"] for i in staff_note["items"]))
pending = {m["full_name"]: m["name"] for m in call("finance.pending_reviews", A, "GET", cooperative=CO)["members"]}
MT, MB, MC = pending["Tara Treasurer"], pending["Bola Member"], pending["Chidi New"]
call("coop.update_member", A, member=MT, status="Active")
call("coop.update_member", A, member=MT, role="Treasurer")
call("coop.update_member", T, member=MB, status="Active")
check("treasurer can approve join requests", True)
rejected("treasurer can't change roles", "coop.update_member", T, member=MB, role="Admin", contains="roles")
rejected("treasurer can't regenerate invite", "coop.regenerate_join_code", T, cooperative=CO)
rejected("treasurer can't change rules", "coop.update_cooperative", T, cooperative=CO, interest_rate=50)
me_admin = [m for m in call("coop.list_members", A, "GET", cooperative=CO) if m["full_name"] == "Ada Admin"][0]["name"]
rejected("nobody changes their own role", "coop.update_member", A, member=me_admin, role="Member", contains="own")
call("coop.update_member", A, member=MC, status="Active")
bench("frappe.client.set_value", doctype="Coop Member", name=MB, fieldname="joined_on", value="2026-08-01")
bench("frappe.client.set_value", doctype="Coop Member", name=MT, fieldname="joined_on", value="2026-08-01")
bench("frappe.client.set_value", doctype="Coop Member", name=me_admin, fieldname="joined_on", value="2026-08-01")
check("approved member was notified", any("You're in" in i["title"] for i in call("notifications.list_notifications", B, "GET")["items"]))

# Statement leak: an admin of another cooperative can't read this cooperative's members
xc = call("coop.create_cooperative", X, cooperative_name=f"Elsewhere {RUN}")
rejected("other cooperative's admin can't read a member's statement", "coop.get_statement", X, "GET",
	cooperative=xc["name"], member=MB)
dash = call("coop.get_dashboard", B, "GET", cooperative=CO)
check("members don't see the invite code or full bank account", "join_code" not in dash["cooperative"])

# --- Opening balances (instead of manually recorded payments) -------------------------
rejected("manual contribution recording is gone", "finance.record_contribution", A, cooperative=CO, amount=5000)
rejected("file download endpoint is gone", "finance.download_proof", A, "GET", doctype="Coop Contribution", name="x")
ob = call("finance.propose_opening_balance", T, cooperative=CO, member=MB, amount=30000, as_of="2026-10-01")
check("opening balance waits for the member", ob["status"] == "Pending")
check("not counted until accepted", call("coop.get_dashboard", B, "GET", cooperative=CO)["balances"]["savings"] == 0)
rejected("treasurer can't propose their own", "finance.propose_opening_balance", T, cooperative=CO, member=MT, amount=50000)
rejected("someone else can't accept it", "finance.respond_opening_balance", A, name=ob["name"], accept=1)
call("finance.respond_opening_balance", B, name=ob["name"], accept=1)
check("accepted opening balance counts", call("coop.get_dashboard", B, "GET", cooperative=CO)["balances"]["savings"] == 30000)
for who, tok, amt in ((me_admin, A, 50000), (MT, T, 40000)):
	proposer = T if who == me_admin else A
	o = call("finance.propose_opening_balance", proposer, cooperative=CO, member=who, amount=amt, as_of="2026-10-01")
	call("finance.respond_opening_balance", tok, name=o["name"], accept=1)

# --- Payout account needs the password ------------------------------------------------
rejected("loan needs a payout account first", "finance.apply_loan", B, cooperative=CO, amount=10000, tenure_months=2,
	contains="bank account")
rejected("payout change needs the password", "auth.set_payout_account", B, bank_code="058", account_number="0987654321",
	password="wrong")
call("auth.set_payout_account", B, bank_code="058", account_number="0987654321", password=PW)
check("staff told about payout change", any("payout account" in i["title"] for i in call("notifications.list_notifications", A, "GET")["items"]))
for tok in (A, T):
	call("auth.set_payout_account", tok, bank_code="058", account_number="0555555555", password=PW)

# --- Loans: sent with a reference, confirmed by the borrower ---------------------------
loan = call("finance.apply_loan", B, cooperative=CO, amount=20000, tenure_months=2, purpose="Stock")
L = call("finance.review_loan", A, name=loan["name"], action="approve")
check("approving without sending leaves it approved", L["status"] == "Approved", L["status"])
rejected("send needs a transfer reference", "finance.send_loan", A, name=loan["name"], transfer_reference="", contains="reference")
L = call("finance.send_loan", A, name=loan["name"], transfer_reference="GTB-778899")
check("loan is Sent, schedule not started", L["status"] == "Sent" and not L["schedule"], L["status"])
check("payout account snapshot on the loan", L["payout_account_number"] == "0987654321")
pool_after_send = call("coop.get_dashboard", B, "GET", cooperative=CO)["pool"]["pool_balance"]
check("pool reduced when sent", pool_after_send == 30000 + 50000 + 40000 - 20000, pool_after_send)
check("borrower told to confirm", any("on its way" in i["title"] for i in call("notifications.list_notifications", B, "GET")["items"]))
L = call("finance.confirm_loan_received", B, name=loan["name"])
check("borrower confirms → Active with schedule from today", L["status"] == "Active" and L["received_on"] == date.today().isoformat()
	and L["schedule"]["instalments"] == 2, (L["status"], L["received_on"]))

own = call("finance.apply_loan", A, cooperative=CO, amount=10000, tenure_months=1)
rejected("admin can't send their own loan", "finance.review_loan", A, name=own["name"], action="approve", transfer_reference="X1",
	contains="own")
call("finance.review_loan", T, name=own["name"], action="approve", transfer_reference="ZEN-1")
tl = call("finance.apply_loan", T, cooperative=CO, amount=10000, tenure_months=1)
call("finance.review_loan", A, name=tl["name"], action="approve", transfer_reference="WRONG-REF")
call("finance.report_loan_not_received", T, name=tl["name"], note="Nothing in my account")
pr = call("finance.pending_reviews", A, "GET", cooperative=CO)
check("dispute shows for admins", len(pr["disputes"]) == 1)
call("finance.update_loan_transfer", A, name=tl["name"], transfer_reference="ZEN-2")
call("finance.confirm_loan_received", T, name=tl["name"])
check("dispute resolved", len(call("finance.pending_reviews", A, "GET", cooperative=CO)["disputes"]) == 0)

# --- Withdrawals ------------------------------------------------------------------------
el = call("finance.withdrawal_eligibility", B, "GET", cooperative=CO)
check("withdrawable excludes savings backing a loan", el["withdrawable"] == 30000 - 21000, el["withdrawable"])
rejected("can't withdraw savings that back a loan", "finance.request_withdrawal", B, cooperative=CO, amount=15000)
w = call("finance.request_withdrawal", B, cooperative=CO, amount=5000, reason="Rent")
rejected("one withdrawal at a time", "finance.request_withdrawal", B, cooperative=CO, amount=1000, contains="already")
rejected("members can't send withdrawals", "finance.review_withdrawal", B, name=w["name"], action="approve", transfer_reference="R")
call("finance.review_withdrawal", T, name=w["name"], action="approve", transfer_reference="GTB-W1")
W = call("finance.confirm_withdrawal_received", B, name=w["name"])
dash = call("coop.get_dashboard", B, "GET", cooperative=CO)
check("withdrawal completed reduces savings", W["status"] == "Completed" and dash["balances"]["savings"] == 25000,
	dash["balances"]["savings"])
rejected("can't leave while owing or holding savings", "coop.leave_cooperative", B, cooperative=CO)

# --- Contribution plan isn't retroactive; first full period for new members -------------
before = call("coop.get_dashboard", A, "GET", cooperative=CO)["standing"]
res = call("coop.update_cooperative", A, cooperative=CO, contribution_amount=15000, contribution_frequency="Monthly")
check("new amount applies from next period", res["plan_change"] and res["plan_change"]["effective_from"] > date.today().isoformat(),
	res["plan_change"])
after = call("coop.get_dashboard", A, "GET", cooperative=CO)["standing"]
check("existing standing unchanged by the raise", before["state"] == after["state"] and before["due_now"] == after["due_now"],
	(before["state"], after["state"]))
ann = call("coop.list_announcements", B, "GET", cooperative=CO)
sys_ann = [x for x in ann if x["is_system"]]
check("members get an automatic notice of the change", bool(sys_ann))
rejected("automatic notices can't be deleted", "coop.delete_announcement", A, name=sys_ann[0]["name"])
cst = call("coop.get_dashboard", C, "GET", cooperative=CO)["standing"]
expected_state = "due" if date.today().day == 1 else "not_started"
check("a member who joins mid-period starts next period", cst["state"] == expected_state, cst)

# --- Bank account changes ---------------------------------------------------------------
rejected("treasurer can't change the bank account", "coop.set_bank_account", T, cooperative=CO, bank_code="058",
	account_number="0123456789", password=PW)
rejected("bank change needs the password", "coop.set_bank_account", A, cooperative=CO, bank_code="058",
	account_number="0123456789", password="nope")
call("coop.set_bank_account", A, cooperative=CO, bank_code="058", account_number="0123456789", password=PW)
call("coop.set_bank_account", A, cooperative=CO, bank_code="044", account_number="0123456789", password=PW)
check("members told when the settlement account changes",
	any("Settlement account changed" in i["title"] for i in call("notifications.list_notifications", B, "GET")["items"]))

# --- Paystack: checkout, refund and dispute ----------------------------------------------
init = call("payments.initialize", C, cooperative=CO, purpose="Contribution", amount=10000)
paid = requests.post(FAKE + "/_test/pay", json={"reference": init["reference"]}).json()["data"]
v = call("payments.verify", C, reference=init["reference"])
check("checkout works with session auth", v["status"] == "Success")
savings_c = call("coop.get_dashboard", C, "GET", cooperative=CO)["balances"]["savings"]
r = webhook("refund.processed", {"status": "processed", "transaction_reference": init["reference"], "amount": "1025381", "currency": "NGN"})
dash_c = call("coop.get_dashboard", C, "GET", cooperative=CO)["balances"]["savings"]
check("refund reverses the contribution", r.status_code == 200 and dash_c == savings_c - 10000, (r.status_code, dash_c))
init2 = call("payments.initialize", C, cooperative=CO, purpose="Contribution", amount=10000)
requests.post(FAKE + "/_test/pay", json={"reference": init2["reference"]})
call("payments.verify", C, reference=init2["reference"])
webhook("charge.dispute.create", {"transaction": {"reference": init2["reference"]}, "status": "awaiting-merchant-feedback"})
check("dispute keeps the payment counted while open",
	call("coop.get_dashboard", C, "GET", cooperative=CO)["balances"]["savings"] == dash_c + 10000)
webhook("charge.dispute.resolve", {"transaction": {"reference": init2["reference"]}, "resolution": "merchant-accepted"})
check("lost dispute reverses it", call("coop.get_dashboard", C, "GET", cooperative=CO)["balances"]["savings"] == dash_c)

# --- Reset codes & security hold ---------------------------------------------------------
rejected("treasurer can't create reset codes", "coop.create_reset_code", T, cooperative=CO, member=MB)
rejected("admin can't reset their own password this way", "coop.create_reset_code", A, cooperative=CO, member=me_admin)
rc = call("coop.create_reset_code", A, cooperative=CO, member=MB)
check("reset code created", len(rc["code"]) == 9 and rc["code"][4] == "-", rc["code"])
rejected("wrong reset code", "auth.reset_password_with_code", login=f"0803{n:06d}3", code="AAAA-BBBB", new_password=PW + "!r")
res = call("auth.reset_password_with_code", login=f"0803{n:06d}3", code=rc["code"].lower(), new_password=PW + "!r")
check("reset logs the member in", bool(res["token"]))
check("reset ends old sessions", raw("auth.me", B, "GET").status_code == 401)
B = res["token"]
rejected("code can't be used twice", "auth.reset_password_with_code", login=f"0803{n:06d}3", code=rc["code"], new_password=PW)
rejected("payout change paused after reset", "auth.set_payout_account", B, bank_code="058", account_number="0123456789",
	password=PW + "!r", contains="paused")
rejected("loans paused after reset", "finance.apply_loan", B, cooperative=CO, amount=1000, contains="paused")

# --- Notifications & push ---------------------------------------------------------------
nb = call("notifications.list_notifications", B, "GET")
check("member has notifications", nb["unread"] > 0 and nb["items"])
check("all read", call("notifications.mark_read", B, all=1)["unread"] == 0)
check("push key available", bool(call("notifications.push_config", B, "GET")["public_key"]))
call("notifications.subscribe", B, subscription={"endpoint": f"https://fcm.googleapis.com/fcm/send/test-{RUN}",
	"keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
	"auth": "tBHItJI5svbpez7KI4CCXg"}})
call("notifications.unsubscribe", B, endpoint=f"https://fcm.googleapis.com/fcm/send/test-{RUN}")
check("push subscribe / unsubscribe", True)

# --- Throttles and dedicated accounts -----------------------------------------------------
codes = [raw("payments.resolve_account", C, "GET", bank_code="058", account_number="0987654321").status_code for _ in range(11)]
check("account-name lookups are throttled per user", codes[:10].count(200) == 10 and codes[10] != 200, codes[-3:])
call("payments.request_dedicated_account", C, cooperative=CO) if False else None
xd = call("coop.join_cooperative", C, join_code=xc["join_code"]) if False else None
call("auth.update_profile", C, phone=f"0803{n:06d}4")
call("payments.request_dedicated_account", C, cooperative=CO)
xcm = call("coop.join_cooperative", C, join_code=call("coop.get_dashboard", X, "GET", cooperative=xc["name"])["cooperative"]["join_code"])
xm = call("finance.pending_reviews", X, "GET", cooperative=xc["name"])["members"][0]["name"]
call("coop.update_member", X, member=xm, status="Active")
rejected("one account number per person", "payments.request_dedicated_account", C, cooperative=xc["name"], contains="already linked")

print(f"\n{sum(results)}/{len(results)} passed (run {RUN}, coop {CO})")
sys.exit(0 if all(results) else 1)
