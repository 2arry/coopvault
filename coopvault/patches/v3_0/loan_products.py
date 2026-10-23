"""Loan products and stored schedules for existing loans.

Each cooperative gets a "Standard loan" product from its loan settings. Existing loans keep the
terms they were agreed on (flat interest for the whole loan at the rate that applied then); their
instalments are stored, and each repayment's split is taken from the books, so nothing already
posted changes. Idempotent.
"""

import frappe
from frappe.utils import flt

from coopvault import approvals, loans


def execute():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		approvals.ensure_default_rules(coop)
		loans.ensure_default_product(coop)

	for name in frappe.get_all("Coop Loan", pluck="name"):
		doc = frappe.get_doc("Coop Loan", name)
		if doc.instalments:
			continue
		doc.product = doc.product or loans.ensure_default_product(doc.cooperative)
		doc.interest_method, doc.rate_period = "Flat", "Whole loan"
		started = doc.status in ("Active", "Repaid")
		start = (doc.received_on or doc.disbursed_on) if started else doc.application_date
		rows = loans.build_schedule(doc.principal, doc.tenure_months, "Flat", doc.interest_rate, "Whole loan", start or doc.creation)
		for row in rows:
			doc.append("instalments", row)
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)

	for rep in frappe.get_all("Coop Loan Repayment", fields=["name", "loan", "amount", "status"],
			filters={"principal_part": 0, "interest_part": 0, "penalty_part": 0}):
		parts = _parts_from_books(rep.name)
		if parts is None and rep.status == "Confirmed":
			parts = loans.allocate(rep.loan, rep.amount, exclude=rep.name)
		if parts:
			frappe.db.set_value("Coop Loan Repayment", rep.name, {"principal_part": parts.principal,
				"interest_part": parts.interest, "penalty_part": parts.penalty}, update_modified=False)

	for name in frappe.get_all("Coop Loan", filters={"status": ["in", ["Active", "Repaid"]]}, pluck="name"):
		loans.refresh(name)


def _parts_from_books(repayment):
	je = frappe.db.get_value("Coop Journal Entry", {"reference_doctype": "Coop Loan Repayment", "reference_name": repayment,
		"voucher_type": "Loan Repayment"}, "name", order_by="creation asc")
	if not je:
		return None
	lines = frappe.get_all("Coop Journal Line", filters={"parent": je}, fields=["account_type", "credit"])
	pick = lambda kind: flt(sum(flt(l.credit) for l in lines if l.account_type == kind), 2)
	return frappe._dict(principal=pick("Loan Receivable"), interest=pick("Interest Income"), penalty=pick("Penalty Income"))
