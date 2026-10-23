"""Check the books against the records they came from. Run with:

	bench --site <site> execute coopvault.dev_check_books.run   (no: this file isn't in the package)
	echo 'exec(open("dev/tests/check_books.py").read())' | bench --site <site> console
	(not `console < check_books.py`: IPython runs that line by line and a reported problem crashes)

Prints problems and a one-line verdict; read-only.
"""
import frappe
from frappe.utils import flt

problems = []


def problem(msg):
	problems.append(msg)
	print("PROBLEM", msg)


# 1. Every entry balances, and so does each cooperative's ledger
for je in frappe.db.sql("""select parent, sum(debit) d, sum(credit) c from `tabCoop Journal Line` group by parent
		having abs(sum(debit) - sum(credit)) > 0.005""", as_dict=True):
	problem(f"{je.parent} doesn't balance: {je.d} vs {je.c}")
for row in frappe.db.sql("""select cooperative, sum(debit) d, sum(credit) c from `tabCoop Journal Line` where is_posted = 1
		group by cooperative having abs(sum(debit) - sum(credit)) > 0.005""", as_dict=True):
	problem(f"{row.cooperative} ledger out of balance: {row.d} vs {row.c}")

# 2. Each member's savings in the books = confirmed contributions - withdrawals paid + share-outs - part refunds
from coopvault.accounting import cash_balance, loan_position, member_savings, type_balance

for m in frappe.get_all("Coop Member", fields=["name", "cooperative", "full_name"]):
	contributed = flt(frappe.db.sql("select coalesce(sum(amount),0) from `tabCoop Contribution` where member=%s and status='Confirmed'", m.name)[0][0])
	withdrawn = flt(frappe.db.sql("select coalesce(sum(amount),0) from `tabCoop Withdrawal` where member=%s and status in ('Sent','Completed')", m.name)[0][0])
	shared = flt(frappe.db.sql("""select coalesce(sum(l.share_amount),0) from `tabCoop Share Out Line` l join `tabCoop Share Out` s
		on s.name=l.parent where l.member=%s and s.status='Posted'""", m.name)[0][0])
	part_refunds = flt(frappe.db.sql("""select coalesce(sum(l.debit),0) from `tabCoop Journal Line` l join `tabCoop Journal Entry` j
		on j.name=l.parent where l.member=%s and j.voucher_type='Refund' and l.account_type='Member Savings' and l.is_posted=1""",
		m.name)[0][0])
	expected = flt(contributed - withdrawn + shared - part_refunds, 2)
	actual = member_savings(m.name)
	if abs(expected - actual) > 0.005:
		problem(f"{m.full_name} ({m.name}) savings: books {actual}, records {expected}")

# 3. Loans: the books owe exactly the principal not yet repaid; repaid loans are at zero
for ln in frappe.get_all("Coop Loan", filters={"status": ["in", ["Sent", "Active", "Repaid"]]},
		fields=["name", "status", "principal", "interest_amount", "total_payable", "amount_repaid"]):
	pos = loan_position(ln.name)
	if ln.status == "Repaid" and (abs(pos.principal_outstanding) > 0.005 or abs(pos.interest_received - flt(ln.interest_amount)) > 0.01):
		problem(f"{ln.name} repaid but books show principal {pos.principal_outstanding}, interest {pos.interest_received}")
	penalties = flt(frappe.db.sql("""select coalesce(sum(credit - debit), 0) from `tabCoop Journal Line`
		where loan=%s and account_type='Penalty Income' and is_posted=1""", ln.name)[0][0])
	collected = flt(flt(ln.principal) - pos.principal_outstanding + pos.interest_received + penalties, 2)
	if abs(collected - flt(ln.amount_repaid)) > 0.01:
		problem(f"{ln.name}: repayments {ln.amount_repaid} but books collected {collected}")

# 4. Cash = everything in - everything out - fees kept by Paystack
for coop in frappe.get_all("Cooperative", pluck="name"):
	ins = flt(frappe.db.sql("select coalesce(sum(amount),0) from `tabCoop Contribution` where cooperative=%s and status='Confirmed'", coop)[0][0])
	ins += flt(frappe.db.sql("select coalesce(sum(amount),0) from `tabCoop Loan Repayment` where cooperative=%s and status='Confirmed'", coop)[0][0])
	outs = flt(frappe.db.sql("select coalesce(sum(principal - processing_fee),0) from `tabCoop Loan` where cooperative=%s and status in ('Sent','Active','Repaid')", coop)[0][0])
	outs += flt(frappe.db.sql("select coalesce(sum(amount),0) from `tabCoop Withdrawal` where cooperative=%s and status in ('Sent','Completed')", coop)[0][0])
	# Paystack's fees as booked (a share-out closes the expense account to zero, so read the entries
	# that charged them rather than the account's balance)
	charges = flt(frappe.db.sql("""select coalesce(sum(l.debit - l.credit),0) from `tabCoop Journal Line` l
		join `tabCoop Journal Entry` j on j.name=l.parent where l.cooperative=%s and l.account_type='Payment Charges'
		and j.voucher_type not in ('Share-out', 'Year End Closing') and l.is_posted=1""", coop)[0][0])
	unallocated = -type_balance(coop, ["Unallocated Receipts"])
	refunds = flt(frappe.db.sql("""select coalesce(sum(l.debit),0) from `tabCoop Journal Line` l join `tabCoop Journal Entry` j
		on j.name=l.parent where l.cooperative=%s and j.voucher_type='Refund' and l.account_type='Member Savings' and l.is_posted=1""", coop)[0][0])
	manual = flt(frappe.db.sql("""select coalesce(sum(l.debit - l.credit),0) from `tabCoop Journal Line` l join `tabCoop Journal Entry` j
		on j.name=l.parent where l.cooperative=%s and l.account_type in ('Bank','Cash','Paystack Clearing') and l.is_posted=1
		and (j.voucher_type = 'Manual Journal' or (j.voucher_type = 'Reversal' and j.reference_doctype is null))""",
		coop)[0][0])
	expected = flt(ins - outs - charges + unallocated - refunds + manual, 2)
	actual = cash_balance(coop)
	# Charges booked in the books include refund extras; both sides use the same figures
	if abs(expected - actual) > 0.01:
		problem(f"{coop} cash: books {actual}, records say {expected}")

print(f"BOOKS {'OK' if not problems else str(len(problems)) + ' PROBLEMS'}")
