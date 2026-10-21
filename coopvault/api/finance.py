"""Money in a cooperative: contributions, loans, repayments, withdrawals and opening balances.

Payments into the pool only come through Paystack (see api/payments.py), so there are no
uploaded receipts to check. Money going out (loans and withdrawals) is sent by an admin or the
treasurer from the cooperative's bank, recorded with the transfer reference, and confirmed by
the member who received it. Nobody can send money to themselves.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, getdate, now_datetime, today

from coopvault.api import ensure_not_self, get_membership
from coopvault.ledger import cooperative_totals, loan_schedule, member_balances
from coopvault.notify import notify, staff_of
from coopvault.utils import check_hold, mask_account, money_text


def _money(amount, cooperative):
	return money_text(amount, frappe.db.get_value("Cooperative", cooperative, "currency"))


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


# Contributions ------------------------------------------------------------------

CONTRIBUTION_FIELDS = [
	"name", "member", "member_name", "amount", "contribution_date", "status", "payment_method",
	"reference", "notes", "review_note", "creation",
]


@frappe.whitelist()
def list_contributions(cooperative, scope="mine", status=None, limit=50, start=0):
	me = get_membership(cooperative)
	return _list("Coop Contribution", cooperative, me, scope, status, CONTRIBUTION_FIELDS, limit, start)


@frappe.whitelist(methods=["POST"])
def propose_opening_balance(cooperative, member, amount, as_of=None, note=None):
	"""Bring over what a member had already saved before joining CoopVault.

	An admin or the treasurer proposes it (never for themselves); it only counts once the member
	agrees in the app.
	"""
	me = get_membership(cooperative, admin=True)
	m = frappe.db.get_value("Coop Member", member, ["name", "cooperative", "user", "status"], as_dict=True)
	if not m or m.cooperative != cooperative or m.status != "Active":
		frappe.throw(_("Member not found"))
	ensure_not_self(me, m.name, _("Another admin or the treasurer has to propose your own opening balance"))
	if frappe.db.exists("Coop Contribution", {"member": member, "payment_method": "Opening Balance", "status": ["in", ["Pending", "Confirmed"]]}):
		frappe.throw(_("This member already has an opening balance"))
	if as_of and getdate(as_of) > getdate(today()):
		frappe.throw(_("The date can't be in the future"))
	doc = frappe.get_doc(
		{
			"doctype": "Coop Contribution",
			"cooperative": cooperative,
			"member": member,
			"amount": flt(amount),
			"contribution_date": as_of or today(),
			"payment_method": "Opening Balance",
			"status": "Pending",
			"proposed_by": frappe.session.user,
			"notes": note,
		}
	).insert(ignore_permissions=True)
	notify(
		m.user,
		_("Confirm your opening balance of {0}").format(_money(amount, cooperative)),
		_("This is what you'd already saved before CoopVault. Check it and accept or decline."),
		link="/home",
		cooperative=cooperative,
		kind="opening-balance",
	)
	return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def respond_opening_balance(name, accept):
	doc = frappe.get_doc("Coop Contribution", name)
	me = get_membership(doc.cooperative)
	if doc.member != me.name or doc.payment_method != "Opening Balance" or doc.status != "Pending":
		frappe.throw(_("Nothing to confirm here"), frappe.PermissionError)
	doc.status = "Confirmed" if cint(accept) else "Rejected"
	doc.reviewed_by = frappe.session.user
	doc.save(ignore_permissions=True)
	verb = _("accepted") if doc.status == "Confirmed" else _("declined")
	notify(
		[doc.proposed_by],
		_("{0} {1} their opening balance of {2}").format(doc.member_name, verb, _money(doc.amount, doc.cooperative)),
		link=f"/admin/members/{doc.member}",
		cooperative=doc.cooperative,
		kind="opening-balance",
	)
	return doc.as_dict()


# Loans --------------------------------------------------------------------------

LOAN_FIELDS = [
	"name", "member", "member_name", "principal", "interest_rate", "interest_amount", "total_payable",
	"amount_repaid", "balance", "status", "purpose", "tenure_months", "application_date",
	"disbursed_on", "received_on", "due_date", "review_note", "transfer_reference", "payout_bank_name",
	"payout_account_number", "payout_account_name", "receipt_disputed", "dispute_note", "creation",
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
	payout = frappe.db.get_value("Coop Member", me.name, ["payout_account_number", "security_hold_until"], as_dict=True)
	return {
		**b,
		**coop,
		"pool_balance": pool,
		"max_now": max(0.0, min(b.borrow_limit, pool)),
		"has_payout_account": bool(payout.payout_account_number),
		"security_hold_until": payout.security_hold_until,
	}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def apply_loan(cooperative, amount, tenure_months=1, purpose=None):
	me = get_membership(cooperative)
	if not me.name:
		frappe.throw(_("Only members can borrow"))
	check_hold(me.name)
	if not frappe.db.get_value("Coop Member", me.name, "payout_account_number"):
		frappe.throw(_("Add the bank account your loan should go to first (Me → Where to send my money)"))
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
	notify(
		staff_of(cooperative, exclude_users=[me.user]),
		_("{0} asked to borrow {1}").format(me.full_name, _money(doc.principal, cooperative)),
		purpose,
		link="/admin?tab=reviews",
		cooperative=cooperative,
		kind="loan-request",
	)
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
	data["is_mine"] = loan.member == me.name
	data["repayments"] = frappe.get_all(
		"Coop Loan Repayment", filters={"loan": name}, fields=REPAYMENT_FIELDS, order_by="creation desc"
	)
	data["confirm_after_days"] = cint(frappe.db.get_single_value("CoopVault Settings", "confirm_after_days")) or 7
	if me.is_admin:
		data["member_balances"] = member_balances(loan.member, exclude_loan=name if loan.status == "Pending" else None)
		data["payout_account"] = frappe.db.get_value(
			"Coop Member", loan.member,
			["payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"], as_dict=True,
		)
	return data


@frappe.whitelist(methods=["POST"])
def review_loan(name, action, transfer_reference=None, note=None):
	"""Send (after transferring the money) or reject a loan request. Never your own."""
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, admin=True)
	ensure_not_self(me, loan.member, _("Another admin or the treasurer has to send your own loan"))
	if loan.status != "Pending":
		frappe.throw(_("Only pending loan requests can be reviewed"))
	if action == "approve":
		loan.status = "Sent"
		loan.transfer_reference = (transfer_reference or "").strip()
	elif action == "reject":
		loan.status = "Rejected"
	else:
		frappe.throw(_("Invalid action"))
	loan.review_note = note
	loan.save(ignore_permissions=True)

	amount = _money(loan.principal, loan.cooperative)
	if loan.status == "Sent":
		notify(
			loan.user,
			_("{0} is on its way to {1} {2}").format(amount, loan.payout_bank_name, mask_account(loan.payout_account_number)),
			_("Confirm in the app when it arrives. Your repayments start from that day."),
			link=f"/loans/{loan.name}",
			cooperative=loan.cooperative,
			kind="loan-sent",
		)
	else:
		notify(loan.user, _("Your loan request for {0} was declined").format(amount), note, link=f"/loans/{loan.name}",
			cooperative=loan.cooperative, kind="loan-rejected")
	return get_loan(name)


def _confirm_received(doc, label, link):
	doc.receipt_disputed = 0
	doc.save(ignore_permissions=True)
	notify(
		staff_of(doc.cooperative, exclude_users=[doc.user]),
		_("{0} confirmed they received {1}").format(doc.member_name, label),
		link=link,
		cooperative=doc.cooperative,
		kind="transfer-confirmed",
		push=False,
	)


@frappe.whitelist(methods=["POST"])
def confirm_loan_received(name):
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, allow_inactive=True)
	if loan.member != me.name or loan.status != "Sent":
		frappe.throw(_("Nothing to confirm here"), frappe.PermissionError)
	loan.status = "Active"
	loan.received_on = today()
	_confirm_received(loan, _("their loan of {0}").format(_money(loan.principal, loan.cooperative)), f"/loans/{loan.name}")
	return get_loan(name)


def _report_not_received(doc, note, label, link):
	doc.receipt_disputed = 1
	doc.dispute_note = note
	doc.save(ignore_permissions=True)
	notify(
		staff_of(doc.cooperative, exclude_users=[doc.user]),
		_("{0} says {1} hasn't arrived").format(doc.member_name, label),
		_("Reference {0}. {1}").format(doc.transfer_reference, note or ""),
		link=link,
		cooperative=doc.cooperative,
		kind="transfer-disputed",
	)


@frappe.whitelist(methods=["POST"])
def report_loan_not_received(name, note=None):
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, allow_inactive=True)
	if loan.member != me.name or loan.status != "Sent":
		frappe.throw(_("Nothing to report here"), frappe.PermissionError)
	_report_not_received(loan, note, _("their loan of {0}").format(_money(loan.principal, loan.cooperative)),
		"/admin?tab=reviews")
	return get_loan(name)


@frappe.whitelist(methods=["POST"])
def update_loan_transfer(name, transfer_reference, note=None):
	"""After a borrower reports a missing transfer: record the corrected or new transfer."""
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, admin=True)
	ensure_not_self(me, loan.member)
	if loan.status != "Sent":
		frappe.throw(_("This loan isn't waiting for a transfer"))
	if not (transfer_reference or "").strip():
		frappe.throw(_("Enter the transfer reference"))
	loan.transfer_reference = transfer_reference.strip()
	loan.receipt_disputed = 0
	loan.review_note = note or loan.review_note
	loan.save(ignore_permissions=True)
	notify(loan.user, _("Your loan transfer was updated"), _("New reference {0}. Confirm when it arrives.").format(
		loan.transfer_reference), link=f"/loans/{loan.name}", cooperative=loan.cooperative, kind="loan-sent")
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


# Repayments (only through Paystack, see api/payments.py) ---------------------------

REPAYMENT_FIELDS = [
	"name", "loan", "member", "member_name", "amount", "payment_date", "status", "payment_method",
	"reference", "notes", "review_note", "creation",
]


@frappe.whitelist()
def list_repayments(cooperative, scope="mine", status=None, loan=None, limit=50, start=0):
	me = get_membership(cooperative)
	extra = {"loan": loan} if loan else None
	return _list("Coop Loan Repayment", cooperative, me, scope, status, REPAYMENT_FIELDS, limit, start, extra)


# Withdrawals --------------------------------------------------------------------

WITHDRAWAL_FIELDS = [
	"name", "member", "member_name", "amount", "reason", "status", "requested_on", "sent_on", "received_on",
	"transfer_reference", "payout_bank_name", "payout_account_number", "payout_account_name", "receipt_disputed",
	"dispute_note", "review_note", "creation",
]


@frappe.whitelist()
def withdrawal_eligibility(cooperative):
	me = get_membership(cooperative)
	b = member_balances(me.name)
	payout = frappe.db.get_value(
		"Coop Member", me.name, ["payout_bank_name", "payout_account_number", "payout_account_name", "security_hold_until"],
		as_dict=True,
	)
	return {
		**b,
		"pool_balance": cooperative_totals(cooperative).pool_balance,
		"currency": frappe.db.get_value("Cooperative", cooperative, "currency"),
		"payout_account": payout,
		"pending": frappe.db.exists("Coop Withdrawal", {"member": me.name, "status": ["in", ["Pending", "Sent"]]}),
	}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def request_withdrawal(cooperative, amount, reason=None):
	me = get_membership(cooperative)
	check_hold(me.name)
	if not frappe.db.get_value("Coop Member", me.name, "payout_account_number"):
		frappe.throw(_("Add the bank account to receive the money first (Me → Where to send my money)"))
	if frappe.db.exists("Coop Withdrawal", {"member": me.name, "status": ["in", ["Pending", "Sent"]]}):
		frappe.throw(_("You already have a withdrawal in progress"))
	doc = frappe.get_doc(
		{
			"doctype": "Coop Withdrawal",
			"cooperative": cooperative,
			"member": me.name,
			"amount": flt(amount),
			"reason": reason,
			"status": "Pending",
			"requested_on": today(),
		}
	).insert(ignore_permissions=True)
	notify(
		staff_of(cooperative, exclude_users=[me.user]),
		_("{0} asked to withdraw {1}").format(me.full_name, _money(doc.amount, cooperative)),
		reason,
		link="/admin?tab=reviews",
		cooperative=cooperative,
		kind="withdrawal-request",
	)
	return doc.as_dict()


@frappe.whitelist()
def list_withdrawals(cooperative, scope="mine", status=None, limit=50, start=0):
	me = get_membership(cooperative)
	return _list("Coop Withdrawal", cooperative, me, scope, status, WITHDRAWAL_FIELDS, limit, start)


@frappe.whitelist()
def get_withdrawal(name):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	data = {f: doc.get(f) for f in WITHDRAWAL_FIELDS}
	data["cooperative"] = doc.cooperative
	data["currency"] = frappe.db.get_value("Cooperative", doc.cooperative, "currency")
	data["is_mine"] = doc.member == me.name
	data["confirm_after_days"] = cint(frappe.db.get_single_value("CoopVault Settings", "confirm_after_days")) or 7
	if me.is_admin:
		data["payout_account"] = frappe.db.get_value(
			"Coop Member", doc.member,
			["payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"], as_dict=True,
		)
		data["member_balances"] = member_balances(doc.member, exclude_withdrawal=name)
	return data


@frappe.whitelist(methods=["POST"])
def cancel_withdrawal(name):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name or doc.status != "Pending":
		frappe.throw(_("Only your own pending requests can be cancelled"), frappe.PermissionError)
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def review_withdrawal(name, action, transfer_reference=None, note=None):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, admin=True)
	ensure_not_self(me, doc.member, _("Another admin or the treasurer has to send your own withdrawal"))
	if doc.status != "Pending":
		frappe.throw(_("Only pending withdrawals can be reviewed"))
	if action == "approve":
		doc.status = "Sent"
		doc.transfer_reference = (transfer_reference or "").strip()
	elif action == "reject":
		doc.status = "Rejected"
		doc.reviewed_by = frappe.session.user
	else:
		frappe.throw(_("Invalid action"))
	doc.review_note = note
	doc.save(ignore_permissions=True)
	amount = _money(doc.amount, doc.cooperative)
	if doc.status == "Sent":
		notify(doc.user, _("{0} is on its way to {1} {2}").format(amount, doc.payout_bank_name,
			mask_account(doc.payout_account_number)), _("Confirm in the app when it arrives."),
			link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-sent")
	else:
		notify(doc.user, _("Your withdrawal of {0} was declined").format(amount), note,
			link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-rejected")
	return get_withdrawal(name)


@frappe.whitelist(methods=["POST"])
def confirm_withdrawal_received(name):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name or doc.status != "Sent":
		frappe.throw(_("Nothing to confirm here"), frappe.PermissionError)
	doc.status = "Completed"
	doc.received_on = today()
	_confirm_received(doc, _("their withdrawal of {0}").format(_money(doc.amount, doc.cooperative)),
		f"/withdrawals/{doc.name}")
	return get_withdrawal(name)


@frappe.whitelist(methods=["POST"])
def report_withdrawal_not_received(name, note=None):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name or doc.status != "Sent":
		frappe.throw(_("Nothing to report here"), frappe.PermissionError)
	_report_not_received(doc, note, _("their withdrawal of {0}").format(_money(doc.amount, doc.cooperative)),
		"/admin?tab=reviews")
	return get_withdrawal(name)


@frappe.whitelist(methods=["POST"])
def update_withdrawal_transfer(name, transfer_reference, note=None):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, admin=True)
	ensure_not_self(me, doc.member)
	if doc.status != "Sent":
		frappe.throw(_("This withdrawal isn't waiting for a transfer"))
	if not (transfer_reference or "").strip():
		frappe.throw(_("Enter the transfer reference"))
	doc.transfer_reference = transfer_reference.strip()
	doc.receipt_disputed = 0
	doc.review_note = note or doc.review_note
	doc.save(ignore_permissions=True)
	notify(doc.user, _("Your withdrawal transfer was updated"), _("New reference {0}. Confirm when it arrives.").format(
		doc.transfer_reference), link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-sent")
	return get_withdrawal(name)


def auto_confirm_transfers():
	"""Daily: treat loans and withdrawals as received when the member neither confirmed nor
	disputed them within the configured number of days."""
	days = cint(frappe.db.get_single_value("CoopVault Settings", "confirm_after_days")) or 7
	cutoff = frappe.utils.add_days(today(), -days)
	for doctype, status in (("Coop Loan", "Active"), ("Coop Withdrawal", "Completed")):
		for name in frappe.get_all(
			doctype, filters={"status": "Sent", "receipt_disputed": 0, "disbursed_on" if doctype == "Coop Loan" else "sent_on": ["<=", cutoff]},
			pluck="name",
		):
			doc = frappe.get_doc(doctype, name)
			doc.status = status
			doc.received_on = frappe.utils.add_days(doc.get("disbursed_on") or doc.get("sent_on"), days)
			doc.flags.ignore_permissions = True
			doc.save()
			notify(doc.user, _("We've marked your transfer as received"),
				_("It was sent {0} days ago and wasn't disputed.").format(days), cooperative=doc.cooperative,
				kind="transfer-confirmed", push=False)
			frappe.db.commit()


# Receipts and admin overview --------------------------------------------------------

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
	automatic = doc.status == "Confirmed" and doc.payment_method == "Paystack"
	reviewer = doc.reviewed_by and not automatic and frappe.db.get_value("User", doc.reviewed_by, "full_name")
	return {
		"doctype": doctype,
		"name": doc.name,
		"kind": _("Opening balance") if doc.payment_method == "Opening Balance" else label,
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
		"review_note": doc.review_note,
		"confirmed_automatically": automatic,
		"confirmed_by": reviewer,
		"confirmed_on": doc.reviewed_on,
	}


@frappe.whitelist()
def pending_reviews(cooperative):
	"""Everything waiting on an admin or the treasurer, in one call."""
	me = get_membership(cooperative, admin=True)
	loans = _list("Coop Loan", cooperative, me, "all", "Pending", LOAN_FIELDS, 100, 0)
	withdrawals = _list("Coop Withdrawal", cooperative, me, "all", "Pending", WITHDRAWAL_FIELDS, 100, 0)
	payout_fields = ["payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"]
	for row in loans + withdrawals:
		row.payout_account = frappe.db.get_value("Coop Member", row.member, payout_fields, as_dict=True)
		row.is_mine = row.member == me.name
	for row in loans:
		row.member_balances = member_balances(row.member, exclude_loan=row.name)
	for row in withdrawals:
		row.member_balances = member_balances(row.member, exclude_withdrawal=row.name)
	disputed = frappe.get_all("Coop Loan", filters={"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1},
		fields=LOAN_FIELDS) + frappe.get_all("Coop Withdrawal",
		filters={"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1}, fields=WITHDRAWAL_FIELDS)
	for row in disputed:
		row.kind = "loan" if row.get("principal") is not None else "withdrawal"
		row.is_mine = row.member == me.name
	return {
		"members": frappe.get_all(
			"Coop Member",
			filters={"cooperative": cooperative, "status": "Pending"},
			fields=["name", "full_name", "user", "phone", "creation"],
			order_by="creation asc",
		),
		"loans": loans,
		"withdrawals": withdrawals,
		"disputes": disputed,
		"awaiting_members": frappe.get_all(
			"Coop Contribution",
			filters={"cooperative": cooperative, "status": "Pending", "payment_method": "Opening Balance"},
			fields=["name", "member", "member_name", "amount", "contribution_date"],
		),
		"pool": cooperative_totals(cooperative),
		"now": now_datetime(),
	}
