import hashlib
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, add_to_date, cint, flt, getdate, now_datetime, today

from coopvault import approvals, paystack, savings
from coopvault.api import CAPABILITIES, ROLES, can, ensure_not_self, get_membership, is_super_user, require, require_login
from coopvault.ledger import (
	TRACKED,
	contribution_standing,
	cooperative_totals,
	first_full_period,
	loan_schedule,
	member_balances,
	period_start,
	plan_on,
	plans_for,
	refresh_all,
	shift_period,
)
from coopvault.notify import members_of, notify, staff_of
from coopvault.utils import mask_account, money_text, normalize_phone, require_password

COOP_PUBLIC_FIELDS = [
	"name",
	"cooperative_name",
	"description",
	"currency",
	"status",
	"contribution_amount",
	"contribution_frequency",
	"start_date",
	"loan_multiplier",
	"interest_rate",
	"max_loan_tenure_months",
	"bank_name",
	"account_number",
	"account_name",
	"account_verified",
	"settlement_bank_code",
	"bank_changed_on",
	"accept_online_payments",
	"fee_bearer",
	"auto_allocate",
	"extra_savings_product",
]
COOP_RULE_FIELDS = ["cooperative_name", "description", "loan_multiplier", "interest_rate", "max_loan_tenure_months",
	"accept_online_payments", "fee_bearer", "auto_allocate", "extra_savings_product"]
RESET_CODE_MINUTES = 30
SETUP_CODE_DAYS = 7
PLACEHOLDER_EMAIL_DOMAIN = "members.invalid"  # reserved TLD: never delivers anywhere
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _membership_summary(member_name):
	m = frappe.db.get_value(
		"Coop Member", member_name, ["name", "cooperative", "role", "status", "joined_on", "member_number"], as_dict=True
	)
	m.capabilities = [c for c in CAPABILITIES if can(m, c)]
	coop = frappe.db.get_value(
		"Cooperative", m.cooperative, ["cooperative_name", "currency", "member_count"], as_dict=True
	)
	m.update(coop)
	if m.status in ("Active", "Left", "Suspended"):
		m.update(member_balances(member_name))
	return m


def _payout_defaults(user):
	"""A new membership inherits the payout account the user already set elsewhere."""
	fields = ["payout_bank_code", "payout_bank_name", "payout_account_number", "payout_account_name", "payout_changed_on"]
	row = frappe.db.get_value(
		"Coop Member", {"user": user, "payout_account_number": ["is", "set"]}, fields, as_dict=True
	)
	return dict(row) if row else {}


def _announce(cooperative, title, message):
	frappe.get_doc(
		{
			"doctype": "Coop Announcement",
			"cooperative": cooperative,
			"title": title,
			"message": message,
			"pinned": 1,
			"is_system": 1,
			"posted_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)


@frappe.whitelist()
def my_cooperatives():
	user = require_login()
	members = frappe.get_all(
		"Coop Member",
		filters={"user": user, "status": ["in", ["Active", "Pending", "Suspended", "Left"]]},
		fields=["name", "status", "total_contributed"],
		order_by="creation asc",
	)
	# A group someone left only shows while it still holds money of theirs
	return [_membership_summary(m.name) for m in members if m.status != "Left" or flt(m.total_contributed) > 0.005]


@frappe.whitelist(methods=["POST"])
def create_cooperative(cooperative_name, **kwargs):
	user = require_login()
	if not (
		frappe.db.get_single_value("CoopVault Settings", "allow_create_cooperative") or is_super_user()
	):
		frappe.throw(_("Creating cooperatives is restricted. Contact the platform admin."))

	coop = frappe.new_doc("Cooperative")
	coop.cooperative_name = cooperative_name
	for field in COOP_RULE_FIELDS + ["contribution_amount", "contribution_frequency", "start_date"]:
		if field in kwargs and kwargs[field] not in (None, ""):
			coop.set(field, kwargs[field])
	coop.created_by_user = user
	coop.insert(ignore_permissions=True)

	profile = frappe.db.get_value("User", user, ["full_name", "mobile_no"], as_dict=True)
	frappe.get_doc(
		{
			"doctype": "Coop Member",
			"cooperative": coop.name,
			"user": user,
			"full_name": profile.full_name,
			"phone": profile.mobile_no,
			"role": "Admin",
			"status": "Active",
			"joined_on": today(),
			"member_number": next_member_number(coop.name),
			**_payout_defaults(user),
		}
	).insert(ignore_permissions=True)
	return {"name": coop.name, "join_code": coop.join_code}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=60 * 10)
def preview_invite(join_code):
	"""What an invite link points to, so new members know what they are joining."""
	coop = frappe.db.get_value(
		"Cooperative",
		{"join_code": (join_code or "").strip().upper(), "status": "Active"},
		["cooperative_name", "description", "member_count", "contribution_amount", "contribution_frequency",
			"currency"],
		as_dict=True,
	)
	if not coop:
		frappe.throw(_("This invite code isn't valid any more. Ask your admin for a new link."))
	return coop


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60 * 10)
def join_cooperative(join_code):
	user = require_login()
	coop = frappe.db.get_value(
		"Cooperative", {"join_code": (join_code or "").strip().upper()}, ["name", "status"], as_dict=True
	)
	if not coop:
		frappe.throw(_("No cooperative found with that join code"))
	if coop.status != "Active":
		frappe.throw(_("This cooperative is not accepting members"))

	existing = frappe.db.get_value(
		"Coop Member", {"cooperative": coop.name, "user": user}, ["name", "status"], as_dict=True
	)
	full_name = frappe.db.get_value("User", user, "full_name")
	if existing:
		if existing.status == "Left":
			frappe.db.set_value("Coop Member", existing.name, "status", "Pending")
			_request_admission(coop.name, existing.name, full_name)
			return {"name": coop.name, "status": "Pending"}
		return {"name": coop.name, "status": existing.status}

	profile = frappe.db.get_value("User", user, ["full_name", "mobile_no"], as_dict=True)
	frappe.get_doc(
		{
			"doctype": "Coop Member",
			"cooperative": coop.name,
			"user": user,
			"full_name": profile.full_name,
			"phone": profile.mobile_no,
			"role": "Member",
			"status": "Pending",
			**_payout_defaults(user),
		}
	).insert(ignore_permissions=True)
	_request_admission(coop.name, frappe.db.get_value("Coop Member", {"cooperative": coop.name, "user": user}), full_name)
	return {"name": coop.name, "status": "Pending"}


