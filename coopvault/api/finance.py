import base64

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

from coopvault.api import get_membership
from coopvault.ledger import cooperative_totals, loan_schedule, member_balances

MANUAL_METHODS = ("Bank Transfer", "Cash")
MAX_PROOF_BYTES = 5 * 1024 * 1024


def _resolve_member(cooperative, me, member=None):
	"""Admins may act on behalf of another member; everyone else acts for themselves."""
	if member and member != me.name:
		if not me.is_admin:
			frappe.throw(_("You can only record payments for yourself"), frappe.PermissionError)
		if frappe.db.get_value("Coop Member", member, "cooperative") != cooperative:
			frappe.throw(_("Member does not belong to this cooperative"))
		return member
	if not me.name:
		frappe.throw(_("Choose the member this payment is for"))
	return me.name


def _attach_proof(doc, proof_data=None, proof_filename=None):
	"""Save a base64 data URL as a private file attached to the document."""
	if not proof_data:
		return
	if "," in proof_data:
		proof_data = proof_data.split(",", 1)[1]
	content = base64.b64decode(proof_data)
	if len(content) > MAX_PROOF_BYTES:
		frappe.throw(_("Proof of payment must be smaller than 5MB"))
	f = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": proof_filename or f"{doc.name}-proof",
			"attached_to_doctype": doc.doctype,
			"attached_to_name": doc.name,
			"attached_to_field": "proof",
			"is_private": 1,
			"content": content,
		}
	).insert(ignore_permissions=True)
	doc.db_set("proof", f.file_url)


def _review_fields(doc, action, note):
	if doc.status != "Pending":
		frappe.throw(_("This record has already been {0}").format(_(doc.status).lower()))
	if action not in ("confirm", "reject"):
		frappe.throw(_("Invalid action"))
	doc.status = "Confirmed" if action == "confirm" else "Rejected"
	doc.review_note = note
	doc.reviewed_by = frappe.session.user


def _list(doctype, cooperative, me, scope, status, fields, limit, start, extra_filters=None):
	filters = {"cooperative": cooperative, **(extra_filters or {})}
	if scope == "all":
		if not me.is_admin:
			frappe.throw(_("Only admins can see everyone's records"), frappe.PermissionError)
	else:
		filters["member"] = me.name
	if status:
		filters["status"] = status
	return frappe.get_all(
		doctype, filters=filters, fields=fields, order_by="creation desc", limit=cint(limit) or 50,
		start=cint(start),
	)


