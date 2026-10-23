"""Subscription plans and what they allow.

Cooperative360 is sold per group. A plan sets limits (members, loan products, savings products)
and switches features on (online payments, personal account numbers, Autosave). New groups get
the default plan's free trial once a platform operator approves them; after that an admin pays
for months in advance through Paystack. That money is the platform's, so it never touches the
group's books.

When a subscription runs out (after a few days' grace) the group keeps working for money already
in motion: payments, repayments and withdrawals go on, because members' money must never be
trapped. What pauses is growth: no new loans and no new members until it's renewed.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, getdate, today

# name, monthly price, trial days, members, loan products, savings products, online, account numbers, autosave
DEFAULT_PLANS = [
	("Starter", 0, 0, 30, 1, 1, 1, 0, 0, "For small groups getting started: regular contributions and one loan product."),
	("Growth", 15000, 30, 200, 5, 5, 1, 1, 1, "Voluntary savings, several loan products, account numbers and Autosave."),
	("Enterprise", 50000, 30, 0, 0, 0, 1, 1, 1, "No limits, for large and multi-branch cooperatives."),
]
FIELDS = ["name", "plan_name", "enabled", "is_default", "description", "price_monthly", "trial_days", "max_members",
	"max_loan_products", "max_savings_products", "online_payments", "dedicated_accounts", "autosave"]
FEATURES = {
	"online_payments": _("Online payments"),
	"dedicated_accounts": _("Personal account numbers"),
	"autosave": _("Autosave"),
}
COUNTED_MEMBERS = ("Pending", "Active", "Suspended")


def ensure_default_plans():
	if frappe.db.count("Coop Plan"):
		return
	for i, (name, price, trial, members, loans, savings, online, dva, autosave, desc) in enumerate(DEFAULT_PLANS):
		frappe.get_doc(
			{
				"doctype": "Coop Plan",
				"plan_name": name,
				"enabled": 1,
				"is_default": 1 if name == "Growth" else 0,
				"description": desc,
				"price_monthly": price,
				"trial_days": trial,
				"max_members": members,
				"max_loan_products": loans,
				"max_savings_products": savings,
				"online_payments": online,
				"dedicated_accounts": dva,
				"autosave": autosave,
			}
		).insert(ignore_permissions=True)


def default_plan():
	return frappe.db.get_value("Coop Plan", {"is_default": 1, "enabled": 1}) or frappe.db.get_value(
		"Coop Plan", {"enabled": 1}, order_by="price_monthly asc")


def get(name):
	return frappe.db.get_value("Coop Plan", name, FIELDS, as_dict=True) if name else None


def plan_of(cooperative):
	"""The group's plan; a group without one is treated as on the default plan."""
	return get(frappe.db.get_value("Cooperative", cooperative, "plan") or default_plan())


def validate_plan(doc):
	doc.plan_name = (doc.plan_name or "").strip()
	for field in ("price_monthly", "trial_days", "max_members", "max_loan_products", "max_savings_products"):
		if flt(doc.get(field)) < 0:
			frappe.throw(_("Limits and prices can't be negative"))
	if doc.is_default:
		frappe.db.set_value("Coop Plan", {"is_default": 1, "name": ["!=", doc.name or ""]}, "is_default", 0)
		doc.enabled = 1


# Subscription state ---------------------------------------------------------------------------


def grace_days():
	return cint(frappe.db.get_single_value("CoopVault Settings", "subscription_grace_days") or 7)


