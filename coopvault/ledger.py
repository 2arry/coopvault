"""Balance calculations for cooperatives, members and loans.

Everything here is recomputed from the source documents (confirmed contributions,
confirmed repayments and loans) instead of being incremented, so the stored totals
can always be rebuilt and never drift.
"""

import frappe
from frappe.utils import flt

OPEN_LOAN_STATES = ("Pending", "Active")


def _sum(doctype, field, filters):
	value = frappe.get_all(doctype, filters=filters, fields=[f"sum(`{field}`) as total"])
	return flt(value[0].total) if value else 0.0


def remaining_principal(loan):
	"""Portion of a loan's principal that has not yet been paid back."""
	if loan.status == "Pending":
		return flt(loan.principal)
	if loan.status != "Active" or not flt(loan.total_payable):
		return 0.0
	return flt(loan.principal) * flt(loan.balance) / flt(loan.total_payable)


def member_balances(member, exclude_loan=None):
	"""Return the savings / borrowing position of a Coop Member.

	borrow_limit = contributed x cooperative multiplier - principal still owed (or requested).
	With the default multiplier of 1 a member can never owe more than they have contributed.
	"""
	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	multiplier = flt(frappe.db.get_value("Cooperative", cooperative, "loan_multiplier"))

	contributed = _sum("Coop Contribution", "amount", {"member": member, "status": "Confirmed"})
	repaid = _sum("Coop Loan Repayment", "amount", {"member": member, "status": "Confirmed"})

	loan_filters = {"member": member, "status": ["in", OPEN_LOAN_STATES]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	loans = frappe.get_all(
		"Coop Loan", filters=loan_filters, fields=["status", "principal", "total_payable", "balance"]
	)

	outstanding = sum(flt(l.balance) for l in loans if l.status == "Active")
	pending = sum(flt(l.principal) for l in loans if l.status == "Pending")
	exposure = sum(remaining_principal(l) for l in loans)

	return frappe._dict(
		total_contributed=contributed,
		total_repaid=repaid,
		loan_outstanding=outstanding,
		pending_loans=pending,
		exposure=exposure,
		multiplier=multiplier,
		borrow_limit=max(0.0, contributed * multiplier - exposure),
	)


def cooperative_totals(cooperative, exclude_loan=None):
	contributions = _sum("Coop Contribution", "amount", {"cooperative": cooperative, "status": "Confirmed"})
	repayments = _sum("Coop Loan Repayment", "amount", {"cooperative": cooperative, "status": "Confirmed"})

	loan_filters = {"cooperative": cooperative, "status": ["in", ("Active", "Repaid")]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	disbursed = _sum("Coop Loan", "principal", loan_filters)
	outstanding = _sum("Coop Loan", "balance", {"cooperative": cooperative, "status": "Active"})
	members = frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Active"})

	return frappe._dict(
		member_count=members,
		total_contributions=contributions,
		total_repayments=repayments,
		total_disbursed=disbursed,
		loans_outstanding=outstanding,
		pool_balance=contributions + repayments - disbursed,
	)


def refresh_member(member):
	if not member or not frappe.db.exists("Coop Member", member):
		return
	b = member_balances(member)
	frappe.db.set_value(
		"Coop Member",
		member,
		{
			"total_contributed": b.total_contributed,
			"total_repaid": b.total_repaid,
			"loan_outstanding": b.loan_outstanding,
			"borrow_limit": b.borrow_limit,
		},
		update_modified=False,
	)


def refresh_cooperative(cooperative):
	if not cooperative or not frappe.db.exists("Cooperative", cooperative):
		return
	frappe.db.set_value("Cooperative", cooperative, cooperative_totals(cooperative), update_modified=False)


def refresh_loan(loan_name):
	"""Recompute repaid amount and balance of a loan from its confirmed repayments."""
	loan = frappe.db.get_value(
		"Coop Loan", loan_name, ["name", "status", "total_payable", "member", "cooperative"], as_dict=True
	)
	if not loan:
		return
	repaid = _sum("Coop Loan Repayment", "amount", {"loan": loan_name, "status": "Confirmed"})
	values = {"amount_repaid": repaid, "balance": max(0.0, flt(loan.total_payable) - repaid)}
	if loan.status == "Active" and values["balance"] <= 0:
		values["status"] = "Repaid"
	elif loan.status == "Repaid" and values["balance"] > 0:
		values["status"] = "Active"
	frappe.db.set_value("Coop Loan", loan_name, values, update_modified=False)


def refresh_all(member=None, cooperative=None, loan=None):
	if loan:
		refresh_loan(loan)
	refresh_member(member)
	refresh_cooperative(cooperative)


def on_member_change(doc, method=None):
	refresh_all(member=doc.name, cooperative=doc.cooperative)


def refresh_all_cooperatives():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		for member in frappe.get_all("Coop Member", filters={"cooperative": coop}, pluck="name"):
			refresh_member(member)
		refresh_cooperative(coop)
