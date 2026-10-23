"""Money in a cooperative: contributions, loans, repayments, withdrawals and opening balances.

Payments into the pool only come through Paystack (see api/payments.py), so there are no
uploaded receipts to check. Money going out (loans and withdrawals) is sent by an admin or the
treasurer from the cooperative's bank, recorded with the transfer reference, and confirmed by
the member who received it. Nobody can send money to themselves.
"""

import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, getdate, now_datetime, today

from coopvault import approvals, loans
from coopvault.api import can, ensure_not_self, get_membership, require
from coopvault.ledger import cooperative_totals, loan_schedule, member_balances
from coopvault.notify import holders, notify, staff_of
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
	"""Bring over what a member had already saved before joining Cooperative360.

	An admin or the treasurer proposes it (never for themselves); it only counts once the member
	agrees in the app.
	"""
	me = get_membership(cooperative, capability="opening")
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
		_("This is what you'd already saved before Cooperative360. Check it and accept or decline."),
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
	"product", "interest_method", "rate_period", "processing_fee", "penalties_charged", "overdue_amount", "days_overdue",
	"restructure_months", "restructure_reason",
]
PRODUCT_FIELDS = ["name", "product_name", "description", "interest_method", "interest_rate", "rate_period",
	"processing_fee_percent", "penalty_percent", "grace_days", "min_amount", "max_amount", "savings_multiplier",
	"min_tenure_months", "max_tenure_months", "max_active_loans", "min_membership_months", "require_up_to_date",
	"guarantors_required", "guarantee_percent", "enabled"]


def _products(cooperative, enabled_only=True):
	loans.ensure_default_product(cooperative)
	filters = {"cooperative": cooperative}
	if enabled_only:
		filters["enabled"] = 1
	rows = frappe.get_all("Coop Loan Product", filters=filters, fields=PRODUCT_FIELDS, order_by="creation asc")
	for r in rows:
		r.rate_text = loans.describe_rate(r.interest_method, r.interest_rate, r.rate_period)
	return rows


@frappe.whitelist()
def loan_products(cooperative):
	"""The loans a member can choose from, with each one's terms in words."""
	get_membership(cooperative)
	return _products(cooperative)