def _request_admission(cooperative, member, full_name):
	"""Joining goes through the group's approval rule for new members."""
	approvals.request(cooperative, "Member admission", ("Coop Member", member),
		summary=_("{0} wants to join").format(full_name))


def on_admission_decided(approval, approved, note=None):
	m = frappe.get_doc("Coop Member", approval.reference_name)
	if m.status != "Pending":
		return
	coop_name = frappe.db.get_value("Cooperative", m.cooperative, "cooperative_name")
	if approved:
		m.status = "Active"
		if not m.member_number:
			m.member_number = next_member_number(m.cooperative)
		m.save(ignore_permissions=True)
		notify(m.user, _("You're in! Welcome to {0}").format(coop_name),
			_("Your member number is {0}. You can start saving now.").format(m.member_number),
			link="/home", cooperative=m.cooperative, kind="join-approved")
	else:
		m.status = "Left"
		m.save(ignore_permissions=True)
		notify(m.user, _("Your request to join {0} was declined").format(coop_name), note,
			cooperative=m.cooperative, kind="join-declined")


def next_member_number(cooperative):
	"""Members are numbered in the order they're admitted: initials of the group and a counter, e.g. USC-0007."""
	from frappe.model.naming import getseries

	name = frappe.db.get_value("Cooperative", cooperative, "cooperative_name") or "C"
	initials = "".join(w[0] for w in name.split() if w and w[0].isalnum())[:4].upper() or "C"
	return f"{initials}-{getseries(f'CMN-{cooperative}-', 4)}"


def _loans_with_schedule(filters):
	loans = frappe.get_all(
		"Coop Loan",
		filters=filters,
		fields=["name", "status", "principal", "total_payable", "amount_repaid", "balance", "due_date",
			"disbursed_on", "received_on", "tenure_months", "purpose", "application_date", "transfer_reference",
			"payout_bank_name", "payout_account_number", "receipt_disputed"],
		order_by="creation desc",
	)
	for loan in loans:
		schedule = loan_schedule(loan)
		if schedule:
			schedule.pop("rows")
		loan.schedule = schedule
	return loans


def _plan_info(cooperative):
	"""The plan in force and any change scheduled for a later date."""
	plans = plans_for(cooperative)
	now = getdate(today())
	current = plan_on(plans, now)
	upcoming = next((p for p in plans if p.effective_from > now), None)
	return {"current": current, "upcoming": upcoming, "started": bool(_has_contributions(cooperative))}


def _public_coop(cooperative, membership):
	coop = frappe.db.get_value("Cooperative", cooperative, COOP_PUBLIC_FIELDS + ["join_code"], as_dict=True)
	if not membership.is_admin:
		coop.pop("join_code")
		coop.account_number = mask_account(coop.account_number)
	return coop


