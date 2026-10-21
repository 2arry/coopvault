"""The books: fees, interest, manual journals, reports, refunds, unmatched transfers, share-out."""
from datetime import date, timedelta

from harness import (
	PW, RUN, call, check, console, done, err, fake, get, guard, new_cooperative, pay, rejected, signup, webhook,
)

guard()
a, t, m, n = signup("ada", 1, "Ada Admin"), signup("tayo", 2, "Tayo Treasurer"), signup("mo", 3, "Mo Member"), signup("nk", 4, "Nk Member")
A, T, M, N = a["token"], t["token"], m["token"], n["token"]
coop, ids = new_cooperative(A, "Books", [(T, "Treasurer"), (M, "Member"), (N, "Member")], fee_bearer="Cooperative",
	interest_rate=20)
CO = coop["name"]
call("coop.set_bank_account", A, cooperative=CO, bank_code="001", account_number="0099887766", password=PW)
s = get("accounts.summary", A, cooperative=CO)
check("a new cooperative gets its chart of accounts", s["cash_total"] == 0 and s["members_savings"] == 0)
chart = get("accounts.chart", A, cooperative=CO)["accounts"]
check("system accounts created", len(chart) >= 16 and any(x["account_type"] == "Member Savings" for x in chart))
rejected("members can't read the books", "accounts.summary", M, method="GET", cooperative=CO)

# --- Fees: the group pays Paystack's fee, so the pool is what really arrived --------------
pay(M, CO, 10000)
pay(N, CO, 6000)
s = get("accounts.summary", A, cooperative=CO)
fee = 10000 * 0.015 + 6000 * 0.015  # the fake keeps 1.5%
check("members are credited in full", s["members_savings"] == 16000, s["members_savings"])
check("the pool is what reached the bank (fees taken off)", abs(s["cash_total"] - (16000 - fee)) < 0.01, s["cash_total"])
check("fees show as a cost", abs(s["expenses"] - fee) < 0.01, s["expenses"])
dash = get("coop.get_dashboard", M, cooperative=CO)
check("app's pool = the books' cash", abs(dash["pool"]["pool_balance"] - s["cash_total"]) < 0.01, dash["pool"]["pool_balance"])

# --- Loan interest is income; principal clears the loan account ---------------------------
call("auth.set_payout_account", M, bank_code="001", account_number="0012345601", password=PW)
console(f"frappe.db.set_value('Coop Member', '{ids[M]}', 'payout_changed_on', '2026-01-01 00:00:00')")
loan = call("finance.apply_loan", M, cooperative=CO, amount=8000, tenure_months=2, purpose="Stock")
call("finance.review_loan", T, name=loan["name"], action="approve", transfer_reference="TRF-BOOKS-1")
call("finance.confirm_loan_received", M, name=loan["name"])
pay(M, CO, 4800, purpose="Loan Repayment", loan=loan["name"])  # half of 9,600 (20% flat)
s = get("accounts.summary", A, cooperative=CO)
check("half a flat-rate repayment is half the interest", abs(s["income"] - 800) < 0.01, s["income"])
check("loan account shows principal still owed", abs(s["loans_out"] - 4000) < 0.01, s["loans_out"])
pay(M, CO, 4800, purpose="Loan Repayment", loan=loan["name"])
s = get("accounts.summary", A, cooperative=CO)
check("repaid loan: loan account at zero, all interest earned", abs(s["loans_out"]) < 0.01 and abs(s["income"] - 1600) < 0.01,
	(s["loans_out"], s["income"]))

# --- Manual journals need a second person -------------------------------------------------
acct = {x["account_type"]: x["name"] for x in get("accounts.chart", A, cooperative=CO)["accounts"]}
j = call("accounts.create_journal", A, cooperative=CO, posting_date=date.today().isoformat(), remarks="Bank SMS charges",
	lines=[{"account": acct["Bank Charges"], "debit": 50}, {"account": acct["Bank"], "credit": 50}])
