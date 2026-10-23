"""Platform console: how Cooperative360's own team runs the service.

Platform operators approve new groups, look after plans and subscriptions, and can pause a group
(for example over a complaint or unpaid subscription). They never see a group's money: nothing
here returns savings, loans, payments, members' balances or the books. What they get is who the
group is, who runs it and how to reach them, how big it is, when it was last used and where its
subscription stands. Every decision is logged with a reason, and the group is told.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_first_day, getdate, now_datetime, today

from coopvault import plans
from coopvault.api import STAFF_ROLES, is_platform_operator
from coopvault.api.coop import _platform_log
from coopvault.notify import members_of, notify, staff_of

GROUP_FIELDS = ["name", "cooperative_name", "status", "description", "location", "registration_number",
	"expected_members", "plan", "trial_ends_on", "paid_until", "status_reason", "status_changed_on", "approved_on",
	"creation", "created_by_user"]
PLAN_FIELDS = ["plan_name", "enabled", "is_default", "description", "price_monthly", "trial_days", "max_members",
	"max_loan_products", "max_savings_products", "online_payments", "dedicated_accounts", "autosave"]
STATUSES = ("Pending review", "Active", "Suspended", "Declined", "Inactive")


def _require():
	if not is_platform_operator():
		frappe.throw(_("Only the Cooperative360 platform team can do this"), frappe.PermissionError)
	return frappe.session.user


def _note(text, what):
	text = (text or "").strip()
	if len(text) < 5:
		frappe.throw(_("Say why {0}. The group's admins see this.").format(what))
	return text


def _member_count(cooperative):
	return frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Active"})


def _last_active(cooperative):
	"""When anyone in the group last signed in (a sign of life that says nothing about money)."""
	value = frappe.db.sql(
		"""select max(u.last_active) from `tabCoop Member` m join `tabUser` u on u.name = m.user
		where m.cooperative = %s""",
		cooperative,
	)
	return value[0][0] if value else None


def _row(c):
	st = plans.state(c.name)
	return {
		**{f: c.get(f) for f in ("name", "cooperative_name", "status", "location", "plan", "creation", "approved_on",
			"expected_members", "status_reason")},
		"members": _member_count(c.name),
		"subscription": st.state,
		"ends_on": st.ends_on,
		"founder": frappe.db.get_value("User", c.created_by_user, "full_name") if c.created_by_user else None,
	}


def _tell_group(cooperative, title, body=None, everyone=False):
	users = members_of(cooperative) if everyone else staff_of(cooperative)
	notify(users, title, body, link="/home", cooperative=cooperative, kind="platform")


# Reading -----------------------------------------------------------------------------------------


@frappe.whitelist()
def overview():
	_require()
	groups = frappe.get_all("Cooperative", fields=GROUP_FIELDS, order_by="creation asc")
	by_status = {s: 0 for s in STATUSES}
	subs = {}
	attention = []
	for c in groups:
		by_status[c.status] = by_status.get(c.status, 0) + 1
		if c.status == "Active":
			st = plans.state(c.name)
			subs[st.state] = subs.get(st.state, 0) + 1
			if st.state in ("grace", "lapsed") or (st.state == "trial" and cint(st.days_left) <= 7):
				attention.append(_row(c))
	month = get_first_day(today())
	revenue = frappe.db.sql(
		"""select coalesce(sum(amount), 0), count(*) from `tabCoop Subscription Payment`
		where status = 'Success' and paid_at >= %s""",
		month,
	)[0]
	actions = frappe.get_all("Coop Platform Action", fields=["cooperative", "action", "note", "done_by", "creation"],
		order_by="creation desc", limit=12)
	names = {c.name: c.cooperative_name for c in groups}
	for a in actions:
		a.cooperative_name = names.get(a.cooperative)
		a.done_by_name = frappe.db.get_value("User", a.done_by, "full_name")
	return {
		"by_status": by_status,
		"subscriptions": subs,
		"members": frappe.db.count("Coop Member", {"status": "Active"}),
		"groups": len(groups),
		"revenue_this_month": flt(revenue[0], 2),
		"payments_this_month": cint(revenue[1]),
		"pending": [_row(c) for c in groups if c.status == "Pending review"],
		"attention": attention,
		"recent": actions,
	}


@frappe.whitelist()
def list_groups(status=None, q=None):
	_require()
	filters = {"status": status} if status else {}
	rows = frappe.get_all("Cooperative", filters=filters, fields=GROUP_FIELDS, order_by="creation desc")
	q = (q or "").strip().lower()
	if q:
		rows = [c for c in rows if q in f"{c.cooperative_name} {c.location or ''} {c.name}".lower()]
	return [_row(c) for c in rows]


@frappe.whitelist()
def get_group(name):
	_require()
	c = frappe.db.get_value("Cooperative", name, GROUP_FIELDS, as_dict=True)
	if not c:
		frappe.throw(_("Cooperative not found"), frappe.DoesNotExistError)
	st = plans.state(name)
	use = plans.usage(name)
	officers = frappe.get_all(
		"Coop Member",
		filters={"cooperative": name, "role": ["in", STAFF_ROLES], "status": "Active"},
		fields=["full_name", "role", "phone", "user"],
		order_by="role asc",
	)
	for o in officers:
		email = frappe.db.get_value("User", o.pop("user"), "email") or ""
		o.email = None if email.endswith(".invalid") else email  # members added by hand have no real email
	payments = frappe.get_all("Coop Subscription Payment", filters={"cooperative": name, "status": ["!=", "Initiated"]},
		fields=["reference", "plan", "months", "amount", "status", "paid_at", "period_from", "period_to", "paid_by"],
		order_by="creation desc", limit=24)
	for p in payments:
		p.paid_by_name = frappe.db.get_value("User", p.paid_by, "full_name") if p.paid_by else None
	actions = frappe.get_all("Coop Platform Action", filters={"cooperative": name},
		fields=["action", "note", "done_by", "plan", "paid_until", "creation"], order_by="creation desc", limit=50)
	for a in actions:
		a.done_by_name = frappe.db.get_value("User", a.done_by, "full_name")
	return {
		**c,
		"founder": frappe.db.get_value("User", c.created_by_user, ["full_name", "mobile_no"], as_dict=True)
			if c.created_by_user else None,
		"members": _member_count(name),
		"last_active": _last_active(name),
		"officers": officers,
		"subscription": st,
		"usage": use,
		"plan_problems": plans.fits(st.plan, use) if st.plan else [],
		"payments": payments,
		"actions": actions,
		"plans": [p.plan_name for p in frappe.get_all("Coop Plan", filters={"enabled": 1}, fields=["plan_name"],
			order_by="price_monthly asc")],
	}


# Decisions ---------------------------------------------------------------------------------------


def _set_status(name, status, reason=None):
	frappe.db.set_value("Cooperative", name, {"status": status, "status_reason": reason,
		"status_changed_on": now_datetime(), "status_changed_by": frappe.session.user})


def _load(name, *allowed):
	c = frappe.db.get_value("Cooperative", name, ["name", "cooperative_name", "status"], as_dict=True, for_update=True)
	if not c:
		frappe.throw(_("Cooperative not found"), frappe.DoesNotExistError)
	if allowed and c.status not in allowed:
		frappe.throw(_("{0} is {1}, so that can't be done now").format(c.cooperative_name, _(c.status).lower()))
	return c


@frappe.whitelist(methods=["POST"])
def review_group(name, approve, note=None):
	"""Approve a new group (its trial starts and members can join) or decline it with a reason."""
	_require()
	c = _load(name, "Pending review")
	if cint(approve):
		_set_status(name, "Active")
		frappe.db.set_value("Cooperative", name, "approved_on", now_datetime())
		plans.start_trial(name)
		st = plans.state(name)
		_platform_log(name, "Approved", (note or "").strip() or None, plan=st.plan.name if st.plan else None)
		trial = _(" Your {0}-day free trial has started.").format(cint(st.plan.trial_days)) if st.state == "trial" else ""
		_tell_group(name, _("{0} is approved").format(c.cooperative_name),
			_("You can now invite members and take payments.") + trial)
	else:
		reason = _note(note, _("it was declined"))
		_set_status(name, "Declined", reason)
		_platform_log(name, "Declined", reason)
		_tell_group(name, _("{0} wasn't approved").format(c.cooperative_name), reason)
	return get_group(name)


@frappe.whitelist(methods=["POST"])
def suspend_group(name, reason):
	"""Pause a group: everyone can still sign in and see their records, but nothing can change."""
	_require()
	c = _load(name, "Active")
	reason = _note(reason, _("the group is being paused"))
	_set_status(name, "Suspended", reason)
	_platform_log(name, "Suspended", reason)
	_tell_group(name, _("{0} is paused by Cooperative360").format(c.cooperative_name),
		_("Your savings and loans are safe and you can still see them. Nothing can change until it's resumed. Reason: {0}").format(reason),
		everyone=True)
	return get_group(name)


@frappe.whitelist(methods=["POST"])
def reinstate_group(name, note=None):
	_require()
	c = _load(name, "Suspended", "Declined")
	was = c.status
	_set_status(name, "Active")
	if was == "Declined":
		frappe.db.set_value("Cooperative", name, "approved_on", now_datetime())
		plans.start_trial(name)
	_platform_log(name, "Reinstated", (note or "").strip() or None)
	_tell_group(name, _("{0} is open again").format(c.cooperative_name), (note or "").strip() or None, everyone=was == "Suspended")
	return get_group(name)


@frappe.whitelist(methods=["POST"])
def change_plan(name, plan, note=None):
	"""Move a group to another plan (e.g. after a call). Limits apply to what's added from now on."""
	_require()
	_load(name)
	p = plans.get(plan)
	if not p or not p.enabled:
		frappe.throw(_("Choose an available plan"))
	frappe.db.set_value("Cooperative", name, "plan", p.name)
	_platform_log(name, "Plan changed", (note or "").strip() or None, plan=p.name)
	_tell_group(name, _("Your Cooperative360 plan is now {0}").format(p.plan_name), (note or "").strip() or None)
	return get_group(name)