@frappe.whitelist()
def get_dashboard(cooperative):
	from coopvault.api.payments import autosave_status, config, dedicated_account_status

	membership = get_membership(cooperative)
	data = {
		"cooperative": _public_coop(cooperative, membership),
		"plan": _plan_info(cooperative),
		"membership": membership,
		"pool": cooperative_totals(cooperative),
		"payments": config(cooperative),
		"announcements": frappe.get_all(
			"Coop Announcement",
			filters={"cooperative": cooperative},
			fields=["name", "title", "message", "pinned", "is_system", "posted_by", "creation"],
			order_by="pinned desc, creation desc",
			limit=5,
		),
		"unread_notifications": frappe.db.count("Coop Notification", {"user": frappe.session.user, "read": 0}),
		"savings_products": [
			{k: p.get(k) for k in ["name", "product_name", "kind", "description", "withdrawal_rule", "counts_for_borrowing"]}
			for p in savings.products(cooperative, enabled_only=True)
		],
		"next_meeting": _next_meeting(cooperative, membership),
	}

	if membership.name:
		balances = member_balances(membership.name)
		data["balances"] = balances
		data["standing"] = contribution_standing(membership.name, balances.dues_paid)
		data["active_loans"] = _loans_with_schedule(
			{"member": membership.name, "status": ["in", ["Pending", "Approved", "Sent", "Active"]]}
		)
		data["withdrawals"] = frappe.get_all(
			"Coop Withdrawal",
			filters={"member": membership.name, "status": ["in", ["Pending", "Approved", "Sent"]]},
			fields=["name", "amount", "status", "requested_on", "sent_on", "transfer_reference", "payout_bank_name",
				"payout_account_number", "receipt_disputed"],
			order_by="creation desc",
		)
		data["opening_balances"] = frappe.get_all(
			"Coop Contribution",
			filters={"member": membership.name, "status": "Pending", "payment_method": "Opening Balance"},
			fields=["name", "amount", "contribution_date", "notes", "proposed_by"],
		)
		data["recent"] = get_statement(cooperative, limit=6)["entries"]
		data["autosave"] = autosave_status(membership.name)
		data["dedicated_account"] = dedicated_account_status(membership.name)
		data["security_hold_until"] = frappe.db.get_value("Coop Member", membership.name, "security_hold_until")
		# What one payment would cover right now (dues, then loan instalments): the Pay screen
		data["due"] = savings.due_lines(membership.name)
		data["guarantee_requests"] = frappe.db.sql(
			"""select l.name as loan, l.member_name as borrower, l.principal, g.amount
			from `tabCoop Loan Guarantor` g join `tabCoop Loan` l on l.name = g.parent
			where g.member = %s and g.status = 'Pending' and l.status in ('Pending', 'Approved')
			order by l.creation desc""",
			membership.name,
			as_dict=True,
		)

	if membership.is_admin:
		data["period"] = period_summary(cooperative)
		data["pending"] = pending_counts(cooperative, membership)
	return data


def _next_meeting(cooperative, membership):
	from coopvault.api.meetings import next_meeting

	return next_meeting(cooperative, membership)


