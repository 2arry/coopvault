"""Paystack online payments for contributions and loan repayments.

Flow: the PWA calls `initialize`, redirects the member to Paystack's checkout page,
Paystack redirects back to `<frontend_url>/pay/callback?reference=...` and the PWA
calls `verify`. The webhook does the same verification server-to-server so a payment
is recorded even if the member closes the browser before returning.
"""

import hashlib
import hmac
import json

import frappe
import requests
from frappe import _
from frappe.utils import flt, fmt_money, today

from coopvault.api import get_membership

PAYSTACK_API = "https://api.paystack.co"


def _settings():
	s = frappe.get_cached_doc("CoopVault Settings")
	secret = s.get_password("paystack_secret_key", raise_exception=False) if s.paystack_enabled else None
	return s, secret


def _paystack(method, path, secret, **kwargs):
	try:
		res = requests.request(
			method,
			PAYSTACK_API + path,
			headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
			timeout=30,
			**kwargs,
		)
		return res.json()
	except Exception:
		frappe.log_error(title="Paystack request failed", message=frappe.get_traceback())
		frappe.throw(_("Could not reach Paystack. Please try again."))


@frappe.whitelist(allow_guest=True)
def config():
	s, secret = _settings()
	return {"enabled": bool(s.paystack_enabled and secret), "public_key": s.paystack_public_key}


@frappe.whitelist(methods=["POST"])
def initialize(cooperative, purpose, amount, loan=None):
	s, secret = _settings()
	if not secret:
		frappe.throw(_("Online payment is not enabled. Record a bank transfer instead."))

	me = get_membership(cooperative)
	if not me.name:
		frappe.throw(_("Only members can pay"))
	amount = flt(amount, 2)
	if amount < 100:
		frappe.throw(_("Minimum online payment is 100"))

	currency = frappe.db.get_value("Cooperative", cooperative, "currency") or "NGN"
	if purpose == "Loan Repayment":
		ln = frappe.db.get_value("Coop Loan", loan, ["member", "status", "balance"], as_dict=True)
		if not ln or ln.member != me.name:
			frappe.throw(_("Loan not found"))
		if ln.status != "Active":
			frappe.throw(_("This loan is not active"))
		if amount > flt(ln.balance) + 0.005:
			frappe.throw(_("Amount exceeds the loan balance of {0}").format(fmt_money(ln.balance, currency=currency)))
	elif purpose != "Contribution":
		frappe.throw(_("Invalid payment purpose"))

	reference = "CV-" + frappe.generate_hash(length=16).upper()
	email = frappe.db.get_value("User", me.user, "email")
	callback = (s.frontend_url or "").rstrip("/") + "/pay/callback"

	res = _paystack(
		"POST",
		"/transaction/initialize",
		secret,
		json={
			"email": email,
			"amount": int(round(amount * 100)),
			"currency": currency,
			"reference": reference,
			"callback_url": callback,
			"metadata": {"cooperative": cooperative, "purpose": purpose, "loan": loan, "member": me.name},
		},
	)
	if not res.get("status"):
		frappe.log_error(title="Paystack initialize failed", message=json.dumps(res))
		frappe.throw(res.get("message") or _("Could not start the payment"))

	frappe.get_doc(
		{
			"doctype": "Coop Payment Transaction",
			"reference": reference,
			"purpose": purpose,
			"status": "Initiated",
			"amount": amount,
			"currency": currency,
			"cooperative": cooperative,
			"member": me.name,
			"user": me.user,
			"loan": loan if purpose == "Loan Repayment" else None,
			"authorization_url": res["data"]["authorization_url"],
		}
	).insert(ignore_permissions=True)
	return {"reference": reference, "authorization_url": res["data"]["authorization_url"]}


def _complete(reference, data):
	"""Record the contribution/repayment for a verified successful transaction (idempotent)."""
	tx = frappe.get_doc("Coop Payment Transaction", reference, for_update=True)
	if tx.status == "Success":
		return tx

	paid = flt(data.get("amount")) / 100
	tx.gateway_response = json.dumps(data, indent=1, default=str)

	if data.get("status") != "success":
		tx.status = "Abandoned" if data.get("status") == "abandoned" else "Failed"
		tx.save(ignore_permissions=True)
		return tx
	if abs(paid - flt(tx.amount)) > 0.01 or (data.get("currency") or tx.currency) != tx.currency:
		tx.status = "Failed"
		tx.save(ignore_permissions=True)
		frappe.log_error(title="Paystack amount mismatch", message=f"{reference}: expected {tx.amount}, got {paid}")
		return tx

	common = {
		"amount": paid,
		"payment_method": "Paystack",
		"reference": reference,
		"status": "Confirmed",
		"reviewed_by": "Administrator",
	}
	if tx.purpose == "Contribution":
		doc = frappe.get_doc(
			{
				"doctype": "Coop Contribution",
				"cooperative": tx.cooperative,
				"member": tx.member,
				"contribution_date": today(),
				**common,
			}
		)
	else:
		doc = frappe.get_doc(
			{"doctype": "Coop Loan Repayment", "loan": tx.loan, "payment_date": today(), **common}
		)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	tx.status = "Success"
	tx.reference_doctype = doc.doctype
	tx.reference_name = doc.name
	tx.save(ignore_permissions=True)
	return tx


@frappe.whitelist()
def verify(reference):
	tx = frappe.db.get_value(
		"Coop Payment Transaction", reference, ["name", "user", "status", "cooperative"], as_dict=True
	)
	if not tx:
		frappe.throw(_("Payment not found"))
	me = get_membership(tx.cooperative, allow_inactive=True)
	if tx.user != me.user and not me.is_admin:
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if tx.status != "Success":
		_, secret = _settings()
		res = _paystack("GET", f"/transaction/verify/{reference}", secret)
		if res.get("status") and res.get("data"):
			_complete(reference, res["data"])

	tx = frappe.get_doc("Coop Payment Transaction", reference)
	return {
		"reference": tx.reference,
		"status": tx.status,
		"purpose": tx.purpose,
		"amount": tx.amount,
		"currency": tx.currency,
		"cooperative": tx.cooperative,
		"loan": tx.loan,
		"document": tx.reference_name,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def paystack_webhook():
	_, secret = _settings()
	if not secret:
		return
	payload = frappe.request.get_data()
	signature = frappe.get_request_header("x-paystack-signature") or ""
	expected = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
	if not hmac.compare_digest(expected, signature):
		frappe.local.response.http_status_code = 401
		return

	event = json.loads(payload)
	data = event.get("data") or {}
	reference = data.get("reference")
	if event.get("event") == "charge.success" and reference and frappe.db.exists(
		"Coop Payment Transaction", reference
	):
		# Re-verify with Paystack rather than trusting the payload
		res = _paystack("GET", f"/transaction/verify/{reference}", secret)
		if res.get("status") and res.get("data"):
			frappe.set_user("Administrator")
			_complete(reference, res["data"])
			frappe.db.commit()
	return {"ok": True}