check("manual journal waits as a draft", j["status"] == "Draft")
cash_before = get("accounts.summary", A, cooperative=CO)["cash_total"]
rejected("you can't approve your own journal", "accounts.approve_journal", A, name=j["name"], contains="other than")
rejected("unbalanced journal refused", "accounts.create_journal", A, cooperative=CO, posting_date=date.today().isoformat(),
	remarks="Bad", lines=[{"account": acct["Bank Charges"], "debit": 50}, {"account": acct["Bank"], "credit": 40}], contains="equal")
j = call("accounts.approve_journal", T, name=j["name"])
check("approved by someone else, it's posted", j["status"] == "Posted" and j["approved_by_name"] == "Tayo Treasurer")
check("posting moved the cash", abs(get("accounts.summary", A, cooperative=CO)["cash_total"] - (cash_before - 50)) < 0.01)
r = call("accounts.reverse_journal", A, name=j["name"], reason="Bank refunded the charge")
check("reversal undoes it with a linked entry", r["status"] == "Reversed" and r["reversed_by"])
check("cash back after reversal", abs(get("accounts.summary", A, cooperative=CO)["cash_total"] - cash_before) < 0.01)
contrib_je = get("accounts.journals", A, cooperative=CO, voucher_type="Contribution")[0]["name"]
rejected("automatic entries can't be reversed by hand", "accounts.reverse_journal", A, name=contrib_je, reason="x",
	contains="manual")

# --- Reports agree with each other --------------------------------------------------------
tb = get("accounts.trial_balance", A, cooperative=CO)
check("trial balance balances", abs(tb["total_debit"] - tb["total_credit"]) < 0.01 and tb["total_debit"] > 0,
	(tb["total_debit"], tb["total_credit"]))
bs = get("accounts.balance_sheet", A, cooperative=CO)
check("balance sheet balances", bs["balanced"], (bs["total_assets"], bs["total_liabilities"], bs["total_equity"]))
inc = get("accounts.income_statement", A, cooperative=CO)
s = get("accounts.summary", A, cooperative=CO)
check("income statement surplus = summary", abs(inc["surplus"] - s["surplus"]) < 0.01, (inc["surplus"], s["surplus"]))
cf = get("accounts.cash_flow", A, cooperative=CO)
check("cash flow ends at the cash balance", abs(cf["closing"] - s["cash_total"]) < 0.01, (cf["closing"], s["cash_total"]))
gl = get("accounts.general_ledger", A, cooperative=CO, account=acct["Member Savings"])
check("general ledger runs to the account balance", abs(gl["closing"] - s["members_savings"]) < 0.01)

# --- Refunds --------------------------------------------------------------------------------
v = pay(N, CO, 3000)
savings_before = get("coop.get_statement", A, cooperative=CO, member=ids[N])["balances"]["savings"]
webhook("refund.processed", {"id": f"RF{RUN}", "transaction_reference": v["reference"], "amount": 300000})
webhook("refund.processed", {"id": f"RF{RUN}", "transaction_reference": v["reference"], "amount": 300000})
st = get("coop.get_statement", A, cooperative=CO, member=ids[N])
check("full refund takes the payment back off savings (once, even if resent)", st["balances"]["savings"] == savings_before - 3000,
	st["balances"]["savings"])
v2 = pay(N, CO, 2000)
webhook("refund.processed", {"id": f"RP{RUN}", "transaction_reference": v2["reference"], "amount": 50000})
st = get("coop.get_statement", A, cooperative=CO, member=ids[N])
check("part refund comes off savings", st["balances"]["savings"] == savings_before - 3000 + 2000 - 500, st["balances"]["savings"])

# --- An unmatched transfer, then matched to its sender ------------------------------------
sub = console(f"print('SUB=' + frappe.db.get_value('Cooperative', '{CO}', 'paystack_subaccount_code'))").split("SUB=")[1].split()[0]
fake("/_test/transfer", reference=f"UM-{RUN}", amount=250000, email="who@example.com", account_number="7000000000",
	subaccount=sub)