@frappe.whitelist()
def loan_quote(cooperative, product, amount, tenure_months):
	"""What a loan would cost, its instalments, and whether this member can take it."""
	me = get_membership(cooperative)
	if frappe.db.get_value("Coop Loan Product", product, "cooperative") != cooperative:
		frappe.throw(_("Loan product not found"))
	quote = loans.preview(product, flt(amount), cint(tenure_months))
	quote.eligibility = loans.eligibility(me.name, product, flt(amount), cint(tenure_months))
	return quote


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
def apply_loan(cooperative, amount, tenure_months=1, purpose=None, product=None, guarantors=None):
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
			"product": product,
			"status": "Pending",
			"application_date": today(),
			"guarantors": [{"member": g.get("member"), "amount": flt(g.get("amount"))} for g in _json(guarantors)],
		}
	).insert(ignore_permissions=True)
	for g in doc.guarantors:
		notify(g.user, _("{0} asked you to guarantee {1}").format(me.full_name, _money(g.amount, cooperative)),
			_("Part of their loan of {0}. Your promise holds that much of your savings until it's repaid.").format(
				_money(doc.principal, cooperative)), link=f"/loans/{doc.name}", cooperative=cooperative, kind="guarantee")
	approvals.request(
		cooperative, "Loan", ("Coop Loan", doc.name), amount=doc.principal, beneficiary=me.name,
		summary=_("{0} asked to borrow {1} over {2} months").format(me.full_name, _money(doc.principal, cooperative),
			doc.tenure_months),
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
	mine_as_guarantor = next((g for g in loan.guarantors if g.member == me.name), None)
	if loan.member != me.name and not me.is_admin and not mine_as_guarantor:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	data = {f: loan.get(f) for f in LOAN_FIELDS}
	data["product_name"] = frappe.db.get_value("Coop Loan Product", loan.product, "product_name") if loan.product else None
	data["rate_text"] = loans.describe_rate(loan.interest_method or "Flat", loan.interest_rate, loan.rate_period or "Whole loan")
	data["amount_to_send"] = flt(flt(loan.principal) - flt(loan.processing_fee), 2)
	data["guarantors"] = [{"member": g.member, "member_name": g.member_name, "amount": g.amount, "status": g.status,
		"responded_on": g.responded_on, "note": g.note} for g in loan.guarantors]
	data["my_guarantee"] = {"amount": mine_as_guarantor.amount, "status": mine_as_guarantor.status} if mine_as_guarantor else None
	data["guarantor_blocker"] = loans.guarantor_blocker(loan.name) if loan.status == "Pending" else None
	data["restructures"] = [{"restructured_on": r.restructured_on, "outstanding": r.outstanding, "new_months": r.new_months,
		"reason": r.reason} for r in loan.restructures]
	data["can_restructure"] = loan.status == "Active" and loan.member != me.name and "loans" in (me.get("capabilities") or [])
	data["cooperative"] = loan.cooperative
	data["currency"] = frappe.db.get_value("Cooperative", loan.cooperative, "currency")
	data["schedule"] = loan_schedule(loan)
	data["is_mine"] = loan.member == me.name
	data["repayments"] = frappe.get_all(
		"Coop Loan Repayment", filters={"loan": name}, fields=REPAYMENT_FIELDS, order_by="creation desc"
	)
	data["confirm_after_days"] = cint(frappe.db.get_single_value("CoopVault Settings", "confirm_after_days")) or 7
	if me.is_admin:
		data["member_balances"] = member_balances(loan.member, exclude_loan=name if loan.status in ("Pending", "Approved") else None)
		data["payout_account"] = frappe.db.get_value(
			"Coop Member", loan.member,
			["payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"], as_dict=True,
		)
	data["approval"] = approvals.for_reference("Coop Loan", name)
	return data


@frappe.whitelist(methods=["POST"])
def review_loan(name, action, transfer_reference=None, note=None):
	"""Decide a loan request (through the group's approval rule) and/or send an approved loan.

	approve: a pending request gets this officer's approval; once fully approved, a transfer
	reference sends it straight away if this officer may send money. An approved loan is sent.
	reject: declines it, at any point before the money goes."""
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, admin=True)
	ensure_not_self(me, loan.member, _("Another officer has to deal with your own loan"))
	if action not in ("approve", "reject"):
		frappe.throw(_("Invalid action"))
	if loan.status == "Pending":
		approvals.decide_for("Coop Loan", loan.name, action == "approve", note)
		loan.reload()
		if loan.status == "Approved" and (transfer_reference or "").strip() and can(me, "pay_out"):
			_send_loan(loan, me, transfer_reference, note)
	elif loan.status == "Approved":
		require(me, "pay_out")
		if action == "approve":
			_send_loan(loan, me, transfer_reference, note)
		else:
			loan.status = "Rejected"
			loan.review_note = note
			loan.save(ignore_permissions=True)
			notify(loan.user, _("Your loan of {0} was declined").format(_money(loan.principal, loan.cooperative)), note,
				link=f"/loans/{loan.name}", cooperative=loan.cooperative, kind="loan-rejected")
	else:
		frappe.throw(_("This loan has already been dealt with"))
	return get_loan(name)


@frappe.whitelist(methods=["POST"])
def send_loan(name, transfer_reference, note=None):
	"""Record that an approved loan was transferred from the group's bank."""
	loan = frappe.get_doc("Coop Loan", name)
	me = get_membership(loan.cooperative, capability="pay_out")
	ensure_not_self(me, loan.member, _("Another officer has to send your own loan"))
	if loan.status != "Approved":
		frappe.throw(_("Only an approved loan can be sent"))
	_send_loan(loan, me, transfer_reference, note)
	return get_loan(name)


def _send_loan(loan, me, transfer_reference, note=None):
	loan.status = "Sent"
	loan.transfer_reference = (transfer_reference or "").strip()
	if note:
		loan.review_note = note
	loan.save(ignore_permissions=True)
	notify(
		loan.user,
		_("{0} is on its way to {1} {2}").format(_money(loan.principal, loan.cooperative), loan.payout_bank_name,
			mask_account(loan.payout_account_number)),
		_("Confirm in the app when it arrives. Your repayments start from that day."),
		link=f"/loans/{loan.name}",
		cooperative=loan.cooperative,
		kind="loan-sent",
	)


