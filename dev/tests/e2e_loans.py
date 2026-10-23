"""Loan products, schedules, guarantors, penalties, arrears and restructuring."""
from datetime import date, timedelta

from harness import (
	PW, call, check, console, done, get, guard, new_cooperative, pay, rejected, signup,
)

guard()
people = {k: signup(k, i, n) for i, (k, n) in enumerate([("ada", "Ada Admin"), ("lo", "Lara Loans"), ("ch", "Chuka Chair"),
	("tr", "Tobi Treasurer"), ("m1", "Moji Borrower"), ("g1", "Gbenga Guarantor"), ("m2", "Musa Second"),
	("g2", "Grace Backup")], 1)}
T = {k: v["token"] for k, v in people.items()}
coop, ids = new_cooperative(T["ada"], "Loans", [(T["lo"], "Loan Officer"), (T["ch"], "Chairman"), (T["tr"], "Treasurer"),
	(T["m1"], "Member"), (T["g1"], "Member"), (T["m2"], "Member"), (T["g2"], "Member")])
CO = coop["name"]
for k in ("m1", "m2"):
	call("auth.set_payout_account", T[k], bank_code="001", account_number="00123457" + ("01" if k == "m1" else "02"), password=PW)
	console(f"frappe.db.set_value('Coop Member', '{ids[T[k]]}', 'payout_changed_on', '2026-01-01 00:00:00')")
pay(T["m1"], CO, 20000)
pay(T["g1"], CO, 10000)
pay(T["m2"], CO, 30000)
pay(T["g2"], CO, 8000)

# --- Products --------------------------------------------------------------------------------
products = get("finance.loan_products", T["m1"], cooperative=CO)
check("every group starts with a standard loan", len(products) == 1 and products[0]["product_name"] == "Standard loan")
rejected("members can't create loan products", "finance.save_loan_product", T["m1"], cooperative=CO, product_name="X")
rejected("reducing balance needs a monthly or yearly rate", "finance.save_loan_product", T["ada"], cooperative=CO,
	product_name="Bad", interest_method="Reducing balance", interest_rate=2, rate_period="Whole loan")
all_products = call("finance.save_loan_product", T["ada"], cooperative=CO, product_name="Emergency loan",
	interest_method="Reducing balance", interest_rate=2, rate_period="Per month", min_tenure_months=3, max_tenure_months=12,
	processing_fee_percent=1, penalty_percent=5, grace_days=3, guarantors_required=1, guarantee_percent=50,
	max_active_loans=1, require_up_to_date=0)
EM = next(p for p in all_products if p["product_name"] == "Emergency loan")
check("the rate reads in plain words", EM["rate_text"] == "2% a month on the reducing balance", EM["rate_text"])
all_products = call("finance.save_loan_product", T["ada"], cooperative=CO, product_name="Quick loan", interest_method="Flat",
	interest_rate=2, rate_period="Per month", max_tenure_months=6, max_active_loans=2)
QK = next(p for p in all_products if p["product_name"] == "Quick loan")

# --- Schedules --------------------------------------------------------------------------------
q = get("finance.loan_quote", T["m1"], cooperative=CO, product=EM["name"], amount=12000, tenure_months=6)
emi = 12000 * 0.02 / (1 - 1.02 ** -6)
check("reducing balance: equal instalments (EMI)", abs(q["first_instalment"] - round(emi, 2)) < 0.02, q["first_instalment"])
check("reducing balance: first month's interest is 2% of the whole amount", q["rows"][0]["interest"] == 240)
check("reducing balance: principal adds up exactly", round(sum(r["principal"] for r in q["rows"]), 2) == 12000)
check("processing fee comes off what's sent", q["processing_fee"] == 120 and q["you_receive"] == 11880)
q2 = get("finance.loan_quote", T["m1"], cooperative=CO, product=QK["name"], amount=10000, tenure_months=3)
check("flat 2% a month over 3 months is 6% interest", q2["total_interest"] == 600 and q2["rows"][0]["interest"] == 200)
check("quote includes the eligibility checklist", q["eligibility"]["ok"] is False or q["eligibility"]["checks"])

