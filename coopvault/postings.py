"""What each money event posts to the books (see accounting.py for the engine).

Called from the document controllers when their status changes, and by the payment code for
Paystack's fees and refunds. Each posting is idempotent: an event already in the books isn't
posted twice, and undoing an event reverses its entry instead of deleting anything.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from coopvault.accounting import active_entry, loan_position, make_entry, reverse_for

TX = "Coop Payment Transaction"


def _tx(reference):
	if not reference or not frappe.db.exists(TX, reference):
		return None
	return frappe.db.get_value(
		TX, reference, ["name", "status", "settles_to", "allocated_to", "amount", "settled_amount", "cooperative",
			"member", "currency"], as_dict=True,
	)


def receipt_account(doc):
	"""Where the money for a contribution or repayment came into: the account to debit."""
	if doc.payment_method == "Opening Balance":
		# What members had saved before joining is taken to be in the group's bank already
		return "Bank"
	tx = doc.flags.get("payment_tx") or _tx(doc.reference)
	if tx:
		if tx.status == "Unmatched" or tx.allocated_to:
			return "Unallocated Receipts"  # received earlier without a member; now being allocated
		return "Paystack Clearing" if tx.settles_to == "Platform Paystack Balance" else "Bank"
	return "Cash" if doc.payment_method == "Cash" else "Bank"


def _when(value):
	return getdate(value) if value else getdate(today())


# Contributions --------------------------------------------------------------------------


def contribution(doc):
	ref = ("Coop Contribution", doc.name)
	if doc.status == "Confirmed" and not active_entry(*ref, "Contribution"):
		kind = _("Opening balance") if doc.payment_method == "Opening Balance" else _("Savings")
		make_entry(
			doc.cooperative,
			"Contribution",
			[
				{"type": receipt_account(doc), "debit": doc.amount, "member": doc.member},
				{"type": "Member Savings", "credit": doc.amount, "member": doc.member},
			],
			posting_date=_when(doc.contribution_date),
			reference=ref,
			member=doc.member,
			remarks=_("{0} from {1}").format(kind, doc.member_name),
		)
	elif doc.status in ("Reversed", "Rejected") and active_entry(*ref, "Contribution"):
		reverse_for(*ref, doc.review_note or _("Contribution reversed"))


# Loans ----------------------------------------------------------------------------------


def loan(doc):
	ref = ("Coop Loan", doc.name)
	if doc.status in ("Sent", "Active", "Repaid") and not active_entry(*ref, "Loan Disbursement"):
		# The borrower owes the full amount; a processing fee is kept back from what's sent
		fee = flt(doc.get("processing_fee"))
		make_entry(
			doc.cooperative,
			"Loan Disbursement",
			[
				{"type": "Loan Receivable", "debit": doc.principal, "member": doc.member, "loan": doc.name},
				{"type": "Bank", "credit": flt(flt(doc.principal) - fee, 2), "member": doc.member, "loan": doc.name},
				{"type": "Fee Income", "credit": fee, "member": doc.member, "loan": doc.name, "remarks": _("Processing fee")},
			],
			posting_date=_when(doc.disbursed_on),
			reference=ref,
			member=doc.member,
			remarks=_("Loan to {0} (ref {1})").format(doc.member_name, doc.transfer_reference or "-"),
		)


def split_repayment(loan_name, amount):
	"""How much of a repayment is principal and how much is interest.

	Flat-rate loans carry the same mix of principal and interest in every naira repaid; the
	repayment that clears the loan takes whatever principal is left, so the loan account ends at
	exactly zero.
	"""
	ln = frappe.db.get_value("Coop Loan", loan_name, ["principal", "interest_amount", "total_payable"], as_dict=True)
	pos = loan_position(loan_name)
	amount = flt(amount, 2)
	interest_left = max(0.0, flt(ln.interest_amount) - pos.interest_received)
	owed = pos.principal_outstanding + interest_left
	if amount + 0.005 >= owed:
		principal = max(0.0, pos.principal_outstanding)
	else:
		ratio = flt(ln.principal) / flt(ln.total_payable) if flt(ln.total_payable) else 1.0
		principal = min(max(0.0, pos.principal_outstanding), flt(amount * ratio, 2))
	principal = min(principal, amount)
	return frappe._dict(principal=flt(principal, 2), interest=flt(amount - principal, 2))


def repayment(doc):
	ref = ("Coop Loan Repayment", doc.name)
	if doc.status == "Confirmed" and not active_entry(*ref, "Loan Repayment"):
		if flt(doc.get("principal_part")) or flt(doc.get("interest_part")) or flt(doc.get("penalty_part")):
			part = frappe._dict(principal=flt(doc.principal_part), interest=flt(doc.interest_part),
				penalty=flt(doc.penalty_part))
		else:
			part = split_repayment(doc.loan, doc.amount)
			part.penalty = 0.0
		make_entry(
			doc.cooperative,
			"Loan Repayment",
			[
				{"type": receipt_account(doc), "debit": doc.amount, "member": doc.member, "loan": doc.loan},
				{"type": "Loan Receivable", "credit": part.principal, "member": doc.member, "loan": doc.loan},
				{"type": "Interest Income", "credit": part.interest, "member": doc.member, "loan": doc.loan},
				{"type": "Penalty Income", "credit": part.penalty, "member": doc.member, "loan": doc.loan},
			],
			posting_date=_when(doc.payment_date),
			reference=ref,
			member=doc.member,
			remarks=_("Repayment from {0} on {1}").format(doc.member_name, doc.loan),
		)
	elif doc.status in ("Reversed", "Rejected") and active_entry(*ref, "Loan Repayment"):
		reverse_for(*ref, doc.review_note or _("Repayment reversed"))


# Withdrawals ----------------------------------------------------------------------------


def withdrawal(doc):
	ref = ("Coop Withdrawal", doc.name)
	if doc.status in ("Sent", "Completed") and not active_entry(*ref, "Withdrawal"):
		make_entry(
			doc.cooperative,
			"Withdrawal",
			[
				{"type": "Member Savings", "debit": doc.amount, "member": doc.member},
				{"type": "Bank", "credit": doc.amount, "member": doc.member},
			],
			posting_date=_when(doc.sent_on),
			reference=ref,
			member=doc.member,
			remarks=_("Withdrawal by {0} (ref {1})").format(doc.member_name, doc.transfer_reference or "-"),
		)


# Paystack -------------------------------------------------------------------------------


def _cash_type(tx):
	return "Paystack Clearing" if tx.settles_to == "Platform Paystack Balance" else "Bank"


def payment_charges(tx):
	"""What Paystack (and any platform commission) kept from a successful payment: the amount
	credited to the member minus what actually reached the cooperative."""
	ref = (TX, tx.name)
	if not tx.cooperative or not flt(tx.settled_amount) or active_entry(*ref, "Payment Charges"):
		return
	charges = flt(flt(tx.amount) - flt(tx.settled_amount), 2)
	if abs(charges) < 0.01:
		return
	make_entry(
		tx.cooperative,
		"Payment Charges",
		[
			{"type": "Payment Charges", "debit": charges, "member": tx.member},
			{"type": _cash_type(tx), "credit": charges},
		],
		posting_date=_when(tx.paid_at),
		reference=ref,
		member=tx.member,
		remarks=_("Paystack fee on {0}").format(tx.name),
	)


def unallocated_receipt(tx):
	"""A transfer nobody could be matched to: the money is in, but owed to whoever sent it."""
	ref = (TX, tx.name)
	if not tx.cooperative or active_entry(*ref, "Unallocated Receipt"):
		return
	make_entry(
		tx.cooperative,
		"Unallocated Receipt",
		[
			{"type": _cash_type(tx), "debit": tx.amount},
			{"type": "Unallocated Receipts", "credit": tx.amount},
		],
		reference=ref,
		remarks=_("Transfer {0} not yet matched to a member").format(tx.name),
	)
	payment_charges(tx)


def refund(tx, refunded, full):
	"""Paystack gave money back to the payer (refund or lost chargeback).

	Full refund: the member's contribution/repayment has been reversed already; the bank also
	lost whatever was refunded beyond that (usually the fee the member paid at checkout).
	Partial refund: the payer got part of it back, so it comes off their savings."""
	if not tx.cooperative:
		return
	if full:
		extra = flt(flt(refunded) - flt(tx.amount), 2)
		if abs(extra) < 0.01:
			return
		lines = [{"type": "Payment Charges", "debit": extra, "member": tx.member}, {"type": _cash_type(tx), "credit": extra}]
		remarks = _("Refund of {0}: amount returned beyond the payment itself").format(tx.name)
	else:
		lines = [
			{"type": "Member Savings", "debit": refunded, "member": tx.member},
			{"type": _cash_type(tx), "credit": refunded, "member": tx.member},
		]
		remarks = _("Part refund of {0}").format(tx.name)
	make_entry(tx.cooperative, "Refund", lines, reference=(TX, tx.name), member=tx.member, remarks=remarks)
