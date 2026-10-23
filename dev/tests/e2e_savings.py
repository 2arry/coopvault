"""Phase 5: savings products, split payments and shared-out transfers, meetings, and members added by hand."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from harness import (
	FAKE, N, PW, RUN, call, check, console, done, fake, get, guard, new_cooperative, pay, raw, rejected, signup, webhook,
)

guard()
people = {k: signup(k, i, n) for i, (k, n) in enumerate([("ada", "Ada Admin"), ("tr", "Tolu Treasurer"),
	("se", "Sade Secretary"), ("lo", "Lanre Loans"), ("m1", "Musa Saver"), ("m2", "Mary Leaver")], 1)}
T = {k: v["token"] for k, v in people.items()}
coop, ids = new_cooperative(T["ada"], "Savings", [(T["tr"], "Treasurer"), (T["se"], "Secretary"), (T["lo"], "Loan Officer"),
	(T["m1"], "Member"), (T["m2"], "Member")])
CO = coop["name"]
M1, M2 = ids[T["m1"]], ids[T["m2"]]
call("coop.set_bank_account", T["ada"], cooperative=CO, bank_code="001", account_number="0099887755", password=PW)
for k, n in (("m1", "01"), ("m2", "02")):
	call("auth.set_payout_account", T[k], bank_code="001", account_number="00123457" + n, password=PW)
	console(f"frappe.db.set_value('Coop Member', '{ids[T[k]]}', 'payout_changed_on', '2026-01-01 00:00:00')")


def dash(k):
	return get("coop.get_dashboard", T[k], cooperative=CO)


def product_row(d, name):
	return next(p for p in d["balances"]["products"] if p["product_name"] == name)


def account_balance(code):
	out = console(f"""