def pending_counts(cooperative, me):
	"""What's waiting on this officer: approvals they can decide, money to send, transfers to fix or match."""
	counts = {"approvals": sum(1 for a in approvals.pending(cooperative, me.user) if a["can_decide"])}
	if can(me, "pay_out"):
		counts["to_send"] = frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Approved"}) + frappe.db.count(
			"Coop Withdrawal", {"cooperative": cooperative, "status": "Approved"})
		counts["disputes"] = frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1}) \
			+ frappe.db.count("Coop Withdrawal", {"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1})
	if can(me, "record"):
		counts["unmatched"] = frappe.db.count("Coop Payment Transaction", {"cooperative": cooperative, "status": "Unmatched"})
	return counts


def _has_contributions(cooperative):
	return frappe.db.exists("Coop Contribution", {"cooperative": cooperative, "status": "Confirmed"})


@frappe.whitelist(methods=["POST"])
def update_cooperative(cooperative, **kwargs):
	"""Rules and details (Admins only). A new contribution amount or frequency takes effect from the
	start of the next period, so nobody is suddenly behind for months they already paid."""
	get_membership(cooperative, owner=True)
	coop = frappe.get_doc("Cooperative", cooperative)
	for field in COOP_RULE_FIELDS:
		if field in kwargs:
			coop.set(field, kwargs[field])

	change = None
	amount = flt(kwargs.get("contribution_amount", coop.contribution_amount))
	frequency = kwargs.get("contribution_frequency") or coop.contribution_frequency
	started = _has_contributions(cooperative)
	if "start_date" in kwargs and (kwargs["start_date"] or None) != (str(coop.start_date) if coop.start_date else None):
		if started:
			frappe.throw(_("The start date can't change once members have contributed"))
		coop.start_date = kwargs["start_date"] or None

	latest = coop.contribution_plans[-1] if coop.contribution_plans else None
	if not latest or flt(latest.amount) != amount or latest.frequency != frequency:
		if not started:
			coop.contribution_plans = []
			coop.contribution_amount, coop.contribution_frequency = amount, frequency
		else:
			effective = (
				first_full_period(add_days(today(), 1), frequency) if frequency in TRACKED else getdate(add_days(today(), 1))
			)
			# Replace a change that hasn't started yet instead of stacking several
			coop.contribution_plans = [r for r in coop.contribution_plans if getdate(r.effective_from) <= getdate(today())]
			coop.append(
				"contribution_plans",
				{"effective_from": effective, "amount": amount, "frequency": frequency, "changed_by": frappe.session.user},
			)
			change = {"effective_from": effective, "amount": amount, "frequency": frequency}
	coop.save(ignore_permissions=True)

	if change:
		every = {"Weekly": _("a week"), "Monthly": _("a month"), "Quarterly": _("a quarter")}.get(frequency)
		text = (
			_("From {0}, each member contributes {1} {2}. Earlier periods keep the old amount.").format(
				frappe.utils.formatdate(change["effective_from"], "d MMMM yyyy"),
				money_text(amount, coop.currency),
				every,
			)
			if every
			else _("From {0}, contributions are flexible.").format(frappe.utils.formatdate(change["effective_from"], "d MMMM yyyy"))
		)
		_announce(cooperative, _("Contribution change"), text)
		notify(members_of(cooperative), _("Contribution change"), text, link="/home", cooperative=cooperative, kind="plan")

	refresh_all(cooperative=cooperative)
	for m in frappe.get_all("Coop Member", filters={"cooperative": cooperative}, pluck="name"):
		refresh_all(member=m)
	return {"cooperative": get_dashboard(cooperative)["cooperative"], "plan_change": change}


@frappe.whitelist(methods=["POST"])
def regenerate_join_code(cooperative):
	from coopvault.coopvault.doctype.cooperative.cooperative import generate_join_code

	get_membership(cooperative, owner=True)
	code = generate_join_code()
	frappe.db.set_value("Cooperative", cooperative, "join_code", code)
	return {"join_code": code}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 10)
def set_bank_account(cooperative, bank_code, account_number, password=None):
	"""Verify the cooperative's bank account with Paystack and settle online payments into it.
	Admins only, password required, and every member is told."""
	get_membership(cooperative, owner=True)
	require_password(password)
	if not paystack.is_enabled():
		frappe.throw(_("Online payments aren't set up yet"))
	account_name = paystack.resolve_account(account_number, bank_code)
	coop = frappe.get_doc("Cooperative", cooperative)
	had_account = bool(coop.account_number)
	coop.update(
		{
			"settlement_bank_code": bank_code,
			"bank_name": paystack.bank_name(bank_code, coop.currency) or coop.bank_name,
			"account_number": account_number.strip(),
			"account_name": account_name,
			"account_verified": 1,
			"bank_changed_on": now_datetime(),
			"bank_changed_by": frappe.session.user,
		}
	)
	coop.paystack_subaccount_code = paystack.upsert_subaccount(coop)
	coop.save(ignore_permissions=True)

	who = frappe.db.get_value("User", frappe.session.user, "full_name")
	text = _("{0} set the cooperative's settlement account to {1} {2} ({3}). Online payments now go there.").format(
		who, coop.bank_name, mask_account(coop.account_number), account_name
	)
	if had_account:
		_announce(cooperative, _("Settlement account changed"), text)
		notify(members_of(cooperative), _("Settlement account changed"), text, link="/announcements",
			cooperative=cooperative, kind="bank-changed")
	else:
		notify(staff_of(cooperative), _("Settlement account connected"), text, cooperative=cooperative, kind="bank-changed")
	return get_dashboard(cooperative)["cooperative"]


def period_summary(cooperative):
	"""Collection progress for the current contribution period."""
	plan = plan_on(plans_for(cooperative), today())
	if plan.frequency not in TRACKED or not flt(plan.amount):
		return None
	start = period_start(today(), plan.frequency)
	end = add_days(shift_period(start, plan.frequency), -1)
	members = frappe.get_all("Coop Member", filters={"cooperative": cooperative, "status": "Active"}, pluck="name")
	counts = {"up_to_date": 0, "due": 0, "behind": 0, "not_started": 0}
	for m in members:
		state = contribution_standing(m).get("state")
		if state in counts:
			counts[state] += 1
	collected = frappe.get_all(
		"Coop Contribution",
		filters={"cooperative": cooperative, "status": "Confirmed", "contribution_date": ["between", [start, end]]},
		fields=[{"SUM": "amount", "as": "total"}],
	)
	return {
		"frequency": plan.frequency,
		"start": start,
		"end": end,
		"expected": flt(plan.amount) * len(members),
		"collected": flt(collected[0].total) if collected else 0.0,
		"members": len(members),
		**counts,
	}


@frappe.whitelist()
def contribution_tracker(cooperative):
	"""Who is up to date, who still owes this period and who is behind (admins and treasurer)."""
	get_membership(cooperative, admin=True)
	rows = []
	for m in frappe.get_all(
		"Coop Member",
		filters={"cooperative": cooperative, "status": "Active"},
		fields=["name", "full_name", "phone", "role", "joined_on"],
	):
		standing = contribution_standing(m.name)
		last = frappe.get_all(
			"Coop Contribution",
			filters={"member": m.name, "status": "Confirmed"},
			fields=["contribution_date", "amount"],
			order_by="contribution_date desc, creation desc",
			limit=1,
		)
		rows.append({**m, "standing": standing, "last_payment": last[0] if last else None})
	order = {"behind": 0, "due": 1, "up_to_date": 2, "not_started": 3}
	rows.sort(key=lambda r: (order.get(r["standing"].get("state"), 4), -flt(r["standing"].get("due_now"))))
	return {"period": period_summary(cooperative), "members": rows}


@frappe.whitelist()
def list_members(cooperative, status=None):
	membership = get_membership(cooperative)
	filters = {"cooperative": cooperative}
	if not membership.is_admin:
		filters["status"] = "Active"
	elif status:
		filters["status"] = status

	fields = ["name", "full_name", "role", "status", "joined_on", "member_number"]
	if membership.is_admin:
		fields += ["user", "phone", "total_contributed", "loan_outstanding", "borrow_limit", "creation",
			"payout_changed_on"]
	members = frappe.get_all("Coop Member", filters=filters, fields=fields, order_by="full_name asc")
	members.sort(key=lambda m: m.status != "Pending")
	return members


def _ensure_admin_remains(m):
	"""Never leave a cooperative without an active admin."""
	before = frappe.db.get_value("Coop Member", m.name, ["role", "status"], as_dict=True)
	was_active_admin = before.role == "Admin" and before.status == "Active"
	if was_active_admin and not (m.role == "Admin" and m.status == "Active"):
		others = frappe.db.count(
			"Coop Member",
			{"cooperative": m.cooperative, "role": "Admin", "status": "Active", "name": ["!=", m.name]},
		)
		if not others:
			frappe.throw(_("A cooperative needs at least one active admin. Make someone else admin first."))


@frappe.whitelist(methods=["POST"])
def update_member(member, status=None, role=None, note=None):
	"""Join requests are decided through the group's approval rule. Admins and the Chairman suspend
	or reinstate members, only Admins change roles, and nobody changes their own role or status."""
	m = frappe.get_doc("Coop Member", member)
	me = get_membership(m.cooperative, admin=True)
	ensure_not_self(me, m.name, _("You can't change your own role or status. Ask another admin."))
	coop_name = frappe.db.get_value("Cooperative", m.cooperative, "cooperative_name")

	if m.status == "Pending" and status in ("Active", "Left"):
		if not approvals.open_request("Coop Member", m.name):
			_request_admission(m.cooperative, m.name, m.full_name)
		approvals.decide_for("Coop Member", m.name, status == "Active", note)
		m.reload()
		return {"name": m.name, "status": m.status, "role": m.role}

	before_status, before_role = m.status, m.role
	if role and role != m.role:
		if not can(me, "settings"):
			frappe.throw(_("Only the cooperative's admins can change roles"), frappe.PermissionError)
		if role not in ROLES:
			frappe.throw(_("Invalid role"))
		m.role = role
	if status and status != m.status:
		if status not in ("Active", "Suspended", "Left"):
			frappe.throw(_("Invalid status"))
		require(me, "suspend" if status in ("Active", "Suspended") else "settings")
		if status in ("Suspended", "Left") and m.status == "Active":
			b = member_balances(m.name)
			if status == "Left" and (b.savings > 0.005 or b.loan_outstanding > 0.005):
				frappe.throw(_("This member still has savings or a loan. Suspend them instead."))
		m.status = status

	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)

	if m.status != before_status:
		notify(m.user, _("Your membership of {0} is now {1}").format(coop_name, _(m.status).lower()),
			cooperative=m.cooperative, kind="membership")
	if m.role != before_role:
		notify(m.user, _("You're now {0} of {1}").format(_(m.role).lower() if m.role != "Member" else _("a member"),
			coop_name), cooperative=m.cooperative, kind="membership")
	return {"name": m.name, "status": m.status, "role": m.role}