# --- Guarantors ------------------------------------------------------------------------------
rejected("the emergency loan needs a guarantor", "finance.apply_loan", T["m1"], cooperative=CO, amount=12000,
	tenure_months=6, product=EM["name"], contains="guarantor")
rejected("a guarantor can't promise more than they have free", "finance.apply_loan", T["m1"], cooperative=CO,
	amount=12000, tenure_months=6, product=EM["name"], guarantors=[{"member": ids[T["g1"]], "amount": 15000}], contains="at most")
rejected("guarantors must cover half the loan", "finance.apply_loan", T["m1"], cooperative=CO, amount=12000, tenure_months=6,
	product=EM["name"], guarantors=[{"member": ids[T["g1"]], "amount": 2000}], contains="cover")
loan = call("finance.apply_loan", T["m1"], cooperative=CO, amount=12000, tenure_months=6, product=EM["name"],
	purpose="Shop repairs", guarantors=[{"member": ids[T["g1"]], "amount": 6000}])
ap = call("approvals.for_reference", T["ada"], reference_doctype="Coop Loan", reference_name=loan["name"])
lo_view = get("approvals.pending", T["lo"], cooperative=CO)
mine = next(a for a in lo_view if a["reference_name"] == loan["name"])
check("approval waits for the guarantor", not mine["can_decide"] and "guarantor" in (mine["why_not"] or "").lower(), mine["why_not"])
check("but it can still be declined", mine["can_reject"])
g_view = get("finance.my_guarantees", T["g1"], cooperative=CO)
check("the guarantor sees the request", any(g["loan"] == loan["name"] and g["status"] == "Pending" for g in g_view))
before = get("finance.withdrawal_eligibility", T["g1"], cooperative=CO)["withdrawable"]
call("finance.respond_guarantee", T["g1"], loan=loan["name"], accept=1)
after = get("finance.withdrawal_eligibility", T["g1"], cooperative=CO)["withdrawable"]
check("accepting holds that much of the guarantor's savings", round(before - after, 2) == 6000, (before, after))
call("approvals.decide", T["lo"], name=ap["name"], approve=1)
L = call("finance.send_loan", T["tr"], name=loan["name"], transfer_reference="TRF-EM-1")
check("treasurer is told to send the amount less the fee", L["amount_to_send"] == 11880)
cash_before = get("accounts.summary", T["ada"], cooperative=CO)
L = call("finance.confirm_loan_received", T["m1"], name=loan["name"])
check("schedule runs from the day it arrived", len(L["schedule"]["rows"]) == 6 and
	L["schedule"]["rows"][0]["due_date"] > date.today().isoformat())
books = get("accounts.summary", T["ada"], cooperative=CO)
check("fee is income in the books", books["income"] >= 120, books["income"])

# --- Repayments are split interest first ---------------------------------------------------
pay(T["m1"], CO, round(emi, 2), purpose="Repayment" if False else "Loan Repayment", loan=loan["name"])
L = get("finance.get_loan", T["m1"], name=loan["name"])
rep = L["repayments"][0]
check("first repayment: 240 interest, the rest principal", rep["interest_part"] == 240 and
	abs(rep["principal_part"] - (round(emi, 2) - 240)) < 0.01, (rep["interest_part"], rep["principal_part"]))
check("first instalment shows as paid", L["schedule"]["rows"][0]["status"] == "Paid")

# --- Late penalties and arrears -----------------------------------------------------------
late = (date.today() - timedelta(days=40)).isoformat()
console(f"""
d = frappe.get_doc('Coop Loan', '{loan['name']}')
d.instalments[1].due_date = '{late}'
d.flags.ignore_validate = True
d.save(ignore_permissions=True)
from coopvault import loans
print('ADDED', loans.accrue_penalties('{loan['name']}'))
print('AGAIN', loans.accrue_penalties('{loan['name']}'))
""")
L = get("finance.get_loan", T["m1"], name=loan["name"])
row2 = L["schedule"]["rows"][1]
unpaid = row2["principal"] + row2["interest"]
check("a late instalment is charged the penalty (5% a month, after 3 days' grace)", row2["penalty"] > 0 and
	abs(row2["penalty"] / unpaid * 100 - 10) < 0.2, (row2["penalty"], unpaid))
