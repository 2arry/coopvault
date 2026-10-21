"""Open double-entry books for every cooperative and post everything that already happened.

Idempotent: events already in the books are skipped, so it is safe to run again.
"""

import json

import frappe
from frappe.utils import flt

from coopvault import postings
from coopvault.accounting import ensure_chart
from coopvault.ledger import refresh_all_cooperatives


def execute():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		ensure_chart(coop)
	_complete_payment_details()

	# Loans before repayments, so each repayment splits into principal and interest correctly
	for name in frappe.get_all("Coop Loan", filters={"status": ["in", ["Sent", "Active", "Repaid"]]}, pluck="name",
			order_by="disbursed_on asc, creation asc"):
		postings.loan(frappe.get_doc("Coop Loan", name))
	for name in frappe.get_all("Coop Contribution", filters={"status": ["in", ["Confirmed", "Reversed"]]}, pluck="name",
			order_by="contribution_date asc, creation asc"):
		_post_then_undo(frappe.get_doc("Coop Contribution", name), postings.contribution)
	for name in frappe.get_all("Coop Loan Repayment", filters={"status": ["in", ["Confirmed", "Reversed"]]}, pluck="name",
			order_by="payment_date asc, creation asc"):
		_post_then_undo(frappe.get_doc("Coop Loan Repayment", name), postings.repayment)
	for name in frappe.get_all("Coop Withdrawal", filters={"status": ["in", ["Sent", "Completed"]]}, pluck="name"):
		postings.withdrawal(frappe.get_doc("Coop Withdrawal", name))

	for name in frappe.get_all("Coop Payment Transaction",
			filters={"status": ["in", ["Success", "Refunded", "Disputed", "Unmatched"]]}, pluck="name"):
		tx = frappe.get_doc("Coop Payment Transaction", name)
		if tx.status == "Unmatched":
			postings.unallocated_receipt(tx)
			continue
		postings.payment_charges(tx)
		if tx.status == "Refunded" and not frappe.db.exists("Coop Journal Entry",
				{"reference_name": tx.name, "voucher_type": "Refund"}):
			postings.refund(tx, flt(tx.refunded_amount) or flt(tx.charged_amount), full=True)
	refresh_all_cooperatives()


def _post_then_undo(doc, post):
	"""A reversed record was in the books once: post it, then reverse it."""
	final = doc.status
	if final == "Reversed":
		doc.status = "Confirmed"
		post(doc)
		doc.status = final
		doc.review_note = doc.review_note or "Reversed before the books were opened"
	post(doc)


def _complete_payment_details():
	"""Older payments didn't record what actually reached the cooperative or where it settled."""
	from coopvault.api.payments import settled_amount

	for tx in frappe.get_all("Coop Payment Transaction", filters={"status": ["in", ["Success", "Refunded", "Disputed",
			"Unmatched"]]}, fields=["name", "cooperative", "gateway_response", "settled_amount", "settles_to"]):
		doc = frappe.get_doc("Coop Payment Transaction", tx.name)
		data = {}
		try:
			data = json.loads(doc.gateway_response or "{}")
		except ValueError:
			pass
		values = {}
		if not doc.settled_amount and data:
			values["settled_amount"] = settled_amount(doc, data)
		if not doc.settles_to:
			has_sub = (data.get("subaccount") or {}).get("subaccount_code") or (
				doc.cooperative and frappe.db.get_value("Cooperative", doc.cooperative, "paystack_subaccount_code"))
			values["settles_to"] = "Cooperative Bank" if has_sub else "Platform Paystack Balance"
		if values:
			doc.db_set(values, update_modified=False)