@frappe.whitelist()
def download_proof(doctype, name):
	if doctype not in ("Coop Contribution", "Coop Loan Repayment"):
		frappe.throw(_("Invalid document type"))
	doc = frappe.get_doc(doctype, name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not doc.proof:
		frappe.throw(_("No proof of payment attached"))
	f = frappe.get_doc("File", {"file_url": doc.proof})
	frappe.local.response.filename = f.file_name
	frappe.local.response.filecontent = f.get_content()
	frappe.local.response.type = "download"


# Contributions ------------------------------------------------------------------

CONTRIBUTION_FIELDS = [
	"name", "member", "member_name", "amount", "contribution_date", "status", "payment_method",
	"reference", "notes", "proof", "review_note", "creation",
]


@frappe.whitelist()
def list_contributions(cooperative, scope="mine", status=None, limit=50, start=0):
	me = get_membership(cooperative)
	return _list("Coop Contribution", cooperative, me, scope, status, CONTRIBUTION_FIELDS, limit, start)


@frappe.whitelist(methods=["POST"])
def record_contribution(
	cooperative,
	amount,
	payment_method="Bank Transfer",
	reference=None,
	notes=None,
	contribution_date=None,
	member=None,
	proof_data=None,
	proof_filename=None,
):
	"""Record a manual (bank transfer / cash) contribution.

	Members' records start as Pending until an admin confirms the money arrived.
	Admins recording for a member (e.g. cash collected at a meeting) are confirmed at once.
	"""
	me = get_membership(cooperative)
	member = _resolve_member(cooperative, me, member)
	if payment_method not in MANUAL_METHODS:
		frappe.throw(_("Use the Pay with Paystack option for card / online payments"))
	if contribution_date and getdate(contribution_date) > getdate(today()):
		frappe.throw(_("Contribution date cannot be in the future"))

	doc = frappe.get_doc(
		{
			"doctype": "Coop Contribution",
			"cooperative": cooperative,
			"member": member,
			"amount": flt(amount),
			"payment_method": payment_method,
			"reference": reference,
			"notes": notes,
			"contribution_date": contribution_date or today(),
			"status": "Confirmed" if me.is_admin else "Pending",
		}
	).insert(ignore_permissions=True)
	_attach_proof(doc, proof_data, proof_filename)
	return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def review_contribution(name, action, note=None):
	doc = frappe.get_doc("Coop Contribution", name)
	get_membership(doc.cooperative, admin=True)
	_review_fields(doc, action, note)
	doc.save(ignore_permissions=True)
	return doc.as_dict()


# Loans --------------------------------------------------------------------------

LOAN_FIELDS = [
	"name", "member", "member_name", "principal", "interest_rate", "interest_amount", "total_payable",
	"amount_repaid", "balance", "status", "purpose", "tenure_months", "application_date",
	"disbursed_on", "due_date", "review_note", "creation",
]


@frappe.whitelist()
def loan_eligibility(cooperative):
	me = get_membership(cooperative)
	if not me.name:
		frappe.throw(_("Only members can borrow"))
	coop = frappe.db.get_value(
		"Cooperative",
		cooperative,
		["currency", "interest_rate", "max_loan_tenure_months", "loan_multiplier"],
		as_dict=True,
	)
	b = member_balances(me.name)
	pool = cooperative_totals(cooperative).pool_balance
	return {
		**b,
		**coop,
		"pool_balance": pool,
		"max_now": max(0.0, min(b.borrow_limit, pool)),
	}


@frappe.whitelist(methods=["POST"])
def apply_loan(cooperative, amount, tenure_months=1, purpose=None):
	me = get_membership(cooperative)
	if not me.name:
		frappe.throw(_("Only members can borrow"))
	doc = frappe.get_doc(
		{
			"doctype": "Coop Loan",
			"cooperative": cooperative,
			"member": me.name,
			"principal": flt(amount),
			"tenure_months": cint(tenure_months) or 1,
			"purpose": purpose,
			"status": "Pending",
			"application_date": today(),
		}
	).insert(ignore_permissions=True)
	return doc.as_dict()


@frappe.whitelist()
def list_loans(cooperative, scope="mine", status=None, limit=50, start=0):
	me = get_membership(cooperative)
	return _list("Coop Loan", cooperative, me, scope, status, LOAN_FIELDS, limit, start)


@frappe.whitelist()
def get_loan(name):
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, allow_inactive=True)
	if loan.member != me.name and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	data = {f: loan.get(f) for f in LOAN_FIELDS}
	data["cooperative"] = loan.cooperative
	data["currency"] = frappe.db.get_value("Cooperative", loan.cooperative, "currency")
	data["schedule"] = loan_schedule(loan)
	data["repayments"] = frappe.get_all(
		"Coop Loan Repayment", filters={"loan": name}, fields=REPAYMENT_FIELDS, order_by="creation desc"
	)
	if me.is_admin:
		data["member_balances"] = member_balances(loan.member, exclude_loan=name if loan.status == "Pending" else None)
		data["payout_account"] = frappe.db.get_value(
			"Coop Member", loan.member, ["payout_bank_name", "payout_account_number", "payout_account_name"],
			as_dict=True,
		)
	return data


@frappe.whitelist(methods=["POST"])
def review_loan(name, action, note=None):
	"""Admin approves (marks disbursed / Active) or rejects a pending loan."""
	loan = frappe.get_doc("Coop Loan", name)
	get_membership(loan.cooperative, admin=True)
	if loan.status != "Pending":
		frappe.throw(_("Only pending loans can be reviewed"))
	if action == "approve":
		loan.status = "Active"
	elif action == "reject":
		loan.status = "Rejected"
	else:
		frappe.throw(_("Invalid action"))
	loan.review_note = note
	loan.save(ignore_permissions=True)
	return get_loan(name)


@frappe.whitelist(methods=["POST"])
def cancel_loan(name):
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, allow_inactive=True)
	if loan.member != me.name:
		frappe.throw(_("You can only cancel your own loan requests"), frappe.PermissionError)
	if loan.status != "Pending":
		frappe.throw(_("Only pending loan requests can be cancelled"))
	loan.status = "Cancelled"
	loan.save(ignore_permissions=True)
	return {"ok": True}


# Repayments ---------------------------------------------------------------------