acc = frappe.db.get_value('Coop Account', {{'cooperative': '{CO}', 'account_code': '{code}'}})
print('BAL=%s' % frappe.db.sql("select coalesce(sum(credit-debit),0) from `tabCoop Journal Line` where account=%s and is_posted=1", acc)[0][0])
""")
	return float(out.split("BAL=")[1].split()[0])


# --- Savings products ------------------------------------------------------------------------
sp = get("finance.savings_products", T["m1"], cooperative=CO)
check("every group starts with regular contributions", [p["product_name"] for p in sp] == ["Regular contributions"], sp)
rejected("members can't create savings products", "finance.save_savings_product", T["m1"], cooperative=CO, product_name="X")
rejected("the treasurer can't either (settings are for admins)", "finance.save_savings_product", T["tr"], cooperative=CO,
	product_name="X")
rows = call("finance.save_savings_product", T["ada"], cooperative=CO, product_name="Christmas savings",
	description="For the end of year", withdrawal_rule="Any time", counts_for_borrowing=0)
XMAS = next(p for p in rows if p["product_name"] == "Christmas savings")
rows = call("finance.save_savings_product", T["ada"], cooperative=CO, product_name="Retirement",
	withdrawal_rule="Only when leaving", counts_for_borrowing=1)
RET = next(p for p in rows if p["product_name"] == "Retirement")
REG = next(p for p in rows if p["kind"] == "Regular contributions")
check("voluntary products are created as voluntary", XMAS["kind"] == "Voluntary savings" and RET["kind"] == "Voluntary savings")
rejected("product names are unique", "finance.save_savings_product", T["ada"], cooperative=CO, product_name="Retirement",
	contains="already")
rows = call("finance.save_savings_product", T["ada"], cooperative=CO, name=REG["name"], product_name="Monthly dues", enabled=0)
REG = next(p for p in rows if p["name"] == REG["name"])
check("regular contributions can be renamed but never switched off", REG["product_name"] == "Monthly dues" and REG["enabled"] == 1,
	REG)
chart = get("accounts.chart", T["ada"], cooperative=CO)
accts = {a["account_code"]: a for a in (chart["accounts"] if isinstance(chart, dict) else chart)}
check("each voluntary product has its own savings account in the books",
	accts.get("2110", {}).get("account_name") == "Christmas savings" and accts.get("2120", {}).get("account_name") == "Retirement"
	and accts["2110"]["account_type"] == "Member Savings", {k: v["account_name"] for k, v in accts.items() if k.startswith("21")})

# --- Paying into a product -----------------------------------------------------------------------
pay(T["m1"], CO, 10000)
pay(T["m1"], CO, 5000, savings_product=XMAS["name"])
pay(T["m1"], CO, 8000, savings_product=RET["name"])
d = dash("m1")
check("balances by product", (product_row(d, "Monthly dues")["balance"], product_row(d, "Christmas savings")["balance"],
	product_row(d, "Retirement")["balance"]) == (10000, 5000, 8000), d["balances"]["products"])
check("total savings counts every product", d["balances"]["savings"] == 23000, d["balances"]["savings"])
check("only regular contributions count as dues", d["balances"]["dues_paid"] == 10000, d["balances"]["dues_paid"])
check("savings that don't count for borrowing are left out of the limit", d["balances"]["borrow_limit"] == 18000,
	d["balances"]["borrow_limit"])
check("the books hold each product's money in its own account", (account_balance("2110"), account_balance("2120")) == (5000, 8000))
st = get("coop.get_statement", T["m1"], cooperative=CO)
check("the statement names the product", any(e.get("product") == "Christmas savings" for e in st["entries"]))

# --- Withdrawing from a product --------------------------------------------------------------------
el = get("finance.withdrawal_eligibility", T["m1"], cooperative=CO)
rows = {p["product_name"]: p for p in el["products"]}
check("what can come out of each product", (rows["Christmas savings"]["withdrawable"], rows["Retirement"]["withdrawable"],
	rows["Retirement"]["locked"], rows["Monthly dues"]["withdrawable"]) == (5000, 0, True, 10000), rows)
rejected("retirement savings stay until you leave", "finance.request_withdrawal", T["m1"], cooperative=CO, amount=1000,
	savings_product=RET["name"], contains="when you leave")
rejected("can't take more than the product holds", "finance.request_withdrawal", T["m1"], cooperative=CO, amount=6000,
	savings_product=XMAS["name"], contains="at most")
w = call("finance.request_withdrawal", T["m1"], cooperative=CO, amount=3000, savings_product=XMAS["name"], reason="Gifts")
ap = call("approvals.for_reference", T["ada"], reference_doctype="Coop Withdrawal", reference_name=w["name"])
check("the approval says which product", "Christmas savings" in (ap.get("summary") or ""), ap.get("summary"))
call("finance.review_withdrawal", T["tr"], name=w["name"], action="approve", transfer_reference="TRF-XMAS-1")
call("finance.confirm_withdrawal_received", T["m1"], name=w["name"])
d = dash("m1")
check("the withdrawal came out of Christmas savings only", (product_row(d, "Christmas savings")["balance"],
	product_row(d, "Monthly dues")["balance"], account_balance("2110")) == (2000, 10000, 2000))

# --- A loan, then one payment for dues + instalment + savings --------------------------------------------
std = get("finance.loan_products", T["m1"], cooperative=CO)[0]
loan = call("finance.apply_loan", T["m1"], cooperative=CO, amount=9000, tenure_months=3, product=std["name"], purpose="Stock")
ap = call("approvals.for_reference", T["lo"], reference_doctype="Coop Loan", reference_name=loan["name"])
call("approvals.decide", T["lo"], name=ap["name"], approve=1)
call("finance.send_loan", T["tr"], name=loan["name"], transfer_reference="TRF-L1")
call("finance.confirm_loan_received", T["m1"], name=loan["name"])
L = get("finance.get_loan", T["m1"], name=loan["name"])
check("loan active", L["status"] == "Active", L["status"])

# Two months of dues fall behind: the group started (and Musa and Mary joined) two months ago
start = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
start = (start - timedelta(days=1)).replace(day=1)
console(f"""
frappe.db.set_value('Cooperative', '{CO}', 'start_date', '{start}')
frappe.db.sql("update `tabCoop Contribution Plan` set effective_from=%s where parent=%s", ('{start}', '{CO}'))
for m in ('{M1}', '{M2}'):
	frappe.db.set_value('Coop Member', m, 'joined_on', '{start}')