@frappe.whitelist(methods=["POST"])
def leave_cooperative(cooperative):
	me = get_membership(cooperative, allow_inactive=True)
	if not me.name:
		frappe.throw(_("You are not a member of this cooperative"))
	b = member_balances(me.name)
	currency = frappe.db.get_value("Cooperative", cooperative, "currency")
	if b.loan_outstanding > 0.005 or b.pending_loans > 0.005:
		frappe.throw(_("Please repay your loan before leaving"))
	if b.savings > 0.005 or b.withdrawal_requested > 0.005:
		frappe.throw(
			_("You still have {0} in savings. Request a withdrawal for all of it first, then you can leave.").format(
				money_text(b.savings, currency)
			)
		)
	m = frappe.get_doc("Coop Member", me.name)
	m.status = "Left"
	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)
	return {"ok": True}


# Adding a member by hand -----------------------------------------------------------------


def _has_password(user):
	"""Whether the account has a password yet (one an officer created has none until it's set up)."""
	return bool(frappe.db.sql(
		"select 1 from `__Auth` where doctype = 'User' and name = %s and fieldname = 'password' limit 1", user
	))


def _issue_code(user, cooperative, purpose, expires):
	"""A one-time code (shown once, stored hashed). Any earlier unused code stops working."""
	raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
	frappe.db.set_value("Coop Password Reset", {"user": user, "used_on": ["is", "not set"]}, "used_on", now_datetime())
	frappe.get_doc(
		{
			"doctype": "Coop Password Reset",
			"user": user,
			"cooperative": cooperative,
			"code_hash": hashlib.sha256(raw.encode()).hexdigest(),
			"created_by": frappe.session.user,
			"expires_on": expires,
			"purpose": purpose,
		}
	).insert(ignore_permissions=True)
	return f"{raw[:4]}-{raw[4:]}"


