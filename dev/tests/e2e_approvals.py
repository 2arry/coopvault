"""Roles, approval rules and separation of duties; offline payments recorded by one officer and approved by another."""
from datetime import date

from harness import (
	PW, RUN, call, check, console, done, get, guard, new_cooperative, pay, rejected, signup,
)

guard()
people = {k: signup(k, i, n) for i, (k, n) in enumerate([("ada", "Ada Admin"), ("chair", "Chika Chairman"),
	("tres", "Tayo Treasurer"), ("lo", "Lola Loans"), ("acct", "Ayo Accountant"), ("m1", "Mo One"), ("m2", "Ngo Two"),
	("late", "Late Joiner")], 1)}
T = {k: v["token"] for k, v in people.items()}
coop, ids = new_cooperative(T["ada"], "Approvals", [(T["chair"], "Chairman"), (T["tres"], "Treasurer"),
	(T["lo"], "Loan Officer"), (T["acct"], "Accountant"), (T["m1"], "Member"), (T["m2"], "Member")])
CO = coop["name"]
me = get("auth.me", T["m1"])["cooperatives"]
check("members get a member number", any(m["cooperative"] == CO and m.get("member_number") for m in me))
check("officers' capabilities come with their membership",
	"pay_out" in next(m for m in get("auth.me", T["tres"])["cooperatives"] if m["cooperative"] == CO)["capabilities"])

# --- Admission ------------------------------------------------------------------------
call("coop.join_cooperative", T["late"], join_code=coop["join_code"])
q = get("finance.pending_reviews", T["lo"], cooperative=CO)
adm = next(a for a in q["approvals"] if a["action"] == "Member admission")
check("loan officer sees the request but can't decide it", not adm["can_decide"] and "Waiting for" in adm["why_not"], adm["why_not"])
rejected("loan officer can't admit members", "approvals.decide", T["lo"], name=adm["name"], approve=1)
call("approvals.decide", T["tres"], name=adm["name"], approve=1)
late = next(m for m in get("auth.me", T["late"])["cooperatives"] if m["cooperative"] == CO)
check("treasurer admits; the member is active with a number", late["status"] == "Active" and late["member_number"], late)

# --- A small loan: one stage, then the treasurer sends it -------------------------------
for k in ("m1", "m2"):
	call("auth.set_payout_account", T[k], bank_code="001", account_number="00123456" + ("01" if k == "m1" else "02"), password=PW)
	console(f"frappe.db.set_value('Coop Member', '{ids[T[k]]}', 'payout_changed_on', '2026-01-01 00:00:00')")
pay(T["m1"], CO, 50000)
loan = call("finance.apply_loan", T["m1"], cooperative=CO, amount=20000, tenure_months=2, purpose="Stock")
ap = call("approvals.for_reference", T["m1"], reference_doctype="Coop Loan", reference_name=loan["name"])
check("small loan needs one approval", ap and len(ap["steps"]) == 1, ap and len(ap["steps"]))
rejected("rejecting needs a reason", "approvals.decide", T["lo"], name=ap["name"], approve=0, contains="why")
call("approvals.decide", T["lo"], name=ap["name"], approve=1)
L = get("finance.get_loan", T["m1"], name=loan["name"])
check("approved, waiting to be sent", L["status"] == "Approved", L["status"])
rejected("loan officer can't send money", "finance.send_loan", T["lo"], name=loan["name"], transfer_reference="X1",
	contains="treasurer")
rejected("accountant can't send money", "finance.send_loan", T["acct"], name=loan["name"], transfer_reference="X1")
L = call("finance.send_loan", T["tres"], name=loan["name"], transfer_reference="TRF-AP-1")
check("treasurer sends it", L["status"] == "Sent")
call("finance.confirm_loan_received", T["m1"], name=loan["name"])

# --- A big loan: Loan Officer reviews, Chairman signs off ------------------------------
pay(T["m2"], CO, 600000)
big = call("finance.apply_loan", T["m2"], cooperative=CO, amount=500000, tenure_months=6, purpose="Shop")
ap = call("approvals.for_reference", T["m2"], reference_doctype="Coop Loan", reference_name=big["name"])
check("₦500k+ loan needs two stages", len(ap["steps"]) == 2 and ap["steps"][1]["label"] == "Final approval",
	[s["label"] for s in ap["steps"]])
ap = call("approvals.decide", T["lo"], name=ap["name"], approve=1)
check("after review it waits for the chairman", ap["status"] == "Pending" and ap["current_stage"] == 2)
rejected("the reviewer can't also give final approval", "approvals.decide", T["lo"], name=ap["name"], approve=1)
rejected("treasurer can't give final approval", "approvals.decide", T["tres"], name=ap["name"], approve=1)
ap = call("approvals.decide", T["chair"], name=ap["name"], approve=1)
check("chairman's sign-off approves it", ap["status"] == "Approved" and
	get("finance.get_loan", T["m2"], name=big["name"])["status"] == "Approved")

# --- Rejection --------------------------------------------------------------------------
r = call("finance.apply_loan", T["m1"], cooperative=CO, amount=5000, tenure_months=1, purpose="Fees")
ap = call("approvals.for_reference", T["m1"], reference_doctype="Coop Loan", reference_name=r["name"])
call("approvals.decide", T["tres"], name=ap["name"], approve=0, note="Repay the first loan first")
check("rejected loan is closed with the reason", get("finance.get_loan", T["m1"], name=r["name"])["status"] == "Rejected")

# --- Custom rules ------------------------------------------------------------------------
rejected("chairman can't change approval rules", "approvals.save_rule", T["chair"], cooperative=CO, action="Withdrawal",
	min_amount=100000, stages=[{"label": "Treasurer", "roles": ["Treasurer"]}])