""")
plan = get("finance.payment_plan", T["m1"], cooperative=CO)
due = plan["due"]
check("what's due: dues first, then the loan instalment", [x["kind"] for x in due] == ["savings", "loan"] and
	due[0]["amount"] == 20000 and due[0]["savings_product"] == REG["name"] and due[1]["loan"] == loan["name"], due)
inst = due[1]["amount"]
rejected("a split can't pay more than a loan's balance", "payments.initialize", T["m1"], cooperative=CO, purpose="Split",
	allocations=[{"kind": "loan", "loan": loan["name"], "amount": 99999}], contains="more than")
rejected("a split needs at least one amount", "payments.initialize", T["m1"], cooperative=CO, purpose="Split",
	allocations=[{"kind": "savings", "savings_product": XMAS["name"], "amount": 0}])
tx = pay(T["m1"], CO, None, purpose="Split", allocations=[
	{"kind": "savings", "savings_product": REG["name"], "amount": 20000},
	{"kind": "loan", "loan": loan["name"], "amount": inst},
	{"kind": "savings", "savings_product": XMAS["name"], "amount": 1000},
])
check("split payment charged once for the total", tx["status"] == "Success" and tx["amount"] == 21000 + inst, tx)
d = dash("m1")
L = get("finance.get_loan", T["m1"], name=loan["name"])
check("dues up to date after the split", d["standing"]["state"] == "up_to_date", d["standing"]["state"])
check("instalment paid from the split", abs(L["amount_repaid"] - inst) < 0.01, L["amount_repaid"])
check("voluntary part saved", product_row(d, "Christmas savings")["balance"] == 3000)
rec = get("finance.get_receipt", T["m1"], doctype=tx["document_type"], name=tx["document"])
check("the receipt shows every part of the payment", len(rec["parts"]) == 3 and
	round(sum(p["amount"] for p in rec["parts"]), 2) == round(21000 + inst, 2), rec["parts"])
check("nothing more due", get("finance.payment_plan", T["m1"], cooperative=CO)["due"] == [])

# --- Transfers to a member's account number are shared out ----------------------------------------
call("payments.request_dedicated_account", T["m2"], cooperative=CO)
acct = "98" + str(int(RUN, 16) % 10**8).zfill(8)
webhook("dedicatedaccount.assign.success", {"customer": {"email": people["m2"]["user"]["email"], "customer_code": "CUS_m2"},
	"dedicated_account": {"account_number": acct, "account_name": "PAYSTACK/Mary", "bank": {"name": "Test Bank"}},
	"identification": {"status": "success"}})
call("coop.update_cooperative", T["ada"], cooperative=CO, extra_savings_product=XMAS["name"])
tdata = fake("/_test/transfer", reference=f"SAV-{RUN}", amount=5000000, email=people["m2"]["user"]["email"],
	account_number=acct)["data"]
webhook("charge.success", tdata)
webhook("charge.success", tdata)
d = dash("m2")
arrived = 50000 - (50000 * 0.015 + 100)
check("transfer pays the dues owed first", product_row(d, "Monthly dues")["balance"] == 30000 and
	d["standing"]["state"] == "up_to_date", (product_row(d, "Monthly dues")["balance"], d["standing"]["state"]))
check("the rest goes where the group chose (Christmas savings), once", abs(product_row(d, "Christmas savings")["balance"] -
	(arrived - 30000)) < 0.01, product_row(d, "Christmas savings")["balance"])

# Switched off: everything goes to regular contributions, as before
call("coop.update_cooperative", T["ada"], cooperative=CO, auto_allocate=0)
tdata = fake("/_test/transfer", reference=f"SAV2-{RUN}", amount=500000, email=people["m2"]["user"]["email"],
	account_number=acct)["data"]
webhook("charge.success", tdata)
d2 = dash("m2")
check("with sharing off, a transfer is all regular contributions", abs(product_row(d2, "Monthly dues")["balance"] -
	(30000 + 5000 - 175)) < 0.01, product_row(d2, "Monthly dues")["balance"])
call("coop.update_cooperative", T["ada"], cooperative=CO, auto_allocate=1)
rejected("the extra product must belong to the group", "coop.update_cooperative", T["ada"], cooperative=CO,
	extra_savings_product="nope")

# --- Offline payment into a product ------------------------------------------------------------------
rec = call("finance.record_offline_payment", T["tr"], cooperative=CO, member=M2, amount=4000, payment_date=str(date.today()),
	reference=f"OFF-{RUN}", savings_product=RET["name"])
ap = call("approvals.for_reference", T["ada"], reference_doctype=rec["doctype"], reference_name=rec["name"])
check("offline approval names the product", "Retirement" in ap["summary"], ap["summary"])
call("approvals.decide", T["ada"], name=ap["name"], approve=1)
d = dash("m2")
check("offline payment counted in Retirement", product_row(d, "Retirement")["balance"] == 4000)

# --- Leaving takes everything, locked products included ----------------------------------------------
rejected("can't leave while holding savings", "coop.leave_cooperative", T["m2"], cooperative=CO, contains="withdrawal")
el = get("finance.withdrawal_eligibility", T["m2"], cooperative=CO)
everything = el["leaving_amount"]
check("leaving amount is every product", abs(everything - d["balances"]["savings"]) < 0.01, (everything, d["balances"]["savings"]))
rejected("Musa can't take everything out while he has a loan", "finance.request_withdrawal", T["m1"], cooperative=CO,
	amount=100, leaving=1, contains="Repay")
w = call("finance.request_withdrawal", T["m2"], cooperative=CO, amount=everything, leaving=1, reason="Moving abroad")
call("finance.review_withdrawal", T["tr"], name=w["name"], action="approve", transfer_reference="TRF-LEAVE")
call("finance.confirm_withdrawal_received", T["m2"], name=w["name"])
d = dash("m2")
check("everything came out, product by product", abs(d["balances"]["savings"]) < 0.01 and
	all(abs(p["balance"]) < 0.01 for p in d["balances"]["products"]), d["balances"]["products"])
call("coop.leave_cooperative", T["m2"], cooperative=CO)
check("then she can leave", get("coop.list_members", T["ada"], cooperative=CO, status="Left")[0]["name"] == M2)

# --- Unmatched transfer matched to a member is shared out too ----------------------------------------
sub = console(f"print('SUB=' + frappe.db.get_value('Cooperative', '{CO}', 'paystack_subaccount_code'))").split("SUB=")[1].split()[0]
fake("/_test/transfer", reference=f"UMS-{RUN}", amount=200000, email="who@example.com", account_number="7000000001",
	subaccount=sub)
webhook("charge.success", {"reference": f"UMS-{RUN}"})
before = product_row(dash("m1"), "Christmas savings")["balance"]
r = call("finance.allocate_unmatched", T["tr"], reference=f"UMS-{RUN}", member=M1)
after = product_row(dash("m1"), "Christmas savings")["balance"]
check("a matched transfer follows the same sharing (nothing due, so extra savings)", after - before > 0 and
	r["document_type"] == "Coop Contribution", (before, after))

# --- Meetings --------------------------------------------------------------------------------------
me_se = get("coop.get_dashboard", T["se"], cooperative=CO)["membership"]
check("the secretary arranges meetings and announcements", "meetings" in me_se["capabilities"] and
	"announce" in me_se["capabilities"] and "pay_out" not in me_se["capabilities"], me_se["capabilities"])
LAGOS = ZoneInfo("Africa/Lagos")  # the site's time zone; the app sends times as members see them
soon = (datetime.now(LAGOS) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
rejected("members can't call meetings", "meetings.save_meeting", T["m1"], cooperative=CO, title="Party", starts_at=soon)
rejected("meetings are in the future", "meetings.save_meeting", T["se"], cooperative=CO, title="Old one",
	starts_at="2020-01-01T10:00")
mt = call("meetings.save_meeting", T["se"], cooperative=CO, title="Annual general meeting 2026", kind="Annual general meeting",
	starts_at=soon, venue="Community hall", agenda="1. Accounts\n2. Share-out\n3. Elections")
MT = mt["name"]
n1 = console(f"print('N=%s' % frappe.db.count('Coop Notification', {{'user': '{people['m1']['user']['name']}', 'kind': 'meeting'}}))")
check("members are told about the meeting", int(n1.split("N=")[1].split()[0]) >= 1)
call("meetings.reply", T["m1"], name=MT, response="Coming")
call("meetings.reply", T["tr"], name=MT, response="Apology")
m = get("meetings.get_meeting", T["m1"], name=MT)
check("replies are counted", m["counts"]["coming"] == 1 and m["counts"]["apologies"] == 1 and m["my_response"] == "Coming",
	m["counts"])
check("members don't see draft minutes", "minutes" not in m)
console("from coopvault.api.meetings import remind_meetings\nremind_meetings()")
rem = console(f"""
for u in ('{people['m1']['user']['name']}', '{people['tr']['user']['name']}'):
	print('R=%s' % frappe.db.count('Coop Notification', {{'user': u, 'title': ['like', 'Reminder:%']}}))