def _setup_code(user, cooperative):
	expires = add_days(now_datetime(), SETUP_CODE_DAYS)
	return {"setup_code": _issue_code(user, cooperative, "Setup", expires), "expires_on": expires, "days": SETUP_CODE_DAYS}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=40, seconds=60 * 60)
def add_member(cooperative, full_name, phone):
	"""An officer adds someone who isn't on the app yet, e.g. from the old WhatsApp list.

	A new account gets a setup code to pass on (WhatsApp or in person): the member signs in with
	their phone number and the code and chooses their own password, so nobody else ever knows it.
	There's no email. They join through the group's admission approval like anyone else; an officer
	who can approve admissions admits them straight away.
	"""
	from coopvault.api.auth import _app_user

	me = get_membership(cooperative, capability="members")
	full_name = " ".join((full_name or "").split())
	if len(full_name) < 2:
		frappe.throw(_("Enter the member's full name"))
	phone = normalize_phone(phone)
	if not phone or len(phone.lstrip("+")) < 10:
		frappe.throw(_("Enter the member's phone number"))

	user = frappe.db.get_value("User", {"mobile_no": phone})
	existing = user and frappe.db.get_value("Coop Member", {"cooperative": cooperative, "user": user}, ["name", "status"],
		as_dict=True)
	if existing and existing.status != "Left":
		frappe.throw(_("{0} is already in this group").format(phone))
	if user and not _app_user(user):
		frappe.throw(_("This phone number can't be used for a member"))
	if not user:
		names = full_name.split(" ", 1)
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"m{frappe.generate_hash(length=12)}@{PLACEHOLDER_EMAIL_DOMAIN}",
				"first_name": names[0],
				"last_name": names[1] if len(names) > 1 else "",
				"mobile_no": phone,
				"user_type": "Website User",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
		user = u.name

	if existing:
		frappe.db.set_value("Coop Member", existing.name, "status", "Pending")
		member = existing.name
	else:
		member = frappe.get_doc(
			{
				"doctype": "Coop Member",
				"cooperative": cooperative,
				"user": user,
				"full_name": frappe.db.get_value("User", user, "full_name") or full_name,
				"phone": phone,
				"role": "Member",
				"status": "Pending",
				"added_by": me.user,
				**_payout_defaults(user),
			}
		).insert(ignore_permissions=True).name
	ap = approvals.request(cooperative, "Member admission", ("Coop Member", member), requested_by=user,
		summary=_("{0} was added by {1}").format(full_name, me.full_name), notify=False)
	if approvals.why_not(ap, me.user) is None:
		approvals.decide(ap.name, True, _("Added by {0}").format(me.full_name))
	else:
		approvals._notify_approvers(ap)

	out = {"member": member, "full_name": full_name, "phone": phone,
		"status": frappe.db.get_value("Coop Member", member, "status"), "new_account": not _has_password(user)}
	if out["new_account"]:
		out.update(_setup_code(user, cooperative))
	else:
		coop_name = frappe.db.get_value("Cooperative", cooperative, "cooperative_name")
		notify(user, _("{0} added you to {1}").format(me.full_name, coop_name),
			_("Sign in with your usual password to see it."), link="/home", cooperative=cooperative, kind="membership")
	return out


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def new_setup_code(cooperative, member):
	"""A fresh setup code for a member who hasn't set up their account yet (the first one expired or got lost)."""
	me = get_membership(cooperative, capability="members")
	m = frappe.db.get_value("Coop Member", member, ["name", "user", "cooperative", "full_name", "phone"], as_dict=True)
	if not m or m.cooperative != cooperative:
		frappe.throw(_("Member not found"))
	if _has_password(m.user):
		frappe.throw(_("{0} has already set up their account. If they forgot their password, an admin can create a reset code.").format(
			m.full_name))
	notify(staff_of(cooperative, exclude_users=[me.user]),
		_("{0} created a new setup code for {1}").format(me.full_name, m.full_name), cooperative=cooperative,
		kind="security")
	return {"full_name": m.full_name, "phone": m.phone, **_setup_code(m.user, cooperative)}


