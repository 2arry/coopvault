"""A group's Cooperative360 subscription, as its admins see it.

Admins pick a plan and pay for months in advance through Paystack. The money goes to the
platform's own Paystack balance (no split to the group's subaccount) and never enters the group's
books: if the group's funds pay for it, the treasurer can record it as a running cost. Paid months
start after the trial or the current paid period ends, so nothing paid for is lost.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, get_datetime, getdate, now_datetime, today

from coopvault import paystack, plans
from coopvault.api import get_membership, require_login
from coopvault.api.coop import _platform_log
from coopvault.notify import notify, staff_of
from coopvault.utils import money_text

MONTHS = (1, 3, 6, 12)


def _frontend_url(path=""):
	return (frappe.db.get_single_value("CoopVault Settings", "frontend_url") or "").rstrip("/") + path


@frappe.whitelist()
def get_subscription(cooperative):
	me = get_membership(cooperative, admin=True)
	st = plans.state(cooperative)
	use = plans.usage(cooperative)
	offered = frappe.get_all("Coop Plan", filters={"enabled": 1}, fields=plans.FIELDS, order_by="price_monthly asc")
	if st.plan and not any(p.name == st.plan.name for p in offered):
		offered.insert(0, st.plan)  # a plan no longer offered still shows for the group on it
	for p in offered:
		p.problems = plans.fits(p, use)
		p.current = bool(st.plan and p.name == st.plan.name)
	return {
		"subscription": st,
		"usage": use,
		"plans": offered,
		"months": MONTHS,
		"can_pay": "settings" in me.capabilities and paystack.is_enabled(),
		"payments": frappe.get_all("Coop Subscription Payment", filters={"cooperative": cooperative, "status": "Success"},
			fields=["reference", "plan", "months", "amount", "paid_at", "period_from", "period_to"], order_by="creation desc",
			limit=24),
	}


def _check_plan(cooperative, plan):
	p = plans.get(plan)
	if not p or not (p.enabled or p.name == plans.plan_of(cooperative).name):
		frappe.throw(_("Choose one of the plans on offer"))
	problems = plans.fits(p, plans.usage(cooperative))
	if problems:
		frappe.throw(_("This plan is too small for the group right now: {0}").format("; ".join(problems)))
	return p


@frappe.whitelist(methods=["POST"])
def choose_free_plan(cooperative, plan):
	me = get_membership(cooperative, capability="settings")
	p = _check_plan(cooperative, plan)
	if flt(p.price_monthly):
		frappe.throw(_("This plan is paid for by the month"))
	frappe.db.set_value("Cooperative", cooperative, "plan", p.name)
	_platform_log(cooperative, "Plan changed", _("Chosen by {0}").format(me.full_name), plan=p.name)
	return get_subscription(cooperative)


@frappe.whitelist(methods=["POST"])
def pay(cooperative, plan, months=1):
	"""Start a Paystack checkout for months of a plan (to the platform's account, not the group's)."""
	if not paystack.is_enabled():
		frappe.throw(_("Online payments aren't switched on yet"))
	# A group paused for not paying must still be able to pay
	me = get_membership(cooperative, capability="settings", allow_paused=True)
	if frappe.db.get_value("Cooperative", cooperative, "status") not in ("Active", "Suspended"):
		frappe.throw(_("The subscription starts once Cooperative360 has approved the group"))
	months = cint(months)
	if months not in MONTHS:
		frappe.throw(_("Choose 1, 3, 6 or 12 months"))
	p = _check_plan(cooperative, plan)
	if not flt(p.price_monthly):
		frappe.throw(_("{0} is free: choose it without paying").format(p.plan_name))
	amount = flt(flt(p.price_monthly) * months, 2)
	reference = "SUB-" + frappe.generate_hash(length=16).upper()
	name = frappe.db.get_value("Cooperative", cooperative, "cooperative_name")
	data = paystack.initialize_transaction(
		email=frappe.db.get_value("User", me.user, "email"),
		amount=paystack.to_subunit(amount),
		currency="NGN",
		reference=reference,
		callback_url=_frontend_url("/pay/callback"),
		metadata={
			"kind": "subscription",
			"cooperative": cooperative,
			"plan": p.name,
			"months": months,
			"cancel_action": _frontend_url(f"/pay/callback?reference={reference}&cancelled=1"),
			"custom_fields": [
				{"display_name": "Cooperative", "variable_name": "cooperative", "value": name},
				{"display_name": "For", "variable_name": "purpose",
					"value": _("Cooperative360 {0}, {1} month(s)").format(p.plan_name, months)},
			],
		},
	)
	frappe.get_doc(
		{
			"doctype": "Coop Subscription Payment",
			"reference": reference,
			"cooperative": cooperative,
			"plan": p.name,
			"months": months,
			"amount": amount,
			"status": "Initiated",
			"paid_by": me.user,
			"access_code": data.get("access_code"),
			"authorization_url": data.get("authorization_url"),
		}
	).insert(ignore_permissions=True)
	return {"reference": reference, "access_code": data.get("access_code"),
		"authorization_url": data.get("authorization_url"), "amount": amount, "fee": 0, "total": amount}


def _summary(sp):
	return {"reference": sp.reference, "status": sp.status, "kind": "subscription", "cooperative": sp.cooperative,
		"plan": sp.plan, "months": sp.months, "amount": sp.amount, "period_to": sp.period_to, "message": sp.message}


@frappe.whitelist(methods=["POST"])
def verify(reference):
	sp = frappe.db.get_value("Coop Subscription Payment", reference, ["name", "cooperative", "status"], as_dict=True)
	if not sp:
		frappe.throw(_("Payment not found"))
	get_membership(sp.cooperative, admin=True, allow_paused=True)
	if sp.status not in ("Success", "Failed"):
		data = paystack.verify_transaction(reference)
		if data:
			complete(reference, data)
	return _summary(frappe.get_doc("Coop Subscription Payment", reference))


@frappe.whitelist(methods=["POST"])
def mark_abandoned(reference):
	sp = frappe.get_doc("Coop Subscription Payment", reference)
	if sp.paid_by != require_login():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if sp.status == "Initiated":
		sp.db_set("status", "Abandoned")
	return {"ok": True}


def complete(reference, data):
	"""Apply a verified Paystack charge for a subscription (idempotent: webhook and verify both call it)."""
	sp = frappe.get_doc("Coop Subscription Payment", reference, for_update=True)
	if sp.status == "Success":
		return sp
	sp.gateway_response = json.dumps(data, indent=1, default=str)
	sp.channel = data.get("channel")
	if data.get("status") != "success":
		if data.get("status") in ("abandoned", "failed", "reversed"):
			sp.status = "Abandoned" if data.get("status") == "abandoned" else "Failed"
			sp.message = data.get("gateway_response") or data.get("message")
		sp.save(ignore_permissions=True)
		return sp
	paid = flt(data.get("amount")) / 100
	if paid + 0.01 < flt(sp.amount) or (data.get("currency") or "NGN") != "NGN":
		sp.status = "Failed"
		sp.message = _("Paid amount {0} does not match the expected {1}").format(paid, sp.amount)
		sp.save(ignore_permissions=True)
		frappe.log_error(title="Subscription amount mismatch", message=f"{reference}: expected {sp.amount}, got {paid}")
		return sp

	# Paid months follow on from whatever is already covered (trial or earlier payments)
	st = plans.state(sp.cooperative)
	covered = getdate(st.ends_on) if st.ends_on else None
	start = add_days(covered, 1) if covered and covered >= getdate(today()) else getdate(today())
	until = add_days(add_months(start, cint(sp.months)), -1)
	sp.update({"status": "Success", "period_from": start, "period_to": until,
		"paid_at": get_datetime(data["paid_at"][:19].replace("T", " ")) if data.get("paid_at") else now_datetime()})
	sp.save(ignore_permissions=True)
	frappe.db.set_value("Cooperative", sp.cooperative, {"plan": sp.plan, "paid_until": until})
	_platform_log(sp.cooperative, "Subscription paid", _("{0} month(s), {1}").format(sp.months, money_text(sp.amount, "NGN")),
		plan=sp.plan, paid_until=until)
	notify(staff_of(sp.cooperative), _("Cooperative360 is paid until {0}").format(frappe.utils.formatdate(until, "d MMM yyyy")),
		_("{0} plan, {1} month(s).").format(sp.plan, sp.months), link="/admin/plan", cooperative=sp.cooperative,
		kind="subscription")
	return sp