""")
counts = [int(x.split()[0]) for x in rem.split("R=")[1:]]
check("reminder goes out the day before, not to those who sent apologies", counts == [1, 0], counts)
console("from coopvault.api.meetings import remind_meetings\nremind_meetings()")
rem = console(f"print('R=%s' % frappe.db.count('Coop Notification', {{'user': '{people['m1']['user']['name']}', 'title': ['like', 'Reminder:%']}}))")
check("only one reminder", int(rem.split("R=")[1].split()[0]) == 1)
rejected("members can't take attendance", "meetings.record_attendance", T["m1"], name=MT, present=[M1])
rejected("minutes come after attendance", "meetings.save_minutes", T["se"], name=MT, minutes="Notes")
present = [M1, ids[T["se"]], get("coop.get_dashboard", T["ada"], cooperative=CO)["membership"]["name"]]
m = call("meetings.record_attendance", T["se"], name=MT, present=present)
check("attendance taken", m["status"] == "Held" and m["counts"]["present"] == 3, m["counts"])
rejected("more votes than people present", "meetings.save_minutes", T["se"], name=MT, minutes="x" * 30,
	resolutions=[{"resolution": "Accept the accounts", "votes_for": 5}], contains="More votes")
m = call("meetings.save_minutes", T["se"], name=MT, minutes="Draft so far", resolutions=[])
check("draft minutes saved, not published", not m["minutes_published"])
rejected("short minutes can't be published", "meetings.save_minutes", T["se"], name=MT, minutes="Too short", publish=1)
m = call("meetings.save_minutes", T["se"], name=MT, publish=1,
	minutes="The chairman opened the meeting at 10am. The treasurer presented the accounts for the year.",
	resolutions=[{"resolution": "Accept the 2026 accounts", "outcome": "Passed", "votes_for": 2, "votes_against": 0,
		"abstained": 1}, {"resolution": "Raise dues to 12,000", "outcome": "Deferred"}])
check("minutes published", m["minutes_published"] and len(m["resolutions"]) == 2)
rejected("published minutes can't be changed", "meetings.save_minutes", T["se"], name=MT, minutes="Changed " * 5,
	contains="published")
rejected("nor the attendance", "meetings.record_attendance", T["se"], name=MT, present=[M1])
m = get("meetings.get_meeting", T["m1"], name=MT)
check("members read the minutes and resolutions", "treasurer presented" in m["minutes"] and
	m["resolutions"][0]["votes_for"] == 2 and m["i_was_present"], m.get("resolutions"))
check("and see who was there", sorted(a["member"] for a in m["attendance"]) == sorted(present))
tomorrow = (datetime.now(LAGOS) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
nxt = call("meetings.save_meeting", T["ada"], cooperative=CO, title="Monthly meeting", starts_at=tomorrow)
check("the next meeting shows on Home", dash("m1")["next_meeting"]["name"] == nxt["name"])
lst = get("meetings.list_meetings", T["m1"], cooperative=CO)
check("meetings list: upcoming and past", [x["name"] for x in lst["upcoming"]] == [nxt["name"]] and
	lst["past"][0]["name"] == MT and not lst["can_manage"])
call("meetings.cancel_meeting", T["se"], name=nxt["name"], note="Clashes with a holiday")
check("cancelled", get("meetings.get_meeting", T["m1"], name=nxt["name"])["status"] == "Cancelled")

# --- Members added by hand --------------------------------------------------------------------------
phone_k, phone_b, phone_x = f"0805{N:06d}1", f"0805{N:06d}2", f"0805{N:06d}3"
rejected("members can't add members", "coop.add_member", T["m1"], cooperative=CO, full_name="Kemi New", phone=phone_k)
k = call("coop.add_member", T["lo"], cooperative=CO, full_name="Kemi New", phone=phone_k)
check("added by the loan officer: waits for an admission approver", k["status"] == "Pending" and k["new_account"] and
	k["setup_code"], k)
ap = call("approvals.for_reference", T["ada"], reference_doctype="Coop Member", reference_name=k["member"])
check("the approval says who added them", "added by Lanre Loans" in ap["summary"], ap["summary"])
call("approvals.decide", T["ada"], name=ap["name"], approve=1)
b = call("coop.add_member", T["ada"], cooperative=CO, full_name="Bola Added", phone=phone_b)
check("added by an admin: in straight away, with a member number", b["status"] == "Active", b)
rejected("the same phone can't be added twice", "coop.add_member", T["ada"], cooperative=CO, full_name="Bola Again",
	phone=phone_b, contains="already")
em = console(f"print('E=' + frappe.db.get_value('User', {{'mobile_no': '+234{phone_b[1:]}'}}, 'email'))").split("E=")[1].split()[0]
check("no real email is made up for them", em.endswith("@members.invalid"), em)
rejected("the setup code is needed", "auth.reset_password_with_code", login=phone_b, code="AAAA-BBBB", new_password=PW + "!b")
res = call("auth.reset_password_with_code", login=phone_b, code=b["setup_code"], new_password=PW + "!b")
B = res["token"] if isinstance(res, dict) and res.get("token") else call("auth.login", email=phone_b, password=PW + "!b")["token"]
bd = get("coop.get_dashboard", B, cooperative=CO)
check("setting up an account has no security hold", not bd.get("security_hold_until"), bd.get("security_hold_until"))
check("and they're a member", bd["membership"]["status"] == "Active" and bd["membership"]["full_name"] == "Bola Added")
rejected("no new setup code once they've set up", "coop.new_setup_code", T["ada"], cooperative=CO, member=b["member"],
	contains="already set up")
k2 = call("coop.new_setup_code", T["se"], cooperative=CO, member=k["member"])
rejected("the old setup code stops working", "auth.reset_password_with_code", login=phone_k, code=k["setup_code"],
	new_password=PW + "!k")
call("auth.reset_password_with_code", login=phone_k, code=k2["setup_code"], new_password=PW + "!k")
check("the new one works", call("auth.login", email=phone_k, password=PW + "!k").get("token"))
x = signup("ex", 9, "Existing Person")
xphone = x["user"].get("mobile_no") or f"0803{N:06d}9"
xa = call("coop.add_member", T["ada"], cooperative=CO, full_name="Existing Person", phone=xphone)
check("someone with an account is added without a setup code", not xa["new_account"] and "setup_code" not in xa and
	xa["status"] == "Active", xa)

done("savings, meetings and members")