# Password reset codes (no email): an Admin creates a one-time code and passes it on ----


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def create_reset_code(cooperative, member):
	me = get_membership(cooperative, owner=True)
	m = frappe.db.get_value(
		"Coop Member", member, ["name", "user", "cooperative", "full_name", "phone", "phone_changed_on"], as_dict=True
	)
	if not m or m.cooperative != cooperative:
		frappe.throw(_("Member not found"))
	ensure_not_self(me, m.name, _("You can't reset your own password this way. Ask another admin."))
	if frappe.db.get_value("User", m.user, "user_type") != "Website User":
		frappe.throw(_("This account can't be reset here"))

	raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
	frappe.db.set_value("Coop Password Reset", {"user": m.user, "used_on": ["is", "not set"]}, "used_on", now_datetime())
	expires = add_to_date(now_datetime(), minutes=RESET_CODE_MINUTES)
	frappe.get_doc(
		{
			"doctype": "Coop Password Reset",
			"user": m.user,
			"cooperative": cooperative,
			"code_hash": hashlib.sha256(raw.encode()).hexdigest(),
			"created_by": frappe.session.user,
			"expires_on": expires,
		}
	).insert(ignore_permissions=True)
	who = frappe.db.get_value("User", frappe.session.user, "full_name")
	notify(
		staff_of(cooperative, exclude_users=[frappe.session.user, m.user]),
		_("{0} created a password reset code for {1}").format(who, m.full_name),
		cooperative=cooperative,
		kind="security",
	)
	notify(m.user, _("A password reset code was created for you"), _("By {0}. If you didn't ask for this, tell your admins.").format(who),
		cooperative=cooperative, kind="security", push=False)
	return {"code": f"{raw[:4]}-{raw[4:]}", "expires_on": expires, "minutes": RESET_CODE_MINUTES,
		"full_name": m.full_name, "phone": m.phone, "phone_changed_on": m.phone_changed_on}


# Announcements -----------------------------------------------------------------


@frappe.whitelist()
def list_announcements(cooperative, limit=20, start=0):
	get_membership(cooperative)
	return frappe.get_all(
		"Coop Announcement",
		filters={"cooperative": cooperative},
		fields=["name", "title", "message", "pinned", "is_system", "posted_by", "creation"],
		order_by="pinned desc, creation desc",
		limit=cint(limit),
		start=cint(start),
	)


@frappe.whitelist(methods=["POST"])
def post_announcement(cooperative, title, message, pinned=0):
	get_membership(cooperative, capability="announce")
	doc = frappe.get_doc(
		{
			"doctype": "Coop Announcement",
			"cooperative": cooperative,
			"title": title,
			"message": message,
			"pinned": cint(pinned),
		}
	).insert(ignore_permissions=True)
	notify(members_of(cooperative, exclude_users=[frappe.session.user]), title, message[:180], link="/announcements",
		cooperative=cooperative, kind="announcement")
	return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def delete_announcement(name):
	doc = frappe.get_doc("Coop Announcement", name)
	get_membership(doc.cooperative, capability="announce")
	if doc.is_system:
		frappe.throw(_("Automatic notices about money and settings can't be deleted"))
	doc.delete(ignore_permissions=True)
	return {"ok": True}


# Statement ----------------------------------------------------------------------