REPAYMENT_FIELDS = [
	"name", "loan", "member", "member_name", "amount", "payment_date", "status", "payment_method",
	"reference", "notes", "proof", "review_note", "creation",
]


@frappe.whitelist()
def list_repayments(cooperative, scope="mine", status=None, loan=None, limit=50, start=0):
	me = get_membership(cooperative)
	extra = {"loan": loan} if loan else None
	return _list("Coop Loan Repayment", cooperative, me, scope, status, REPAYMENT_FIELDS, limit, start, extra)


@frappe.whitelist(methods=["POST"])
def record_repayment(
	loan,
	amount,
	payment_method="Bank Transfer",
	reference=None,
	notes=None,
	payment_date=None,
	proof_data=None,
	proof_filename=None,
):
	loan_doc = frappe.get_doc("Coop Loan", loan)
	me = get_membership(loan_doc.cooperative)
	if loan_doc.member != me.name and not me.is_admin:
		frappe.throw(_("You can only repay your own loans"), frappe.PermissionError)
	if payment_method not in MANUAL_METHODS:
		frappe.throw(_("Use the Pay with Paystack option for card / online payments"))

	doc = frappe.get_doc(
		{
			"doctype": "Coop Loan Repayment",
			"loan": loan,
			"amount": flt(amount),
			"payment_method": payment_method,
			"reference": reference,
			"notes": notes,
			"payment_date": payment_date or today(),
			"status": "Confirmed" if me.is_admin else "Pending",
		}
	).insert(ignore_permissions=True)
	_attach_proof(doc, proof_data, proof_filename)
	return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def review_repayment(name, action, note=None):
	doc = frappe.get_doc("Coop Loan Repayment", name)
	get_membership(doc.cooperative, admin=True)
	_review_fields(doc, action, note)
	doc.save(ignore_permissions=True)
	return doc.as_dict()


RECEIPT_DOCTYPES = {
	"Coop Contribution": ("contribution_date", _("Savings contribution")),
	"Coop Loan Repayment": ("payment_date", _("Loan repayment")),
}


@frappe.whitelist()
def get_receipt(doctype, name):
	"""Everything needed to show or share a receipt for one payment."""
	if doctype not in RECEIPT_DOCTYPES:
		frappe.throw(_("Invalid document type"))
	doc = frappe.get_doc(doctype, name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	date_field, label = RECEIPT_DOCTYPES[doctype]
	coop = frappe.db.get_value("Cooperative", doc.cooperative, ["cooperative_name", "currency"], as_dict=True)
	automatic = doc.status == "Confirmed" and doc.reviewed_by == "Administrator"
	reviewer = doc.reviewed_by and not automatic and frappe.db.get_value("User", doc.reviewed_by, "full_name")
	return {
		"doctype": doctype,
		"name": doc.name,
		"kind": label,
		"cooperative": doc.cooperative,
		"cooperative_name": coop.cooperative_name,
		"currency": coop.currency,
		"member_name": doc.member_name,
		"amount": doc.amount,
		"date": doc.get(date_field),
		"status": doc.status,
		"payment_method": doc.payment_method,
		"reference": doc.reference,
		"loan": doc.get("loan"),
		"notes": doc.notes,
		"confirmed_automatically": automatic,
		"confirmed_by": reviewer,
		"confirmed_on": doc.reviewed_on,
		"has_proof": bool(doc.proof),
	}


@frappe.whitelist()
def pending_reviews(cooperative):
	"""Everything waiting on an admin, in one call."""
	me = get_membership(cooperative, admin=True)
	return {
		"members": frappe.get_all(
			"Coop Member",
			filters={"cooperative": cooperative, "status": "Pending"},
			fields=["name", "full_name", "user", "phone", "creation"],
			order_by="creation asc",
		),
		"contributions": _list("Coop Contribution", cooperative, me, "all", "Pending", CONTRIBUTION_FIELDS, 100, 0),
		"loans": [
			{
				**l,
				"member_balances": member_balances(l.member, exclude_loan=l.name),
				"payout_account": frappe.db.get_value(
					"Coop Member", l.member, ["payout_bank_name", "payout_account_number", "payout_account_name"],
					as_dict=True,
				),
			}
			for l in _list("Coop Loan", cooperative, me, "all", "Pending", LOAN_FIELDS, 100, 0)
		],
		"repayments": _list("Coop Loan Repayment", cooperative, me, "all", "Pending", REPAYMENT_FIELDS, 100, 0),
		"pool": cooperative_totals(cooperative),
	}