webhook("charge.success", {"reference": f"UM-{RUN}"})
s = get("accounts.summary", A, cooperative=CO)
check("unmatched money waits in Unallocated receipts", abs(s["unallocated"] - 2500) < 0.01, s["unallocated"])
pending = get("finance.pending_reviews", T, cooperative=CO)
check("staff see it to match", any(u["name"] == f"UM-{RUN}" for u in pending["unmatched"]))
rejected("nobody matches a transfer to themselves", "finance.allocate_unmatched", T, reference=f"UM-{RUN}", member=ids[T])
call("finance.allocate_unmatched", T, reference=f"UM-{RUN}", member=ids[N])
s = get("accounts.summary", A, cooperative=CO)
st = get("coop.get_statement", A, cooperative=CO, member=ids[N])
check("matched: added to the member, unallocated back to zero", abs(s["unallocated"]) < 0.01 and
	st["balances"]["savings"] == savings_before - 3000 + 2000 - 500 + 2500, (s["unallocated"], st["balances"]["savings"]))

# --- Year-end share-out -------------------------------------------------------------------
# Everything so far happened "yesterday", so yesterday's year can be shared out
console(f"""
frappe.db.sql("update `tabCoop Journal Entry` set posting_date = date_sub(posting_date, interval 1 day) where cooperative=%s", '{CO}')
frappe.db.sql("update `tabCoop Journal Line` set posting_date = date_sub(posting_date, interval 1 day) where cooperative=%s", '{CO}')
""")
yesterday = (date.today() - timedelta(days=1)).isoformat()
rejected("share-out can't include today", "accounts.preview_share_out", T, method="GET", cooperative=CO,
	period_end=date.today().isoformat(), contains="ended")
p = get("accounts.preview_share_out", T, cooperative=CO, period_end=yesterday, reserve_percent=25, member_percent=100,
	basis="Savings at year end")
check("surplus = interest less fees", abs(p["surplus"] - (p["income"] - p["expenses"])) < 0.01 and p["surplus"] > 0, p)
check("reserve is 25% of the surplus", abs(p["reserve_amount"] - round(p["surplus"] * 0.25, 2)) < 0.01)
check("members share the rest", abs(p["to_members"] + p["retained"] - (p["surplus"] - p["reserve_amount"])) < 0.01 and p["to_members"] > 0)
so = call("accounts.create_share_out", T, cooperative=CO, period_end=yesterday, reserve_percent=25, member_percent=100,
	basis="Savings at year end")
rejected("treasurer can't approve a share-out", "accounts.approve_share_out", T, name=so["name"])
mine_before = get("coop.get_statement", M, cooperative=CO)["balances"]["savings"]
so = call("accounts.approve_share_out", A, name=so["name"])
check("share-out posted", so["status"] == "Posted" and so["journal_entry"])
my_line = next(l for l in so["lines"] if l["member"] == ids[M])
st = get("coop.get_statement", M, cooperative=CO)
check("member's share added to their savings", abs(st["balances"]["savings"] - (mine_before + my_line["share_amount"])) < 0.01)
check("statement shows the share-out", any(e["type"] == "Share-out" for e in st["entries"]))
s = get("accounts.summary", A, cooperative=CO)
check("books closed up to the period end", s["books_closed_until"] == yesterday, s["books_closed_until"])
check("reserve fund got its share", abs(s["reserve"] - so["reserve_amount"]) < 0.01)
bs = get("accounts.balance_sheet", A, cooperative=CO)
check("balance sheet still balances after closing", bs["balanced"])
inc = get("accounts.income_statement", A, cooperative=CO, from_date=yesterday, to_date=yesterday)
check("the closed year's income statement still shows its income", abs(inc["total_income"] - p["income"]) < 0.01,
	inc["total_income"])
rejected("nothing can be posted in a closed period", "accounts.create_journal", A, cooperative=CO, posting_date=yesterday,
	remarks="Late", lines=[{"account": acct["Bank Charges"], "debit": 10}, {"account": acct["Bank"], "credit": 10}], contains="closed")
member_view = get("accounts.get_share_out", M, name=so["name"])
check("members see only their own line", len(member_view["lines"]) == 1)

done("books")