rules = call("approvals.save_rule", T["ada"], cooperative=CO, action="Withdrawal", min_amount=10000,
	stages=[{"label": "Check", "roles": ["Treasurer"]}, {"label": "Sign-off", "roles": ["Chairman"]}])
check("admin adds a rule for bigger withdrawals", any(r["action"] == "Withdrawal" and r["min_amount"] == 10000 for r in rules["rules"]))
base = next(r for r in rules["rules"] if r["action"] == "Withdrawal" and r["min_amount"] == 0)
rejected("the from-zero rule can't be deleted", "approvals.delete_rule", T["ada"], name=base["name"], contains="zero")
wd = call("finance.request_withdrawal", T["m2"], cooperative=CO, amount=20000, reason="School fees")
ap = call("approvals.for_reference", T["m2"], reference_doctype="Coop Withdrawal", reference_name=wd["name"])
check("withdrawal of ₦20k uses the new two-stage rule", [s["label"] for s in ap["steps"]] == ["Check", "Sign-off"])
call("approvals.decide", T["tres"], name=ap["name"], approve=1)
call("approvals.decide", T["chair"], name=ap["name"], approve=1)
W = call("finance.send_withdrawal", T["tres"], name=wd["name"], transfer_reference="TRF-WD-1")
check("then the treasurer sends it", W["status"] == "Sent")

# --- Offline payments: recorded by one officer, approved by another --------------------
before = get("coop.get_statement", T["ada"], cooperative=CO, member=ids[T["m1"]])["balances"]["savings"]
rejected("members can't record payments", "finance.record_offline_payment", T["m1"], cooperative=CO, member=ids[T["m1"]],
	amount=1000, payment_date=date.today().isoformat(), reference="R1")
rejected("transfers need the bank reference", "finance.record_offline_payment", T["tres"], cooperative=CO,
	member=ids[T["m1"]], amount=15000, payment_date=date.today().isoformat(), reference="", contains="reference")
rec = call("finance.record_offline_payment", T["tres"], cooperative=CO, member=ids[T["m1"]], amount=15000,
	payment_date=date.today().isoformat(), reference=f"GTB-{RUN}", payment_method="Bank Transfer", note="Paid at the bank")
check("recorded as waiting", rec["status"] == "Pending")
after = get("coop.get_statement", T["ada"], cooperative=CO, member=ids[T["m1"]])["balances"]["savings"]
check("it doesn't count until approved", after == before, (before, after))
rejected("the same reference can't be recorded twice", "finance.record_offline_payment", T["acct"], cooperative=CO,
	member=ids[T["m1"]], amount=15000, payment_date=date.today().isoformat(), reference=f"GTB-{RUN}", contains="already")
ap = call("approvals.for_reference", T["ada"], reference_doctype=rec["doctype"], reference_name=rec["name"])
rejected("whoever recorded it can't approve it", "approvals.decide", T["tres"], name=ap["name"], approve=1,
	contains="someone else")
call("approvals.decide", T["acct"], name=ap["name"], approve=1)
after = get("coop.get_statement", T["ada"], cooperative=CO, member=ids[T["m1"]])["balances"]["savings"]
check("approved: added to the member's savings", after == before + 15000, (before, after))
rep = call("finance.record_offline_payment", T["acct"], cooperative=CO, member=ids[T["m1"]], amount=10500,
	payment_date=date.today().isoformat(), reference=f"CASH-{RUN}", payment_method="Cash", loan=loan["name"])
ap = call("approvals.for_reference", T["ada"], reference_doctype=rep["doctype"], reference_name=rep["name"])
call("approvals.decide", T["tres"], name=ap["name"], approve=1)
L = get("finance.get_loan", T["m1"], name=loan["name"])
check("offline repayment approved: loan goes down", abs(L["amount_repaid"] - 10500) < 0.01, L["amount_repaid"])

# --- Who can do what ----------------------------------------------------------------------
rejected("treasurer can't suspend members", "coop.update_member", T["tres"], member=ids[T["m2"]], status="Suspended")
call("coop.update_member", T["chair"], member=ids[T["m2"]], status="Suspended")
check("chairman can suspend", True)
call("coop.update_member", T["chair"], member=ids[T["m2"]], status="Active")
rejected("loan officer can't see the books", "accounts.summary", T["lo"], method="GET", cooperative=CO)
check("chairman can see the books", get("accounts.summary", T["chair"], cooperative=CO)["cash_total"] > 0)
rejected("chairman can't prepare journals", "accounts.create_journal", T["chair"], cooperative=CO,
	posting_date=date.today().isoformat(), remarks="x", lines=[])
rejected("treasurer can't change roles", "coop.update_member", T["tres"], member=ids[T["m1"]], role="Admin", contains="roles")

# --- Nobody else to approve: flagged, and appointing an officer unblocks it ---------------
solo_admin, other = signup("solo", 9, "Solo Admin"), signup("help", 0, "Help Later")
solo, sids = new_cooperative(solo_admin["token"], "Solo", [(other["token"], "Member")])
pay(solo_admin["token"], solo["name"], 5000)
call("auth.set_payout_account", solo_admin["token"], bank_code="001", account_number="0012345609", password=PW)
w = call("finance.request_withdrawal", solo_admin["token"], cooperative=solo["name"], amount=1000)
ap = call("approvals.for_reference", solo_admin["token"], reference_doctype="Coop Withdrawal", reference_name=w["name"])
check("a request nobody else can approve is flagged", ap["stuck"], ap)
call("coop.update_member", solo_admin["token"], member=sids[other["token"]], role="Treasurer")
ap = call("approvals.decide", other["token"], name=ap["name"], approve=1)
check("a newly appointed treasurer can approve it", ap["status"] == "Approved")

done("approvals")