@frappe.whitelist()
def get_statement(cooperative, member=None, limit=None):
	"""Chronological ledger of one member's money movements in a cooperative."""
	me = get_membership(cooperative)
	if member and member != me.name:
		if not me.is_admin:
			frappe.throw(_("You can only view your own statement"), frappe.PermissionError)
		if frappe.db.get_value("Coop Member", member, "cooperative") != cooperative:
			frappe.throw(_("Member not found"), frappe.PermissionError)
	else:
		member = me.name
	if not member:
		return {"member": None, "entries": []}

	entries = []
	# Entries for voluntary savings name their product; regular contributions don't need to
	voluntary = {p.name: p.product_name for p in savings.products(cooperative) if p.kind != savings.REGULAR}
	for c in frappe.get_all(
		"Coop Contribution",
		filters={"member": member},
		fields=["name", "amount", "contribution_date", "status", "payment_method", "creation", "savings_product"],
	):
		if c.status == "Pending" and c.payment_method == "Opening Balance":
			continue  # shown as a card to confirm instead
		entries.append(dict(doctype="Coop Contribution", name=c.name, type="Contribution", date=c.contribution_date,
			amount=c.amount, direction="in", status=c.status, method=c.payment_method, creation=c.creation,
			product=voluntary.get(c.savings_product)))
	for loan in frappe.get_all(
		"Coop Loan",
		filters={"member": member, "status": ["in", ["Pending", "Approved", "Sent", "Active", "Repaid", "Rejected"]]},
		fields=["name", "principal", "application_date", "disbursed_on", "status", "creation"],
	):
		entries.append(dict(doctype="Coop Loan", name=loan.name, type="Loan",
			date=loan.disbursed_on or loan.application_date, amount=loan.principal, direction="out",
			status=loan.status, creation=loan.creation))
	for r in frappe.get_all(
		"Coop Loan Repayment",
		filters={"member": member},
		fields=["name", "loan", "amount", "payment_date", "status", "payment_method", "creation"],
	):
		entries.append(dict(doctype="Coop Loan Repayment", name=r.name, type="Repayment", loan=r.loan,
			date=r.payment_date, amount=r.amount, direction="in", status=r.status, method=r.payment_method,
			creation=r.creation))
	for w in frappe.get_all(
		"Coop Withdrawal",
		filters={"member": member, "status": ["in", ["Pending", "Approved", "Sent", "Completed", "Rejected"]]},
		fields=["name", "amount", "requested_on", "sent_on", "status", "creation", "savings_product"],
	):
		entries.append(dict(doctype="Coop Withdrawal", name=w.name, type="Withdrawal", date=w.sent_on or w.requested_on,
			amount=w.amount, direction="out", status=w.status, creation=w.creation, product=voluntary.get(w.savings_product)))
	for s in frappe.db.sql(
		"""select s.name, s.period_end, s.posted_on, l.share_amount from `tabCoop Share Out Line` l
		join `tabCoop Share Out` s on s.name = l.parent where l.member = %s and s.status = 'Posted'""",
		member,
		as_dict=True,
	):
		entries.append(dict(doctype="Coop Share Out", name=s.name, type="Share-out", date=s.period_end,
			amount=s.share_amount, direction="in", status="Confirmed", creation=s.posted_on))

	entries.sort(key=lambda e: (str(e["date"]), str(e["creation"])), reverse=True)
	if limit:
		return {"entries": entries[: cint(limit)]}

	member_doc = frappe.db.get_value(
		"Coop Member",
		member,
		["name", "full_name", "phone", "role", "status", "joined_on", "payout_bank_name", "payout_account_number",
			"payout_account_name", "payout_changed_on", "security_hold_until", "phone_changed_on", "member_number"],
		as_dict=True,
	)
	balances = member_balances(member)
	return {
		"member": member_doc,
		"balances": balances,
		"standing": contribution_standing(member, balances.dues_paid),
		"loans": _loans_with_schedule({"member": member, "status": ["in", ["Pending", "Approved", "Sent", "Active"]]}),
		"entries": entries,
	}


@frappe.whitelist()
def pool_report(cooperative):
	"""Per-member breakdown for admins."""
	get_membership(cooperative, admin=True)
	members = frappe.get_all(
		"Coop Member", filters={"cooperative": cooperative, "status": ["!=", "Pending"]}, pluck="name"
	)
	rows = []
	for m in members:
		info = frappe.db.get_value("Coop Member", m, ["name", "full_name", "status", "role"], as_dict=True)
		info.update(member_balances(m))
		rows.append(info)
	rows.sort(key=lambda r: -flt(r.savings))
	return {"pool": cooperative_totals(cooperative), "members": rows}
