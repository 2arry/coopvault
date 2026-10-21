"""Online payments through Paystack.

Checkout: the PWA calls `initialize`, opens Paystack's popup (InlineJS v2) with the returned
access code and calls `verify` when it closes. Paystack's redirect callback and the webhook
lead to the same idempotent `_complete`, so a payment is recorded exactly once whichever
path arrives first.

Also here: saved payment methods (cards and direct-debit mandates), Autosave (scheduled
contributions charged to a saved method), dedicated virtual accounts (a personal account
number per member; transfers into it become contributions automatically), and bank lookup.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, get_datetime, getdate, now_datetime, today

from coopvault import paystack
from coopvault.api import get_membership, require_login
from coopvault.ledger import TRACKED, contribution_standing, plan_on, plans_for, refresh_all
from coopvault.notify import notify, staff_of
from coopvault.utils import money_text, throttle

MIN_ONLINE_AMOUNT = 100
AUTOSAVE_MAX_FAILURES = 3


def _frontend_url(path=""):
	base = (frappe.db.get_single_value("CoopVault Settings", "frontend_url") or "").rstrip("/")
	return base + path


def _coop(cooperative):
	return frappe.db.get_value(
		"Cooperative",
		cooperative,
		["name", "cooperative_name", "currency", "accept_online_payments", "fee_bearer", "paystack_subaccount_code",
			"contribution_frequency"],
		as_dict=True,
	)


def _new_reference():
	return "CV-" + frappe.generate_hash(length=16).upper()


def _fee_for(coop, amount):
	return paystack.checkout_fee(amount) if coop.fee_bearer == "Member" else 0.0


def _split(coop):
	if coop.paystack_subaccount_code:
		return {"subaccount": coop.paystack_subaccount_code, "bearer": "subaccount"}
	return {}


# Config ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def config(cooperative=None):
	s = paystack.settings()
	enabled = paystack.is_enabled()
	data = {
		"enabled": enabled,
		"public_key": s.paystack_public_key if enabled else None,
		"test_mode": paystack.is_test_mode() if enabled else False,
		"autosave": bool(enabled and s.enable_autosave),
		"direct_debit": bool(enabled and s.enable_direct_debit),
		"dedicated_accounts": bool(enabled and s.enable_dedicated_accounts),
		"fees": {
			"percent": flt(s.fee_percent),
			"flat": flt(s.fee_flat),
			"flat_waived_below": flt(s.fee_flat_waived_below),
			"cap": flt(s.fee_cap),
		},
	}
	if cooperative and frappe.session.user != "Guest":
		get_membership(cooperative, allow_inactive=True)
		coop = _coop(cooperative)
		data.update(
			online=bool(enabled and coop.accept_online_payments),
			fee_bearer=coop.fee_bearer,
			settles_to_cooperative=bool(coop.paystack_subaccount_code),
		)
	return data


@frappe.whitelist()
def quote(cooperative, amount):
	get_membership(cooperative)
	coop = _coop(cooperative)
	amount = flt(amount, 2)
	fee = _fee_for(coop, amount)
	return {"amount": amount, "fee": fee, "total": flt(amount + fee, 2), "fee_bearer": coop.fee_bearer}


# Checkout -------------------------------------------------------------------------


def _check_repayment(me, loan, amount, currency):
	ln = frappe.db.get_value("Coop Loan", loan, ["member", "status", "balance"], as_dict=True)
	if not ln or ln.member != me.name:
		frappe.throw(_("Loan not found"))
	if ln.status != "Active":
		frappe.throw(_("This loan is not active"))
	if amount > flt(ln.balance) + 0.005:
		frappe.throw(_("Amount exceeds the loan balance of {0}").format(money_text(ln.balance, currency)))


@frappe.whitelist(methods=["POST"])
def initialize(cooperative, purpose, amount, loan=None, save_method=0, setup_autosave=0):
	if not paystack.is_enabled():
		frappe.throw(_("Online payments aren't switched on yet. Ask your admin."))
	me = get_membership(cooperative)
	if not me.name:
		frappe.throw(_("Only members can pay"))
	coop = _coop(cooperative)
	if not coop.accept_online_payments:
		frappe.throw(_("{0} is not accepting online payments").format(coop.cooperative_name))

	amount = flt(amount, 2)
	if amount < MIN_ONLINE_AMOUNT:
		frappe.throw(_("Minimum online payment is {0}").format(money_text(MIN_ONLINE_AMOUNT, coop.currency)))
	if purpose == "Loan Repayment":
		_check_repayment(me, loan, amount, coop.currency)
	elif purpose == "Contribution":
		loan = None
	else:
		frappe.throw(_("Invalid payment purpose"))

	setup_autosave = cint(setup_autosave) and purpose == "Contribution"
	save_method = cint(save_method) or setup_autosave
	fee = _fee_for(coop, amount)
	total = flt(amount + fee, 2)
	reference = _new_reference()
	email = frappe.db.get_value("User", me.user, "email")

	body = {
		"email": email,
		"amount": paystack.to_subunit(total),
		"currency": coop.currency,
		"reference": reference,
		"callback_url": _frontend_url("/pay/callback"),
		"metadata": {
			"cooperative": cooperative,
			"purpose": purpose,
			"loan": loan,
			"member": me.name,
			"cancel_action": _frontend_url(f"/pay/callback?reference={reference}&cancelled=1"),
			"custom_fields": [
				{"display_name": "Cooperative", "variable_name": "cooperative", "value": coop.cooperative_name},
				{"display_name": "Member", "variable_name": "member", "value": me.full_name},
				{"display_name": "For", "variable_name": "purpose", "value": purpose},
			],
		},
		**_split(coop),
	}
	if save_method:
		# Only cards give a reusable authorization straight from checkout
		body["channels"] = ["card"]

	data = paystack.initialize_transaction(**body)
	frappe.get_doc(
		{
			"doctype": "Coop Payment Transaction",
			"reference": reference,
			"purpose": purpose,
			"source": "Checkout",
			"status": "Initiated",
			"amount": amount,
			"fee": fee,
			"charged_amount": total,
			"currency": coop.currency,
			"cooperative": cooperative,
			"member": me.name,
			"user": me.user,
			"loan": loan,
			"save_method": save_method,
			"setup_autosave": setup_autosave,
			"access_code": data.get("access_code"),
			"authorization_url": data.get("authorization_url"),
		}
	).insert(ignore_permissions=True)
	return {
		"reference": reference,
		"access_code": data.get("access_code"),
		"authorization_url": data.get("authorization_url"),
		"amount": amount,
		"fee": fee,
		"total": total,
	}


def _tx_summary(tx):
	return {
		"reference": tx.reference,
		"status": tx.status,
		"purpose": tx.purpose,
		"source": tx.source,
		"amount": tx.amount,
		"fee": tx.fee,
		"charged_amount": tx.charged_amount,
		"currency": tx.currency,
		"cooperative": tx.cooperative,
		"loan": tx.loan,
		"channel": tx.channel,
		"message": tx.message,
		"document_type": tx.reference_doctype,
		"document": tx.reference_name,
		"autosave": bool(tx.setup_autosave),
	}


@frappe.whitelist(methods=["POST"])
def verify(reference):
	tx = frappe.db.get_value(
		"Coop Payment Transaction", reference, ["name", "user", "status", "cooperative"], as_dict=True
	)
	if not tx:
		frappe.throw(_("Payment not found"))
	me = get_membership(tx.cooperative, allow_inactive=True)
	if tx.user != me.user and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	current = None
	if tx.status not in ("Success", "Failed"):
		data = paystack.verify_transaction(reference)
		if data:
			# _complete reads under a row lock, so it sees a webhook that finished a moment ago;
			# a plain read here could still return this request's older snapshot
			current = _complete(reference, data)
	return _tx_summary(current or frappe.get_doc("Coop Payment Transaction", reference))


@frappe.whitelist(methods=["POST"])
def mark_abandoned(reference):
	"""Called when the member closes the popup without paying."""
	tx = frappe.get_doc("Coop Payment Transaction", reference)
	if tx.user != require_login():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if tx.status == "Initiated":
		tx.db_set("status", "Abandoned")
	return {"ok": True}


def _record_payment(tx, method="Paystack", note=None):
	"""Create the confirmed contribution / repayment for a successful transaction.

	A repayment larger than what is still owed (e.g. the loan was settled another way in
	the meantime) is split: the balance clears the loan and the rest is saved, so money that
	reached the cooperative is never left unrecorded.
	"""
	common = {
		"payment_method": method,
		"reference": tx.reference,
		"status": "Confirmed",
		"reviewed_by": "Administrator",
	}

	def insert(values):
		doc = frappe.get_doc(values)
		# The money is already in the cooperative's account, so it's recorded whatever the
		# member's status is now (suspended, or left and still paying into their old number)
		doc.flags.money_received = True
		doc.flags.payment_tx = tx
		return doc.insert(ignore_permissions=True)

	amount = flt(tx.amount)
	docs = []
	if tx.purpose == "Loan Repayment" and tx.loan:
		loan = frappe.db.get_value("Coop Loan", tx.loan, ["status", "balance"], as_dict=True)
		repay = min(amount, flt(loan.balance)) if loan and loan.status == "Active" else 0
		if repay > 0:
			docs.append(
				insert({"doctype": "Coop Loan Repayment", "loan": tx.loan, "amount": repay, "payment_date": today(),
					"notes": note, **common})
			)
		amount = flt(amount - repay, 2)
		note = _("Overpayment on loan {0}, added to savings").format(tx.loan) if amount > 0 else note
	if amount > 0:
		docs.append(
			insert({"doctype": "Coop Contribution", "cooperative": tx.cooperative, "member": tx.member,
				"amount": amount, "contribution_date": today(), "notes": note, **common})
		)
	return docs[0]


def _save_authorization(user, data, reference=None):
	auth = data.get("authorization") or {}
	code = auth.get("authorization_code")
	if not auth.get("reusable") or not code:
		return None
	signature = auth.get("signature") or code
	existing = frappe.db.get_value(
		"Coop Payment Authorization", {"user": user, "signature": signature, "status": "Active"}
	)
	if existing:
		return existing
	customer = data.get("customer") or {}
	channel = auth.get("channel") if auth.get("channel") in ("card", "direct_debit", "bank") else "card"
	brand = (auth.get("brand") or auth.get("card_type") or "").strip()
	label = (
		f"{brand.title()} •••• {auth.get('last4')}" if channel == "card" and auth.get("last4") else auth.get("bank")
	)
	doc = frappe.get_doc(
		{
			"doctype": "Coop Payment Authorization",
			"user": user,
			"label": label or _("Saved payment method"),
			"channel": channel,
			"status": "Active",
			"reference": reference,
			"authorization_code": code,
			"signature": signature,
			"customer_code": customer.get("customer_code") or customer.get("code"),
			"email": customer.get("email") or frappe.db.get_value("User", user, "email"),
			"brand": brand,
			"bank": auth.get("bank"),
			"last4": auth.get("last4"),
			"exp_month": cint(auth.get("exp_month")),
			"exp_year": cint(auth.get("exp_year")),
			"account_name": auth.get("account_name"),
		}
	).insert(ignore_permissions=True)
	return doc.name


def _complete(reference, data, method="Paystack"):
	"""Apply a verified Paystack transaction (idempotent)."""
	tx = frappe.get_doc("Coop Payment Transaction", reference, for_update=True)
	if tx.status in ("Success", "Refunded", "Disputed"):
		return tx

	tx.gateway_response = json.dumps(data, indent=1, default=str)
	tx.channel = data.get("channel")
	tx.paystack_fees = flt(data.get("fees")) / 100
	status = data.get("status")
	if status != "success":
		if status in ("abandoned", "failed", "reversed"):
			tx.status = "Abandoned" if status == "abandoned" else "Failed"
			tx.message = data.get("gateway_response") or data.get("message")
		tx.save(ignore_permissions=True)
		return tx

	paid = flt(data.get("amount")) / 100
	expected = flt(tx.charged_amount) or flt(tx.amount)
	if paid + 0.01 < expected or (data.get("currency") or tx.currency) != tx.currency:
		tx.status = "Failed"
		tx.message = _("Paid amount {0} does not match the expected {1}").format(paid, expected)
		tx.save(ignore_permissions=True)
		frappe.log_error(title="Paystack amount mismatch", message=f"{reference}: expected {expected}, got {paid}")
		return tx

	tx.settled_amount = settled_amount(tx, data)
	doc = _record_payment(tx, method=method)

	if tx.save_method and tx.user:
		tx.authorization = _save_authorization(tx.user, data, reference)
		if tx.setup_autosave and tx.authorization:
			_enable_autosave(tx.member, flt(tx.amount), tx.authorization, from_payment=True)

	tx.status = "Success"
	tx.paid_at = get_datetime(data["paid_at"][:19].replace("T", " ")) if data.get("paid_at") else now_datetime()
	tx.reference_doctype = doc.doctype
	tx.reference_name = doc.name
	tx.save(ignore_permissions=True)
	_notify_payment(tx, doc)
	return tx


def settled_amount(tx, data):
	"""What reaches the cooperative from a successful charge: the amount charged minus Paystack's
	fee (the cooperative's subaccount bears it) and any platform commission."""
	charged = flt(data.get("amount")) / 100 or flt(tx.charged_amount) or flt(tx.amount)
	split = data.get("fees_split") or {}
	if split.get("subaccount") is not None:
		return flt(flt(split["subaccount"]) / 100, 2)
	commission = 0.0
	has_subaccount = (data.get("subaccount") or {}).get("subaccount_code") or (
		tx.cooperative and frappe.db.get_value("Cooperative", tx.cooperative, "paystack_subaccount_code")
	)
	if has_subaccount:
		commission = charged * flt(paystack.settings().platform_fee_percent) / 100
	return flt(charged - flt(data.get("fees")) / 100 - commission, 2)


def _notify_payment(tx, doc):
	currency = tx.currency
	what = _("towards your loan") if doc.doctype == "Coop Loan Repayment" else _("to your savings")
	via = {"Autosave": _(" by Autosave"), "Dedicated Account": _(" by transfer to your account number")}.get(tx.source, "")
	notify(
		tx.user,
		_("{0} added {1}{2}").format(money_text(tx.amount, currency), what, via),
		link=f"/receipt/{doc.doctype}/{doc.name}",
		cooperative=tx.cooperative,
		kind="payment",
		# The member is looking at the receipt already when they paid at checkout
		push=tx.source != "Checkout",
	)


def _reverse_payment(tx, reason):
	"""Undo the savings/repayment a refunded or charged-back payment created."""
	for doctype in ("Coop Contribution", "Coop Loan Repayment"):
		for name in frappe.get_all(doctype, filters={"reference": tx.reference, "status": "Confirmed"}, pluck="name"):
			doc = frappe.get_doc(doctype, name)
			doc.status = "Reversed"
			doc.review_note = reason
			doc.flags.ignore_permissions = True
			doc.save()
	if tx.member:
		refresh_all(member=tx.member, cooperative=tx.cooperative, loan=tx.loan)
	notify(
		[tx.user, *staff_of(tx.cooperative)],
		_("A payment of {0} was reversed").format(money_text(tx.amount, tx.currency)),
		reason,
		cooperative=tx.cooperative,
		kind="payment-reversed",
	)


def _handle_refund(data):
	reference = data.get("transaction_reference")
	if not reference or not frappe.db.exists("Coop Payment Transaction", reference):
		return
	tx = frappe.get_doc("Coop Payment Transaction", reference, for_update=True)
	refunded = flt(data.get("amount")) / 100
	tx.refunded_amount = flt(tx.refunded_amount) + refunded
	if tx.status in ("Success", "Disputed") and tx.refunded_amount + 0.01 >= flt(tx.amount):
		tx.status = "Refunded"
		tx.save(ignore_permissions=True)
		_reverse_payment(tx, _("Refunded through Paystack"))
	else:
		tx.save(ignore_permissions=True)
		notify(staff_of(tx.cooperative), _("Partial refund on {0} needs checking").format(reference),
			_("{0} of {1} was refunded.").format(refunded, tx.amount), cooperative=tx.cooperative, kind="payment-reversed")


def _handle_dispute(kind, data):
	reference = ((data.get("transaction") or {}).get("reference")) or data.get("transaction_reference")
	if not reference or not frappe.db.exists("Coop Payment Transaction", reference):
		return
	tx = frappe.get_doc("Coop Payment Transaction", reference, for_update=True)
	if kind == "charge.dispute.create" and tx.status == "Success":
		tx.status = "Disputed"
		tx.save(ignore_permissions=True)
		notify(staff_of(tx.cooperative), _("A card payment is being disputed"),
			_("{0} paid by {1}. It stays counted until the dispute is settled.").format(
				money_text(tx.amount, tx.currency), frappe.db.get_value("User", tx.user, "full_name")),
			cooperative=tx.cooperative, kind="payment-disputed")
	elif kind == "charge.dispute.resolve" and tx.status == "Disputed":
		if data.get("resolution") == "merchant-accepted":
			tx.status = "Refunded"
			tx.save(ignore_permissions=True)
			_reverse_payment(tx, _("The card holder's dispute was accepted"))
		else:
			tx.status = "Success"
			tx.save(ignore_permissions=True)


# Saved payment methods ------------------------------------------------------------


def _method_row(a):
	return {
		"name": a.name,
		"label": a.label,
		"channel": a.channel,
		"status": a.status,
		"brand": a.brand,
		"bank": a.bank,
		"last4": a.last4,
		"exp_month": a.exp_month,
		"exp_year": a.exp_year,
	}


@frappe.whitelist()
def list_methods():
	user = require_login()
	rows = frappe.get_all(
		"Coop Payment Authorization",
		filters={"user": user, "status": "Active"},
		fields=["name", "label", "channel", "status", "brand", "bank", "last4", "exp_month", "exp_year"],
		order_by="creation desc",
	)
	return [_method_row(r) for r in rows]


@frappe.whitelist(methods=["POST"])
def remove_method(name):
	doc = frappe.get_doc("Coop Payment Authorization", name)
	if doc.user != require_login():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	code = doc.get_password("authorization_code", raise_exception=False)
	if code and paystack.is_enabled():
		paystack.deactivate_authorization(code)
	doc.db_set("status", "Inactive")
	for m in frappe.get_all("Coop Member", filters={"autosave_authorization": name}, pluck="name"):
		_disable_autosave(m, _("The saved payment method was removed"))
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def start_mandate(cooperative):
	"""Begin a direct-debit mandate: the member authorises their bank account on Paystack."""
	if not (paystack.is_enabled() and paystack.settings().enable_direct_debit):
		frappe.throw(_("Direct debit is not available"))
	me = get_membership(cooperative)
	email = frappe.db.get_value("User", me.user, "email")
	data = paystack.initialize_mandate(email, _frontend_url(f"/autosave?coop={cooperative}&mandate=1"))
	frappe.get_doc(
		{
			"doctype": "Coop Payment Authorization",
			"user": me.user,
			"email": email,
			"label": _("Bank account (direct debit)"),
			"channel": "direct_debit",
			"status": "Pending",
			"reference": data.get("reference"),
		}
	).insert(ignore_permissions=True)
	return {"redirect_url": data.get("redirect_url"), "reference": data.get("reference")}


def _activate_mandate(auth_name, data):
	doc = frappe.get_doc("Coop Payment Authorization", auth_name)
	customer = data.get("customer") or {}
	doc.update(
		{
			"authorization_code": data.get("authorization_code"),
			"signature": data.get("signature") or data.get("authorization_code"),
			"customer_code": customer.get("code") or customer.get("customer_code"),
			"bank": data.get("bank"),
			"last4": data.get("last4"),
			"account_name": data.get("account_name"),
			"label": _("{0} (direct debit)").format(data.get("bank") or _("Bank account")),
			"status": "Active" if data.get("active") else "Pending",
		}
	)
	doc.save(ignore_permissions=True)
	return doc


@frappe.whitelist(methods=["POST"])
def check_mandate(reference):
	auth = frappe.db.get_value(
		"Coop Payment Authorization", {"reference": reference}, ["name", "user", "status"], as_dict=True
	)
	if not auth or auth.user != require_login():
		frappe.throw(_("Mandate not found"))
	if auth.status == "Pending":
		data = paystack.verify_mandate(reference)
		if data and data.get("authorization_code"):
			_activate_mandate(auth.name, data)
	doc = frappe.get_doc("Coop Payment Authorization", auth.name)
	return _method_row(doc)


# Autosave -------------------------------------------------------------------------


def _next_autosave_date(member, frequency, after=None, earliest=None):
	"""When to charge next.

	With `after`, one period later (used once a scheduled charge has gone through). Otherwise
	the day the member's next unpaid period starts, or `earliest` (default today) if a
	contribution is already due.
	"""
	if after:
		after = getdate(after)
		return add_days(after, 7) if frequency == "Weekly" else getdate(
			add_months(after, 3 if frequency == "Quarterly" else 1)
		)
	earliest = getdate(earliest or today())
	standing = contribution_standing(member)
	if standing.get("tracked") and standing.state == "up_to_date":
		return max(getdate(standing.next_due_date), earliest)
	return earliest


def _after_charge_date(member, frequency, charged_for):
	"""Next charge after a successful one: when the next unpaid period starts, or a period later."""
	standing = contribution_standing(member)
	if standing.get("tracked") and standing.state == "up_to_date":
		return max(getdate(standing.next_due_date), getdate(add_days(today(), 1)))
	return _next_autosave_date(member, frequency, after=charged_for or today())


def _enable_autosave(member, amount, authorization, start_date=None, from_payment=False):
	m = frappe.db.get_value("Coop Member", member, ["name", "cooperative"], as_dict=True)
	coop = frappe.db.get_value(
		"Cooperative", m.cooperative, ["contribution_frequency", "contribution_amount"], as_dict=True
	)
	tracked = coop.contribution_frequency in ("Weekly", "Monthly", "Quarterly")
	frequency = coop.contribution_frequency if tracked else "Monthly"
	if from_payment and tracked and flt(coop.contribution_amount):
		# A catch-up payment shouldn't become the recurring amount: save the regular contribution
		amount = flt(coop.contribution_amount)
	if start_date:
		next_date = getdate(start_date)
	else:
		# Never charge again on the day of the payment that turned Autosave on
		next_date = _next_autosave_date(member, frequency, earliest=add_days(today(), 1) if from_payment else None)
	frappe.db.set_value(
		"Coop Member",
		member,
		{
			"autosave_enabled": 1,
			"autosave_amount": amount,
			"autosave_authorization": authorization,
			"autosave_next_date": next_date,
			"autosave_failures": 0,
			"autosave_last_error": None,
		},
	)
	return next_date


def _disable_autosave(member, reason=None):
	frappe.db.set_value(
		"Coop Member", member, {"autosave_enabled": 0, "autosave_next_date": None, "autosave_last_error": reason}
	)


def _autosave_amount(cooperative, amount, on_date=None):
	"""What Autosave charges on a date. Set to the regular contribution, it follows plan changes;
	a custom amount stays as the member chose it."""
	plans = plans_for(cooperative)
	plan = plan_on(plans, getdate(on_date or today()))
	if plan.frequency in TRACKED and flt(plan.amount) and any(abs(flt(p.amount) - flt(amount)) < 0.005 for p in plans):
		return flt(plan.amount)
	return flt(amount)


def autosave_status(member):
	m = frappe.db.get_value(
		"Coop Member",
		member,
		["cooperative", "autosave_enabled", "autosave_amount", "autosave_authorization", "autosave_next_date",
			"autosave_last_error"],
		as_dict=True,
	)
	method = None
	if m.autosave_authorization:
		a = frappe.db.get_value(
			"Coop Payment Authorization", m.autosave_authorization,
			["name", "label", "channel", "status", "brand", "bank", "last4", "exp_month", "exp_year"], as_dict=True,
		)
		method = _method_row(a) if a else None
	return {
		"enabled": bool(m.autosave_enabled),
		"amount": _autosave_amount(m.cooperative, m.autosave_amount, m.autosave_next_date) if m.autosave_enabled else m.autosave_amount,
		"next_date": m.autosave_next_date,
		"last_error": m.autosave_last_error,
		"method": method,
	}


@frappe.whitelist(methods=["POST"])
def setup_autosave(cooperative, amount, authorization, start_date=None):
	if not paystack.is_enabled() or not paystack.settings().enable_autosave:
		frappe.throw(_("Autosave is not available"))
	me = get_membership(cooperative)
	amount = flt(amount, 2)
	if amount < MIN_ONLINE_AMOUNT:
		frappe.throw(_("Autosave amount is too small"))
	auth = frappe.db.get_value("Coop Payment Authorization", authorization, ["user", "status"], as_dict=True)
	if not auth or auth.user != me.user or auth.status != "Active":
		frappe.throw(_("Choose an active saved payment method"))
	if start_date and getdate(start_date) < getdate(today()):
		frappe.throw(_("Start date cannot be in the past"))
	_enable_autosave(me.name, amount, authorization, start_date)
	return autosave_status(me.name)


@frappe.whitelist(methods=["POST"])
def cancel_autosave(cooperative):
	me = get_membership(cooperative, allow_inactive=True)
	_disable_autosave(me.name)
	return autosave_status(me.name)


@frappe.whitelist()
def get_autosave(cooperative):
	me = get_membership(cooperative)
	return {"autosave": autosave_status(me.name), "methods": list_methods()}


def charge_autosave(member):
	"""Charge one member's autosave contribution. Returns the transaction."""
	m = frappe.get_doc("Coop Member", member)
	auth = frappe.get_doc("Coop Payment Authorization", m.autosave_authorization)
	if m.status != "Active" or auth.status != "Active":
		_disable_autosave(member, _("Membership or payment method is no longer active"))
		return None
	coop = _coop(m.cooperative)
	amount = _autosave_amount(m.cooperative, m.autosave_amount, today())
	if abs(amount - flt(m.autosave_amount)) >= 0.005:
		m.db_set("autosave_amount", amount, update_modified=False)
	fee = _fee_for(coop, amount)
	reference = _new_reference()
	tx = frappe.get_doc(
		{
			"doctype": "Coop Payment Transaction",
			"reference": reference,
			"purpose": "Contribution",
			"source": "Autosave",
			"status": "Initiated",
			"amount": amount,
			"fee": fee,
			"charged_amount": flt(amount + fee, 2),
			"currency": coop.currency,
			"cooperative": m.cooperative,
			"member": member,
			"user": m.user,
			"authorization": auth.name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # the webhook may arrive before this request finishes

	res = paystack.charge_authorization(
		email=auth.email,
		amount=paystack.to_subunit(amount + fee),
		authorization_code=auth.get_password("authorization_code"),
		reference=reference,
		currency=coop.currency,
		metadata=json.dumps({"cooperative": m.cooperative, "member": member, "purpose": "Contribution", "autosave": 1}),
		**_split(coop),
	)
	data = res.get("data") or {}
	frequency = coop.contribution_frequency if coop.contribution_frequency in ("Weekly", "Monthly", "Quarterly") else "Monthly"
	if res.get("status") and data.get("status") == "success":
		_complete(reference, data)
	elif res.get("status") and data.get("status") in ("pending", "ongoing", "processing", "queued"):
		tx.db_set("status", "Pending")
	else:
		message = data.get("gateway_response") or res.get("message") or _("The charge was declined")
		tx.db_set({"status": "Failed", "message": message})
		failures = cint(m.autosave_failures) + 1
		if failures >= AUTOSAVE_MAX_FAILURES:
			_disable_autosave(member, _("Stopped after {0} failed attempts: {1}").format(failures, message))
			notify(m.user, _("Autosave has stopped"), _("Your card was declined {0} times ({1}). Pay manually or set up Autosave again.").format(
				failures, message), link="/autosave", cooperative=m.cooperative, kind="autosave")
		else:
			frappe.db.set_value("Coop Member", member, {"autosave_failures": failures, "autosave_last_error": message})
			notify(m.user, _("Autosave payment didn't go through"), _("{0}. We'll try again tomorrow.").format(message),
				link="/autosave", cooperative=m.cooperative, kind="autosave")
		return tx

	frappe.db.set_value(
		"Coop Member",
		member,
		{
			"autosave_next_date": _after_charge_date(member, frequency, m.autosave_next_date),
			"autosave_failures": 0,
			"autosave_last_error": None,
		},
	)
	return tx


def remind_autosave():
	"""Daily: tell members two days before their card is charged."""
	if not paystack.is_enabled() or not paystack.settings().enable_autosave:
		return
	soon = add_days(today(), 2)
	for m in frappe.get_all(
		"Coop Member",
		filters={"autosave_enabled": 1, "status": "Active", "autosave_next_date": soon},
		fields=["name", "user", "cooperative", "autosave_amount", "autosave_reminded_for"],
	):
		if str(m.autosave_reminded_for or "") == str(soon):
			continue
		currency = frappe.db.get_value("Cooperative", m.cooperative, "currency")
		notify(m.user, _("Autosave in 2 days"), _("We'll charge {0} on {1}. Change or pause it in the app.").format(
			money_text(_autosave_amount(m.cooperative, m.autosave_amount, soon), currency),
			frappe.utils.formatdate(soon, "d MMM")),
			link="/autosave", cooperative=m.cooperative, kind="autosave")
		frappe.db.set_value("Coop Member", m.name, "autosave_reminded_for", soon, update_modified=False)
	frappe.db.commit()


def run_autosave():
	"""Daily scheduler job."""
	if not paystack.is_enabled() or not paystack.settings().enable_autosave:
		return
	due = frappe.get_all(
		"Coop Member",
		filters={"autosave_enabled": 1, "status": "Active", "autosave_next_date": ["<=", today()]},
		pluck="name",
	)
	for member in due:
		try:
			charge_autosave(member)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Autosave failed for {member}", message=frappe.get_traceback())


# Dedicated virtual accounts --------------------------------------------------------


def dedicated_account_status(member):
	m = frappe.db.get_value(
		"Coop Member", member,
		["dva_status", "dva_account_number", "dva_account_name", "dva_bank_name", "dva_error"], as_dict=True,
	)
	return {
		"status": m.dva_status or None,
		"account_number": m.dva_account_number,
		"account_name": m.dva_account_name,
		"bank_name": m.dva_bank_name,
		"error": m.dva_error,
	}


@frappe.whitelist(methods=["POST"])
def request_dedicated_account(cooperative, bvn=None):
	if not (paystack.is_enabled() and paystack.settings().enable_dedicated_accounts):
		frappe.throw(_("Dedicated accounts are not available"))
	me = get_membership(cooperative)
	if not frappe.db.get_value("User", me.user, "mobile_no"):
		frappe.throw(_("Add your phone number in your profile first"))
	m = frappe.db.get_value(
		"Coop Member", me.name, ["dva_status", "payout_bank_code", "payout_account_number"], as_dict=True
	)
	if m.dva_status == "Assigned":
		return dedicated_account_status(me.name)
	elsewhere = frappe.db.get_value(
		"Coop Member", {"user": me.user, "name": ["!=", me.name], "dva_status": ["in", ["Requested", "Assigned"]]},
		"cooperative",
	)
	if elsewhere:
		# Paystack gives each person one account number, and transfers must land in the right group
		frappe.throw(_("Your account number is already linked to {0}. Each person can have one.").format(
			frappe.db.get_value("Cooperative", elsewhere, "cooperative_name")))
	coop = _coop(cooperative)
	paystack.assign_dedicated_account(
		me.user,
		subaccount=coop.paystack_subaccount_code,
		bank_code=m.payout_bank_code,
		account_number=m.payout_account_number,
		bvn=(bvn or "").strip() or None,
	)
	frappe.db.set_value("Coop Member", me.name, {"dva_status": "Requested", "dva_error": None})
	return dedicated_account_status(me.name)


def _apply_dedicated_account(email, data, ok=True):
	user = frappe.db.get_value("User", {"email": email})
	if not user:
		return
	members = frappe.get_all(
		"Coop Member", filters={"user": user, "dva_status": "Requested"}, pluck="name", order_by="modified desc"
	)
	customer = data.get("customer") or {}
	for member in members[:1]:
		if ok:
			acct = data.get("dedicated_account") or {}
			frappe.db.set_value(
				"Coop Member",
				member,
				{
					"dva_status": "Assigned",
					"dva_account_number": acct.get("account_number"),
					"dva_account_name": acct.get("account_name"),
					"dva_bank_name": (acct.get("bank") or {}).get("name"),
					"dva_id": acct.get("id"),
					"paystack_customer_code": customer.get("customer_code"),
					"dva_error": None,
				},
			)
			notify(user, _("Your savings account number is ready"),
				_("{0} {1}. Transfers to it are added to your savings automatically.").format(
					(acct.get("bank") or {}).get("name"), acct.get("account_number")), link="/home", kind="dva")
		else:
			frappe.db.set_value(
				"Coop Member",
				member,
				{"dva_status": "Failed", "dva_error": _("Paystack could not verify your identity for an account")},
			)
			notify(user, _("We couldn't create your account number"), _("Paystack couldn't verify your details."),
				link="/profile", kind="dva")


def close_dedicated_account(member):
	"""When someone leaves, switch their account number off at Paystack so later transfers bounce
	instead of landing in a group they left. The number stays on the record, so a transfer that
	still slips through is matched to them and can be withdrawn."""
	m = frappe.db.get_value(
		"Coop Member", member,
		["name", "cooperative", "full_name", "dva_status", "dva_id", "paystack_customer_code", "dva_account_number"],
		as_dict=True,
	)
	if not m or m.dva_status not in ("Assigned", "Requested"):
		return
	closed = False
	if paystack.is_enabled():
		try:
			closed = paystack.deactivate_dedicated_account(m.dva_id, m.paystack_customer_code)
		except Exception:
			frappe.log_error(title="Couldn't close dedicated account", message=frappe.get_traceback())
	frappe.db.set_value(
		"Coop Member", member,
		{"dva_status": "Closed", "dva_error": None if closed else _("Paystack didn't confirm the account number was switched off")},
	)
	if not closed and m.dva_account_number:
		notify(
			staff_of(m.cooperative),
			_("{0}'s account number may still accept transfers").format(m.full_name),
			_("They left, but Paystack didn't confirm {0} was switched off. Any transfer to it is still recorded for them.").format(
				m.dva_account_number
			),
			cooperative=m.cooperative,
			kind="payment-unmatched",
		)


def _member_for_account_number(number):
	"""The membership an account number belongs to. Paystack can hand the same number back when
	someone moves to a new group, so the live assignment wins over older, closed ones."""
	if not number:
		return None
	rows = frappe.get_all(
		"Coop Member", filters={"dva_account_number": number}, fields=["name", "dva_status"], order_by="modified desc"
	)
	live = [r for r in rows if r.dva_status == "Assigned"]
	best = (live or rows)[:1]
	return best[0].name if best else None


def _cooperative_for_subaccount(data):
	code = (data.get("subaccount") or {}).get("subaccount_code")
	return code and frappe.db.get_value("Cooperative", {"paystack_subaccount_code": code})


def _record_dedicated_transfer(data):
	"""A transfer into a member's account number becomes a confirmed contribution.

	The money is already in the cooperative's account by the time Paystack tells us, so it is
	always recorded: for a suspended or former member too (staff are told so they can return it),
	and as an unmatched transfer when no member can be found.
	"""
	reference = data.get("reference")
	if frappe.db.exists("Coop Payment Transaction", reference):
		return _complete(reference, data, method="Bank Transfer")
	receiver = (data.get("authorization") or {}).get("receiver_bank_account_number")
	member = _member_for_account_number(receiver)
	paid = flt(data.get("amount")) / 100
	currency = data.get("currency") or "NGN"
	if not member:
		cooperative = _cooperative_for_subaccount(data)
		tx = frappe.get_doc(
			{
				"doctype": "Coop Payment Transaction",
				"reference": reference,
				"purpose": "Contribution",
				"source": "Dedicated Account",
				"status": "Unmatched",
				"amount": paid,
				"charged_amount": paid,
				"currency": currency,
				"cooperative": cooperative,
				"channel": data.get("channel"),
				"paystack_fees": flt(data.get("fees")) / 100,
				"message": _("Sent to account number {0}, which doesn't belong to any member").format(receiver or "?"),
				"gateway_response": json.dumps(data, indent=1, default=str),
			}
		)
		tx.settled_amount = settled_amount(tx, data)
		tx.insert(ignore_permissions=True)
		if cooperative:
			notify(
				staff_of(cooperative),
				_("A transfer of {0} couldn't be matched to a member").format(money_text(paid, currency)),
				_("Sent to account number {0}. Find out who sent it so it can be added to their savings.").format(
					receiver or "?"
				),
				cooperative=cooperative,
				kind="payment-unmatched",
			)
		frappe.log_error(title="Unmatched dedicated account transfer", message=json.dumps(data, default=str)[:5000])
		return tx

	m = frappe.db.get_value("Coop Member", member, ["cooperative", "user", "status", "full_name"], as_dict=True)
	frappe.get_doc(
		{
			"doctype": "Coop Payment Transaction",
			"reference": reference,
			"purpose": "Contribution",
			"source": "Dedicated Account",
			"status": "Initiated",
			"amount": paid,
			"charged_amount": paid,
			"currency": currency,
			"cooperative": m.cooperative,
			"member": member,
			"user": m.user,
		}
	).insert(ignore_permissions=True)
	tx = _complete(reference, data, method="Bank Transfer")
	if m.status != "Active":
		what = _("have left the group") if m.status == "Left" else _("are suspended")
		notify(
			staff_of(m.cooperative),
			_("{0} sent {1} to their account number").format(m.full_name, money_text(paid, currency)),
			_("They {0}, so it was added to what the group owes them. They can withdraw it from the app.").format(what),
			link=f"/admin/members/{member}",
			cooperative=m.cooperative,
			kind="payment-inactive-member",
		)
	return tx


# Banks ------------------------------------------------------------------------------


@frappe.whitelist()
def banks(currency="NGN"):
	require_login()
	throttle("banks", 30, 600)
	if not paystack.is_enabled():
		return []
	return paystack.list_banks(currency)


@frappe.whitelist()
def resolve_account(bank_code, account_number):
	require_login()
	throttle("resolve-account", 10, 600)
	return {"account_name": paystack.resolve_account(account_number, bank_code)}


# Webhook ----------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True, methods=["POST"])
def paystack_webhook():
	payload = frappe.request.get_data()
	if not paystack.valid_signature(payload, frappe.get_request_header("x-paystack-signature") or ""):
		frappe.local.response.http_status_code = 401
		return {"ok": False}

	event = json.loads(payload)
	kind, data = event.get("event"), event.get("data") or {}
	frappe.set_user("Administrator")
	try:
		if kind == "charge.success" and data.get("reference"):
			# Never trust the payload alone: re-fetch the transaction from Paystack
			verified = paystack.verify_transaction(data["reference"])
			if verified and verified.get("status") == "success":
				if frappe.db.exists("Coop Payment Transaction", data["reference"]):
					_complete(data["reference"], verified)
				elif verified.get("channel") == "dedicated_nuban":
					_record_dedicated_transfer(verified)
		elif kind == "direct_debit.authorization.active":
			name = frappe.db.get_value("Coop Payment Authorization", {"reference": data.get("reference")})
			if name:
				_activate_mandate(name, data)
		elif kind in ("dedicatedaccount.assign.success", "dedicatedaccount.assign.failed"):
			email = (data.get("customer") or {}).get("email")
			_apply_dedicated_account(email, data, ok=kind.endswith("success"))
		elif kind == "refund.processed":
			_handle_refund(data)
		elif kind in ("charge.dispute.create", "charge.dispute.resolve"):
			_handle_dispute(kind, data)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Paystack webhook {kind} failed", message=frappe.get_traceback())
		# A non-2xx reply makes Paystack retry the event later instead of it being lost
		frappe.local.response.http_status_code = 500
		return {"ok": False}
	return {"ok": True}