@frappe.whitelist(methods=["POST"])
def extend_subscription(name, days, note):
	"""Give a group more time without payment (a goodwill extension or a payment made another way)."""
	_require()
	_load(name, "Active", "Suspended")
	days = cint(days)
	if not 1 <= days <= 366:
		frappe.throw(_("Choose between 1 and 366 days"))
	note = _note(note, _("you're extending it"))
	st = plans.state(name)
	start = max(getdate(today()), getdate(st.ends_on)) if st.ends_on else getdate(today())
	until = add_days(start, days)
	frappe.db.set_value("Cooperative", name, "paid_until", until)
	_platform_log(name, "Subscription extended", note, paid_until=until)
	_tell_group(name, _("Your Cooperative360 subscription runs until {0}").format(frappe.utils.formatdate(until, "d MMM yyyy")),
		note)
	return get_group(name)


# Plans -------------------------------------------------------------------------------------------


@frappe.whitelist()
def list_plans():
	_require()
	rows = frappe.get_all("Coop Plan", fields=["name"] + PLAN_FIELDS, order_by="price_monthly asc")
	for p in rows:
		p.groups = frappe.db.count("Cooperative", {"plan": p.name, "status": ["in", ["Active", "Suspended"]]})
	return rows


@frappe.whitelist(methods=["POST"])
def save_plan(plan_name, name=None, **values):
	_require()
	doc = frappe.get_doc("Coop Plan", name) if name else frappe.new_doc("Coop Plan")
	if not name:
		doc.plan_name = plan_name
	for field in PLAN_FIELDS:
		if field in values and field != "plan_name":
			doc.set(field, values[field])
	# Switching a plan off only stops offering it: groups already on it keep it
	doc.save(ignore_permissions=True)
	if not frappe.db.exists("Coop Plan", {"is_default": 1, "enabled": 1}):
		frappe.throw(_("Keep one plan as the one new groups are given"))
	return list_plans()