def on_loan_decided(approval, approved, note=None):
	loan = frappe.get_doc("Coop Loan", approval.reference_name)
	if loan.status != "Pending":
		return
	amount = _money(loan.principal, loan.cooperative)
	if approved:
		loan.status = "Approved"
		loan.review_note = note or loan.review_note
		loan.save(ignore_permissions=True)
		notify(loan.user, _("Your loan of {0} was approved").format(amount),
			_("The treasurer will send it to your bank account and let you know."), link=f"/loans/{loan.name}",
			cooperative=loan.cooperative, kind="loan-approved")
		notify(holders(loan.cooperative, "pay_out", exclude_users=[loan.user]),
			_("Send {0} to {1}").format(amount, loan.member_name), _("The loan was approved."),
			link="/admin?tab=reviews", cooperative=loan.cooperative, kind="loan-approved")
	else:
		loan.status = "Rejected"
		loan.review_note = note
		loan.save(ignore_permissions=True)
		notify(loan.user, _("Your loan request for {0} was declined").format(amount), note, link=f"/loans/{loan.name}",
			cooperative=loan.cooperative, kind="loan-rejected")


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
	me = get_membership(loan.cooperative, capability="pay_out")
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
	if loan.status not in ("Pending", "Approved"):
		frappe.throw(_("Only a loan that hasn't been sent can be cancelled"))
	loan.status = "Cancelled"
	loan.save(ignore_permissions=True)
	approvals.cancel("Coop Loan", loan.name)
	return {"ok": True}


# Repayments (only through Paystack, see api/payments.py) ---------------------------