def state(cooperative):
	"""Where a group's subscription stands.

	trial: in the free trial · active: paid up · grace: ended, still in the grace days ·
	lapsed: past the grace days (no new loans or members) · free: on a free plan · review: not yet
	approved (nothing has started).
	"""
	coop = frappe.db.get_value("Cooperative", cooperative, ["status", "plan", "trial_ends_on", "paid_until"], as_dict=True)
	plan = plan_of(cooperative)
	now = getdate(today())
	out = frappe._dict(plan=plan, trial_ends_on=coop.trial_ends_on, paid_until=coop.paid_until, grace_days=grace_days())
	dates = [getdate(d) for d in (coop.trial_ends_on, coop.paid_until) if d]
	ends = out.ends_on = max(dates) if dates else None
	if coop.status == "Pending review":
		out.state = "review"
	elif plan and not flt(plan.price_monthly):
		out.state = "free"
	elif ends and ends >= now:
		out.state = "active" if coop.paid_until and getdate(coop.paid_until) >= now else "trial"
		out.days_left = date_diff(ends, now)
	elif ends and date_diff(now, ends) <= out.grace_days:
		out.state = "grace"
		out.days_left = out.grace_days - date_diff(now, ends)
	else:
		out.state = "lapsed"
	out.limited = out.state == "lapsed"
	return out


def start_trial(cooperative):
	"""Called when a group is approved: put it on the default plan and start the trial."""
	coop = frappe.db.get_value("Cooperative", cooperative, ["plan", "trial_ends_on"], as_dict=True)
	plan = get(coop.plan or default_plan())
	values = {"plan": plan.name if plan else None}
	if plan and not coop.trial_ends_on and cint(plan.trial_days):
		values["trial_ends_on"] = add_days(today(), cint(plan.trial_days))
	frappe.db.set_value("Cooperative", cooperative, values)


# Usage and limits -------------------------------------------------------------------------------


def usage(cooperative):
	return frappe._dict(
		members=frappe.db.count("Coop Member", {"cooperative": cooperative, "status": ["in", COUNTED_MEMBERS]}),
		loan_products=frappe.db.count("Coop Loan Product", {"cooperative": cooperative, "enabled": 1}),
		savings_products=frappe.db.count("Coop Savings Product", {"cooperative": cooperative, "enabled": 1}),
	)


def fits(plan, use):
	"""Reasons the group's current usage doesn't fit a plan (empty when it fits)."""
	out = []
	for key, limit in (("members", "max_members"), ("loan_products", "max_loan_products"),
			("savings_products", "max_savings_products")):
		n = cint(plan.get(limit))
		if n and use[key] > n:
			out.append(_("{0} allows {1}; the group has {2}").format(plan.plan_name, _count(n, key), use[key]))
	return out


def _count(n, key):
	one, many = {"members": (_("member"), _("members")), "loan_products": (_("loan product"), _("loan products")),
		"savings_products": (_("savings product"), _("savings products"))}[key]
	return f"{n} {one if n == 1 else many}"


def _upgrade_hint():
	return _("An admin can change the plan in Manage → Settings → Plan.")


def check_members(cooperative, adding=1):
	plan = plan_of(cooperative)
	if plan and cint(plan.max_members):
		count = usage(cooperative).members
		if count + adding > cint(plan.max_members):
			frappe.throw(_("{0} allows {1} and the group has {2}. {3}").format(
				plan.plan_name, _count(cint(plan.max_members), "members"), count, _upgrade_hint()))
	check_not_lapsed(cooperative, _("New members can join once the subscription is renewed."))


def check_products(cooperative, doctype, enabling=True):
	if not enabling:
		return
	plan = plan_of(cooperative)
	field, key = ("max_loan_products", "loan_products") if doctype == "Coop Loan Product" else (
		"max_savings_products", "savings_products")
	limit = cint(plan.get(field)) if plan else 0
	if limit and frappe.db.count(doctype, {"cooperative": cooperative, "enabled": 1}) >= limit:
		frappe.throw(_("{0} allows {1} switched on. {2}").format(plan.plan_name, _count(limit, key), _upgrade_hint()))


def has(cooperative, feature):
	plan = plan_of(cooperative)
	return bool(plan and cint(plan.get(feature)))


def check_feature(cooperative, feature):
	if not has(cooperative, feature):
		frappe.throw(_("{0} isn't included in the group's plan. {1}").format(FEATURES[feature], _upgrade_hint()))


def check_not_lapsed(cooperative, what=None):
	st = state(cooperative)
	if st.limited:
		frappe.throw(_("The group's Cooperative360 subscription ended on {0}. {1}").format(
			frappe.utils.formatdate(st.ends_on, "d MMM yyyy"), what or _("Ask an admin to renew it.")))