check("running it again the same day adds nothing", abs(L["penalties_charged"] - row2["penalty"]) < 0.01)
check("arrears are tracked", L["days_overdue"] == 40 and L["overdue_amount"] > unpaid, (L["days_overdue"], L["overdue_amount"]))
book = get("finance.loan_book", T["lo"], cooperative=CO)
check("the loan book shows it 31–60 days late", any(b["label"].startswith("31") and b["count"] == 1 for b in book["buckets"]) and
	book["par_percent"] > 0, book["buckets"])
rejected("members can't see the loan book", "finance.loan_book", T["m2"], method="GET", cooperative=CO)
pay(T["m1"], CO, round(L["overdue_amount"], 2), purpose="Loan Repayment", loan=loan["name"])
L = get("finance.get_loan", T["m1"], name=loan["name"])
rep = L["repayments"][0]
check("paying the arrears clears the penalty first", abs(rep["penalty_part"] - row2["penalty"]) < 0.01, rep)
check("no longer late", L["days_overdue"] == 0 and L["overdue_amount"] == 0, (L["days_overdue"], L["overdue_amount"]))
pen = get("accounts.income_statement", T["ada"], cooperative=CO)
check("penalties paid show as income", any(i["account_name"] == "Late payment penalties" and i["amount"] > 0 for i in pen["income"]))

# --- Paying off releases the guarantee -------------------------------------------------------
pay(T["m1"], CO, round(L["balance"], 2), purpose="Loan Repayment", loan=loan["name"])
L = get("finance.get_loan", T["m1"], name=loan["name"])
check("loan repaid", L["status"] == "Repaid", L["status"])
after = get("finance.withdrawal_eligibility", T["g1"], cooperative=CO)["withdrawable"]
check("the guarantor's savings are free again", after == before, (before, after))

# --- A guarantor declines; the borrower asks someone else -------------------------------------
l2 = call("finance.apply_loan", T["m2"], cooperative=CO, amount=10000, tenure_months=4, product=EM["name"],
	guarantors=[{"member": ids[T["g1"]], "amount": 5000}])
call("finance.respond_guarantee", T["g1"], loan=l2["name"], accept=0, note="Not this month")
L2 = get("finance.get_loan", T["m2"], name=l2["name"])
check("a decline is shown to the borrower", "declined" in (L2["guarantor_blocker"] or ""), L2["guarantor_blocker"])
call("finance.replace_guarantor", T["m2"], loan=l2["name"], member=ids[T["g1"]], new_member=ids[T["g2"]], amount=5000)
call("finance.respond_guarantee", T["g2"], loan=l2["name"], accept=1)
ap2 = call("approvals.for_reference", T["ada"], reference_doctype="Coop Loan", reference_name=l2["name"])
call("approvals.decide", T["lo"], name=ap2["name"], approve=1)
call("finance.send_loan", T["tr"], name=l2["name"], transfer_reference="TRF-EM-2")
call("finance.confirm_loan_received", T["m2"], name=l2["name"])

# --- Restructuring --------------------------------------------------------------------------------
before = get("finance.get_loan", T["m2"], name=l2["name"])
rejected("members can't restructure loans", "finance.propose_restructure", T["g2"], loan=l2["name"], months=8, reason="x")
call("finance.propose_restructure", T["lo"], loan=l2["name"], months=8, reason="Lost a customer; needs smaller instalments")
ap3 = call("approvals.for_reference", T["ada"], reference_doctype="Coop Loan", reference_name=l2["name"])
rejected("loan officer can't approve the restructure", "approvals.decide", T["lo"], name=ap3["name"], approve=1)
call("approvals.decide", T["ch"], name=ap3["name"], approve=1)
after = get("finance.get_loan", T["m2"], name=l2["name"])
check("restructured over 8 new months", len(after["schedule"]["rows"]) == 8 and after["restructures"], len(after["schedule"]["rows"]))
check("no extra interest: the balance is unchanged", abs(after["balance"] - before["balance"]) < 0.01, (before["balance"], after["balance"]))
check("smaller instalments", after["schedule"]["next_amount"] < before["schedule"]["next_amount"])

done("loans")