REPAYMENT_FIELDS = [
	"name", "loan", "member", "member_name", "amount", "payment_date", "status", "payment_method",
	"reference", "notes", "review_note", "creation", "penalty_part", "interest_part", "principal_part",
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


def _json(value):
	if isinstance(value, str):
		value = json.loads(value or "[]")
	return value or []


# Guarantors -----------------------------------------------------------------------------------


@frappe.whitelist()
def my_guarantees(cooperative):
	"""Loans I've been asked to guarantee or am guaranteeing."""
	me = get_membership(cooperative)
	rows = frappe.db.sql(
		"""select l.name as loan, l.member_name as borrower, l.principal, l.status as loan_status, l.balance,
			g.amount, g.status, g.responded_on
		from `tabCoop Loan Guarantor` g join `tabCoop Loan` l on l.name = g.parent
		where g.member = %s and g.status != 'Released' and l.status in ('Pending', 'Approved', 'Sent', 'Active')
		order by g.status = 'Pending' desc, l.creation desc""",
		me.name,
		as_dict=True,
	)
	return rows


@frappe.whitelist(methods=["POST"])
def respond_guarantee(loan, accept, note=None):
	"""Accept or decline being a guarantor. Accepting holds that much of your savings until the loan is repaid."""
	doc = frappe.get_doc("Coop Loan", loan)
	me = get_membership(doc.cooperative)
	row = next((g for g in doc.guarantors if g.member == me.name), None)
	if not row or row.status != "Pending" or doc.status not in ("Pending", "Approved"):
		frappe.throw(_("There's nothing to answer here"))
	accept = accept in (True, 1, "1", "true", "True")
	if accept:
		free = member_balances(me.name).withdrawable
		if flt(row.amount) > free + 0.005:
			frappe.throw(_("You can guarantee at most {0} right now").format(_money(free, doc.cooperative)))
	frappe.db.set_value("Coop Loan Guarantor", row.name, {"status": "Accepted" if accept else "Declined",
		"responded_on": now_datetime(), "note": note})
	amount = _money(row.amount, doc.cooperative)
	if accept:
		notify(doc.user, _("{0} agreed to guarantee {1} of your loan").format(me.full_name, amount), link=f"/loans/{doc.name}",
			cooperative=doc.cooperative, kind="guarantee")
		if not loans.guarantor_blocker(doc.name):
			ap = approvals.open_request("Coop Loan", doc.name)
			if ap:
				approvals._notify_approvers(ap)
	else:
		notify(doc.user, _("{0} can't guarantee your loan").format(me.full_name),
			note or _("Ask someone else to guarantee it instead."), link=f"/loans/{doc.name}", cooperative=doc.cooperative,
			kind="guarantee")
	return get_loan(loan)


@frappe.whitelist(methods=["POST"])
def replace_guarantor(loan, member, new_member, amount=None):
	"""The borrower asks someone else after a guarantor declined (or hasn't answered)."""
	doc = frappe.get_doc("Coop Loan", loan)
	me = get_membership(doc.cooperative)
	if doc.member != me.name or doc.status != "Pending":
		frappe.throw(_("Only your own pending loan requests can change guarantors"), frappe.PermissionError)
	row = next((g for g in doc.guarantors if g.member == member and g.status in ("Pending", "Declined")), None)
	if not row:
		frappe.throw(_("Choose a guarantor who declined or hasn't answered yet"))
	m = frappe.db.get_value("Coop Member", new_member, ["name", "cooperative", "status", "full_name", "user"], as_dict=True)
	if not m or m.cooperative != doc.cooperative or m.status != "Active" or new_member == doc.member or \
			any(g.member == new_member for g in doc.guarantors):
		frappe.throw(_("Choose another active member who isn't already a guarantor"))
	amount = flt(amount) or flt(row.amount)
	free = member_balances(new_member).withdrawable
	if amount > free + 0.005:
		frappe.throw(_("{0} can guarantee at most {1} right now").format(m.full_name, _money(free, doc.cooperative)))
	frappe.db.set_value("Coop Loan Guarantor", row.name, {"member": new_member, "member_name": m.full_name, "user": m.user,
		"amount": amount, "status": "Pending", "responded_on": None, "note": None})
	notify(m.user, _("{0} asked you to guarantee {1}").format(me.full_name, _money(amount, doc.cooperative)),
		_("Part of their loan of {0}.").format(_money(doc.principal, doc.cooperative)), link=f"/loans/{doc.name}",
		cooperative=doc.cooperative, kind="guarantee")
	return get_loan(loan)


# Restructuring and the loan book ----------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def propose_restructure(loan, months, reason):
	"""Spread what's left over a new number of months (no extra interest). Goes for approval."""
	doc = frappe.get_doc("Coop Loan", loan)
	me = get_membership(doc.cooperative, capability="loans")
	ensure_not_self(me, doc.member, _("Another officer has to restructure your own loan"))
	if doc.status != "Active":
		frappe.throw(_("Only an active loan can be restructured"))
	if not (reason or "").strip():
		frappe.throw(_("Say why it's being restructured"))
	months = cint(months)
	if not 1 <= months <= 60:
		frappe.throw(_("Choose between 1 and 60 months"))
	if approvals.open_request("Coop Loan", doc.name):
		frappe.throw(_("A change to this loan is already waiting for approval"))
	frappe.db.set_value("Coop Loan", doc.name, {"restructure_months": months, "restructure_reason": reason.strip()})
	approvals.request(doc.cooperative, "Loan restructure", ("Coop Loan", doc.name), amount=doc.balance,
		beneficiary=doc.member, summary=_("Spread {0}'s remaining {1} over {2} months").format(
			doc.member_name, _money(doc.balance, doc.cooperative), months))
	return get_loan(loan)


def on_restructure_decided(approval, approved, note=None):
	doc = frappe.get_doc("Coop Loan", approval.reference_name)
	months, reason = doc.restructure_months, doc.restructure_reason
	if approved and months:
		loans.restructure(doc.name, months, reason)
		notify(doc.user, _("Your loan now runs over {0} more months").format(months),
			_("What's left is spread evenly, with no extra interest. See your new repayment plan."), link=f"/loans/{doc.name}",
			cooperative=doc.cooperative, kind="loan-restructured")
	else:
		frappe.db.set_value("Coop Loan", doc.name, {"restructure_months": None, "restructure_reason": None})


@frappe.whitelist()
def loan_book(cooperative):
	"""The loan portfolio: what's out, what's late (portfolio at risk) and who to follow up."""
	me = get_membership(cooperative, capability="loans")
	active = frappe.get_all("Coop Loan", filters={"cooperative": cooperative, "status": "Active"},
		fields=["name", "member", "member_name", "principal", "balance", "overdue_amount", "days_overdue", "due_date",
			"product", "received_on"], order_by="days_overdue desc, balance desc")
	members = {m.name: m for m in frappe.get_all("Coop Member", filters={"cooperative": cooperative},
		fields=["name", "phone", "member_number"])}
	total = flt(sum(flt(l.balance) for l in active), 2)
	buckets = []
	for label, low, high in ((_("1–30 days"), 1, 30), (_("31–60 days"), 31, 60), (_("61–90 days"), 61, 90), (_("Over 90 days"), 91, 10 ** 6)):
		bal = flt(sum(flt(l.balance) for l in active if low <= cint(l.days_overdue) <= high), 2)
		buckets.append({"label": label, "balance": bal, "count": sum(1 for l in active if low <= cint(l.days_overdue) <= high),
			"percent": flt(bal / total * 100, 1) if total else 0})
	late = [l for l in active if cint(l.days_overdue) > 0]
	for l in late:
		l.phone = members.get(l.member, {}).get("phone")
		l.member_number = members.get(l.member, {}).get("member_number")
		l.guarantors = frappe.get_all("Coop Loan Guarantor", filters={"parent": l.name, "status": "Accepted"},
			fields=["member_name", "amount"])
	return {
		"currency": frappe.db.get_value("Cooperative", cooperative, "currency"),
		"active_count": len(active),
		"outstanding": total,
		"in_arrears": flt(sum(flt(l.balance) for l in late), 2),
		"par_percent": flt(sum(flt(l.balance) for l in late) / total * 100, 1) if total else 0,
		"overdue_amount": flt(sum(flt(l.overdue_amount) for l in late), 2),
		"buckets": buckets,
		"late": late,
		"waiting_to_send": frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Approved"}),
		"pending": frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Pending"}),
		"can_restructure": "loans" in me.capabilities,
	}


# Loan products (Admins) -----------------------------------------------------------------------


@frappe.whitelist()
def admin_loan_products(cooperative):
	get_membership(cooperative, admin=True)
	return _products(cooperative, enabled_only=False)


@frappe.whitelist(methods=["POST"])
def save_loan_product(cooperative, product_name, name=None, **values):
	get_membership(cooperative, capability="settings")
	doc = frappe.get_doc("Coop Loan Product", name) if name else frappe.new_doc("Coop Loan Product")
	if name and doc.cooperative != cooperative:
		frappe.throw(_("Loan product not found"))
	doc.cooperative = cooperative
	doc.product_name = (product_name or "").strip()
	for field in PRODUCT_FIELDS:
		if field in values and field not in ("name", "product_name"):
			doc.set(field, values[field])
	loans.validate_product(doc)
	doc.save(ignore_permissions=True)
	if not frappe.db.exists("Coop Loan Product", {"cooperative": cooperative, "enabled": 1}):
		frappe.throw(_("Keep at least one loan product switched on"))
	return _products(cooperative, enabled_only=False)


def _withdrawing_member(cooperative):
	"""Active members withdraw savings; someone who has left can still take out money that
	reached the group after they left (e.g. a late transfer to their old account number)."""
	me = get_membership(cooperative, allow_inactive=True)
	if me.status not in ("Active", "Left") or not me.name:
		frappe.throw(_("Your membership is {0}").format(_(me.status)), frappe.PermissionError)
	return me


@frappe.whitelist()
def withdrawal_eligibility(cooperative):
	me = _withdrawing_member(cooperative)
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
		"pending": frappe.db.exists("Coop Withdrawal", {"member": me.name, "status": ["in", ["Pending", "Approved", "Sent"]]}),
	}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def request_withdrawal(cooperative, amount, reason=None):
	me = _withdrawing_member(cooperative)
	check_hold(me.name)
	if not frappe.db.get_value("Coop Member", me.name, "payout_account_number"):
		frappe.throw(_("Add the bank account to receive the money first (Me → Where to send my money)"))
	if frappe.db.exists("Coop Withdrawal", {"member": me.name, "status": ["in", ["Pending", "Approved", "Sent"]]}):
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
	approvals.request(
		cooperative, "Withdrawal", ("Coop Withdrawal", doc.name), amount=doc.amount, beneficiary=me.name,
		summary=_("{0} asked to withdraw {1}").format(me.full_name, _money(doc.amount, cooperative)),
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
	data["approval"] = approvals.for_reference("Coop Withdrawal", name)
	return data


@frappe.whitelist(methods=["POST"])
def cancel_withdrawal(name):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if doc.member != me.name or doc.status not in ("Pending", "Approved"):
		frappe.throw(_("Only your own requests that haven't been sent can be cancelled"), frappe.PermissionError)
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	approvals.cancel("Coop Withdrawal", doc.name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def review_withdrawal(name, action, transfer_reference=None, note=None):
	"""Like review_loan: approve or decline a request through the approval rule, and send it once approved."""
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, admin=True)
	ensure_not_self(me, doc.member, _("Another officer has to deal with your own withdrawal"))
	if action not in ("approve", "reject"):
		frappe.throw(_("Invalid action"))
	if doc.status == "Pending":
		approvals.decide_for("Coop Withdrawal", doc.name, action == "approve", note)
		doc.reload()
		if doc.status == "Approved" and (transfer_reference or "").strip() and can(me, "pay_out"):
			_send_withdrawal(doc, transfer_reference, note)
	elif doc.status == "Approved":
		require(me, "pay_out")
		if action == "approve":
			_send_withdrawal(doc, transfer_reference, note)
		else:
			doc.status = "Rejected"
			doc.review_note = note
			doc.reviewed_by = frappe.session.user
			doc.save(ignore_permissions=True)
			notify(doc.user, _("Your withdrawal of {0} was declined").format(_money(doc.amount, doc.cooperative)), note,
				link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-rejected")
	else:
		frappe.throw(_("This withdrawal has already been dealt with"))
	return get_withdrawal(name)


@frappe.whitelist(methods=["POST"])
def send_withdrawal(name, transfer_reference, note=None):
	doc = frappe.get_doc("Coop Withdrawal", name)
	me = get_membership(doc.cooperative, capability="pay_out")
	ensure_not_self(me, doc.member, _("Another officer has to send your own withdrawal"))
	if doc.status != "Approved":
		frappe.throw(_("Only an approved withdrawal can be sent"))
	_send_withdrawal(doc, transfer_reference, note)
	return get_withdrawal(name)


def _send_withdrawal(doc, transfer_reference, note=None):
	doc.status = "Sent"
	doc.transfer_reference = (transfer_reference or "").strip()
	if note:
		doc.review_note = note
	doc.save(ignore_permissions=True)
	notify(doc.user, _("{0} is on its way to {1} {2}").format(_money(doc.amount, doc.cooperative), doc.payout_bank_name,
		mask_account(doc.payout_account_number)), _("Confirm in the app when it arrives."),
		link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-sent")


def on_withdrawal_decided(approval, approved, note=None):
	doc = frappe.get_doc("Coop Withdrawal", approval.reference_name)
	if doc.status != "Pending":
		return
	amount = _money(doc.amount, doc.cooperative)
	if approved:
		doc.status = "Approved"
		doc.review_note = note or doc.review_note
		doc.save(ignore_permissions=True)
		notify(doc.user, _("Your withdrawal of {0} was approved").format(amount),
			_("The treasurer will send it to your bank account."), link=f"/withdrawals/{doc.name}",
			cooperative=doc.cooperative, kind="withdrawal-approved")
		notify(holders(doc.cooperative, "pay_out", exclude_users=[doc.user]),
			_("Send {0} to {1}").format(amount, doc.member_name), _("The withdrawal was approved."),
			link="/admin?tab=reviews", cooperative=doc.cooperative, kind="withdrawal-approved")
	else:
		doc.status = "Rejected"
		doc.review_note = note
		doc.reviewed_by = frappe.session.user
		doc.save(ignore_permissions=True)
		notify(doc.user, _("Your withdrawal of {0} was declined").format(amount), note,
			link=f"/withdrawals/{doc.name}", cooperative=doc.cooperative, kind="withdrawal-rejected")


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
	me = get_membership(doc.cooperative, capability="pay_out")
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


# Offline payments (recorded by one officer, approved by another) ---------------------------


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60 * 10)
def record_offline_payment(cooperative, member, amount, payment_date, reference, payment_method="Bank Transfer",
		loan=None, note=None):
	"""A transfer or cash payment made outside Paystack. It's entered as text (no receipts or
	images) and only counts once another officer checks it arrived and approves it."""
	me = get_membership(cooperative, capability="record")
	m = frappe.db.get_value("Coop Member", member, ["name", "cooperative", "status", "full_name"], as_dict=True)
	if not m or m.cooperative != cooperative:
		frappe.throw(_("Member not found"))
	if m.status != "Active":
		frappe.throw(_("Payments can only be recorded for active members"))
	if payment_method not in ("Bank Transfer", "Cash"):
		frappe.throw(_("Choose bank transfer or cash"))
	reference = (reference or "").strip()
	if payment_method == "Bank Transfer" and not reference:
		frappe.throw(_("Enter the bank reference so the transfer can be checked against the statement"))
	amount = flt(amount, 2)
	if amount <= 0:
		frappe.throw(_("Enter the amount"))
	if getdate(payment_date) > getdate(today()):
		frappe.throw(_("The payment date can't be in the future"))
	for doctype in ("Coop Contribution", "Coop Loan Repayment"):
		if reference and frappe.db.exists(doctype, {"cooperative": cooperative, "reference": reference,
				"status": ["in", ["Pending", "Confirmed"]]}):
			frappe.throw(_("A payment with reference {0} has already been recorded").format(reference))
	common = {"cooperative": cooperative, "member": member, "amount": amount, "payment_method": payment_method,
		"reference": reference or None, "status": "Pending", "notes": note, "recorded_by": frappe.session.user}
	if loan:
		ln = frappe.db.get_value("Coop Loan", loan, ["member", "status", "balance"], as_dict=True)
		if not ln or ln.member != member or ln.status != "Active":
			frappe.throw(_("Choose one of the member's active loans"))
		if amount > flt(ln.balance) + 0.005:
			frappe.throw(_("That's more than the {0} left on the loan").format(_money(ln.balance, cooperative)))
		doc = frappe.get_doc({"doctype": "Coop Loan Repayment", "loan": loan, "payment_date": payment_date, **common})
		what = _("loan repayment")
	else:
		doc = frappe.get_doc({"doctype": "Coop Contribution", "contribution_date": payment_date, **common})
		what = _("savings")
	doc.insert(ignore_permissions=True)
	approvals.request(
		cooperative, "Offline payment", (doc.doctype, doc.name), amount=amount, beneficiary=member,
		summary=_("{0} paid {1} by {2} ({3}), recorded by {4}").format(
			m.full_name, _money(amount, cooperative), _(payment_method).lower(), what, me.full_name),
	)
	return {"doctype": doc.doctype, "name": doc.name, "status": doc.status}


def on_offline_payment_decided(approval, approved, note=None):
	doc = frappe.get_doc(approval.reference_doctype, approval.reference_name)
	if doc.status != "Pending":
		return
	doc.status = "Confirmed" if approved else "Rejected"
	doc.review_note = note
	doc.reviewed_by = frappe.session.user
	doc.flags.ignore_permissions = True
	doc.save()
	user = frappe.db.get_value("Coop Member", doc.member, "user")
	amount = _money(doc.amount, doc.cooperative)
	if approved:
		what = _("towards your loan") if doc.doctype == "Coop Loan Repayment" else _("to your savings")
		notify(user, _("{0} added {1}").format(amount, what), _("Your {0} payment was checked and recorded.").format(
			_(doc.payment_method).lower()), link=f"/receipt/{doc.doctype}/{doc.name}", cooperative=doc.cooperative,
			kind="payment")
	if doc.recorded_by and doc.recorded_by != frappe.session.user:
		notify(doc.recorded_by, _("The payment you recorded was {0}").format(_("approved") if approved else _("rejected")),
			note or approval.summary, cooperative=doc.cooperative, kind="approval")


# Transfers nobody could be matched to ------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def allocate_unmatched(reference, member):
	"""Add a transfer that came in without a recognisable account number to the member who sent
	it. Staff do this once they've found out who it was; nobody can allocate one to themselves."""
	tx = frappe.get_doc("Coop Payment Transaction", reference, for_update=True)
	if tx.status != "Unmatched" or not tx.cooperative:
		frappe.throw(_("This transfer has already been dealt with"))
	me = get_membership(tx.cooperative, capability="record")
	ensure_not_self(me, member, _("Another admin or the treasurer has to add this to your own savings"))
	m = frappe.db.get_value("Coop Member", member, ["name", "cooperative", "user", "full_name"], as_dict=True)
	if not m or m.cooperative != tx.cooperative:
		frappe.throw(_("Member not found"))
	doc = frappe.get_doc(
		{
			"doctype": "Coop Contribution",
			"cooperative": tx.cooperative,
			"member": member,
			"amount": tx.amount,
			"contribution_date": today(),
			"payment_method": "Bank Transfer",
			"reference": tx.reference,
			"status": "Confirmed",
			"reviewed_by": frappe.session.user,
			"notes": _("Transfer matched by {0}").format(me.full_name),
		}
	)
	doc.flags.money_received = True
	doc.insert(ignore_permissions=True)
	tx.update({"status": "Success", "allocated_to": member, "member": member, "user": m.user,
		"reference_doctype": doc.doctype, "reference_name": doc.name})
	tx.save(ignore_permissions=True)
	notify(m.user, _("{0} added to your savings").format(_money(doc.amount, tx.cooperative)),
		_("A transfer you sent was matched to you by {0}.").format(me.full_name),
		link=f"/receipt/Coop Contribution/{doc.name}", cooperative=tx.cooperative, kind="payment")
	return {"ok": True, "contribution": doc.name}


@frappe.whitelist()
def pending_reviews(cooperative):
	"""Everything waiting on the officers, in one call: approvals (marked with whether this officer
	can decide them), approved money to send, transfers members say didn't arrive, and transfers to match."""
	me = get_membership(cooperative, admin=True)
	payout_fields = ["payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"]
	items = []
	for a in approvals.pending(cooperative, me.user):
		a["detail"] = _approval_detail(a, me, payout_fields)
		items.append(a)

	to_send, disputes = [], []
	if can(me, "pay_out"):
		loans = _list("Coop Loan", cooperative, me, "all", "Approved", LOAN_FIELDS, 100, 0)
		withdrawals = _list("Coop Withdrawal", cooperative, me, "all", "Approved", WITHDRAWAL_FIELDS, 100, 0)
		for row in loans:
			row.kind = "loan"
		for row in withdrawals:
			row.kind = "withdrawal"
		for row in loans + withdrawals:
			row.payout_account = frappe.db.get_value("Coop Member", row.member, payout_fields, as_dict=True)
			row.is_mine = row.member == me.name
		to_send = loans + withdrawals
		disputes = frappe.get_all("Coop Loan", filters={"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1},
			fields=LOAN_FIELDS) + frappe.get_all("Coop Withdrawal",
			filters={"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1}, fields=WITHDRAWAL_FIELDS)
		for row in disputes:
			row.kind = "loan" if row.get("principal") is not None else "withdrawal"
			row.is_mine = row.member == me.name
	unmatched = frappe.get_all(
		"Coop Payment Transaction",
		filters={"cooperative": cooperative, "status": "Unmatched"},
		fields=["name", "amount", "currency", "creation", "message"],
		order_by="creation asc",
	) if can(me, "record") else []
	return {
		"approvals": items,
		"to_send": to_send,
		"disputes": disputes,
		"unmatched": unmatched,
		# Kept for older clients: join requests waiting
		"members": [a["detail"] for a in items if a["action"] == "Member admission"],
		"awaiting_members": frappe.get_all(
			"Coop Contribution",
			filters={"cooperative": cooperative, "status": "Pending", "payment_method": "Opening Balance"},
			fields=["name", "member", "member_name", "amount", "contribution_date"],
		),
		"pool": cooperative_totals(cooperative),
		"capabilities": me.capabilities,
		"now": now_datetime(),
	}


def _approval_detail(a, me, payout_fields):
	"""What an officer needs to decide an approval, by kind."""
	dt, dn = a["reference_doctype"], a["reference_name"]
	if a["action"] == "Member admission":
		return frappe.db.get_value("Coop Member", dn, ["name", "full_name", "user", "phone", "creation"], as_dict=True)
	if a["action"] in ("Loan", "Withdrawal"):
		fields = LOAN_FIELDS if a["action"] == "Loan" else WITHDRAWAL_FIELDS
		row = frappe.db.get_value(dt, dn, fields, as_dict=True)
		row.payout_account = frappe.db.get_value("Coop Member", row.member, payout_fields, as_dict=True)
		row.is_mine = row.member == me.name
		row.member_balances = member_balances(row.member, **({"exclude_loan": dn} if a["action"] == "Loan"
			else {"exclude_withdrawal": dn}))
		return row
	if a["action"] == "Offline payment":
		date_field = "payment_date" if dt == "Coop Loan Repayment" else "contribution_date"
		row = frappe.db.get_value(dt, dn, ["name", "member", "member_name", "amount", "payment_method", "reference",
			"notes", "recorded_by", date_field], as_dict=True)
		row.date = row.get(date_field)
		row.loan = frappe.db.get_value(dt, dn, "loan") if dt == "Coop Loan Repayment" else None
		row.recorded_by_name = frappe.db.get_value("User", row.recorded_by, "full_name")
		return row
	if a["action"] == "Manual journal":
		je = frappe.get_doc(dt, dn)
		names = dict(frappe.get_all("Coop Account", filters={"cooperative": je.cooperative}, fields=["name", "account_name"],
			as_list=True))
		return {"name": je.name, "remarks": je.remarks, "posting_date": je.posting_date, "total": je.total_debit,
			"lines": [{"account_name": names.get(l.account), "debit": l.debit, "credit": l.credit} for l in je.lines]}
	if a["action"] == "Share-out":
		return frappe.db.get_value(dt, dn, ["name", "period_start", "period_end", "surplus", "reserve_amount", "to_members",
			"basis"], as_dict=True)
	return None
