import hashlib
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, add_to_date, cint, flt, fmt_money, getdate, now_datetime, today

from coopvault import paystack
from coopvault.api import ensure_not_self, get_membership, is_super_user, require_login
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
from coopvault.utils import mask_account, require_password

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
]
COOP_RULE_FIELDS = ["cooperative_name", "description", "loan_multiplier", "interest_rate", "max_loan_tenure_months",
	"accept_online_payments", "fee_bearer"]
RESET_CODE_MINUTES = 30
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _membership_summary(member_name):
	m = frappe.db.get_value(
		"Coop Member", member_name, ["name", "cooperative", "role", "status", "joined_on"], as_dict=True
	)
	coop = frappe.db.get_value(
		"Cooperative", m.cooperative, ["cooperative_name", "currency", "member_count"], as_dict=True
	)
	m.update(coop)
	if m.status == "Active":
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
		filters={"user": user, "status": ["in", ["Active", "Pending", "Suspended"]]},
		pluck="name",
		order_by="creation asc",
	)
	return [_membership_summary(m) for m in members]


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
			_notify_join_request(coop.name, full_name)
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
	_notify_join_request(coop.name, full_name)
	return {"name": coop.name, "status": "Pending"}


def _notify_join_request(cooperative, full_name):
	notify(
		staff_of(cooperative),
		_("{0} wants to join").format(full_name),
		_("Approve or decline them in Manage → Review."),
		link="/admin?tab=reviews",
		cooperative=cooperative,
		kind="join-request",
	)


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
	return {"current": current, "upcoming": upcoming}


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
	}

	if membership.name:
		balances = member_balances(membership.name)
		data["balances"] = balances
		data["standing"] = contribution_standing(membership.name, balances.total_contributed)
		data["active_loans"] = _loans_with_schedule(
			{"member": membership.name, "status": ["in", ["Pending", "Sent", "Active"]]}
		)
		data["withdrawals"] = frappe.get_all(
			"Coop Withdrawal",
			filters={"member": membership.name, "status": ["in", ["Pending", "Sent"]]},
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

	if membership.is_admin:
		data["period"] = period_summary(cooperative)
		data["pending"] = pending_counts(cooperative)
	return data


def pending_counts(cooperative):
	return {
		"members": frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Pending"}),
		"loans": frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Pending"}),
		"withdrawals": frappe.db.count("Coop Withdrawal", {"cooperative": cooperative, "status": "Pending"}),
		"disputes": frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1})
		+ frappe.db.count("Coop Withdrawal", {"cooperative": cooperative, "status": "Sent", "receipt_disputed": 1}),
	}


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
				fmt_money(amount, currency=coop.currency),
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

	fields = ["name", "full_name", "role", "status", "joined_on"]
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
def update_member(member, status=None, role=None):
	"""Admins and the treasurer approve or decline join requests. Only Admins change roles or
	suspend members, and nobody changes their own role or status."""
	m = frappe.get_doc("Coop Member", member)
	me = get_membership(m.cooperative, admin=True)
	ensure_not_self(me, m.name, _("You can't change your own role or status. Ask another admin."))
	coop_name = frappe.db.get_value("Cooperative", m.cooperative, "cooperative_name")
	before_status, before_role = m.status, m.role

	if role and role != m.role:
		if not me.is_owner:
			frappe.throw(_("Only the cooperative's admins can change roles"), frappe.PermissionError)
		if role not in ("Member", "Treasurer", "Admin"):
			frappe.throw(_("Invalid role"))
		m.role = role
	if status and status != m.status:
		if status not in ("Pending", "Active", "Suspended", "Left"):
			frappe.throw(_("Invalid status"))
		is_join_decision = m.status == "Pending" and status in ("Active", "Left")
		if not is_join_decision and not me.is_owner:
			frappe.throw(_("Only the cooperative's admins can suspend or reinstate members"), frappe.PermissionError)
		if status in ("Suspended", "Left") and m.status == "Active":
			b = member_balances(m.name)
			if status == "Left" and (b.savings > 0.005 or b.loan_outstanding > 0.005):
				frappe.throw(_("This member still has savings or a loan. Suspend them instead."))
		m.status = status

	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)

	if before_status == "Pending" and m.status == "Active":
		notify(m.user, _("You're in! Welcome to {0}").format(coop_name), _("You can start saving now."),
			link="/home", cooperative=m.cooperative, kind="join-approved")
	elif before_status == "Pending" and m.status == "Left":
		notify(m.user, _("Your request to join {0} was declined").format(coop_name), cooperative=m.cooperative,
			kind="join-declined")
	elif m.status != before_status:
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
				fmt_money(b.savings, currency=currency)
			)
		)
	m = frappe.get_doc("Coop Member", me.name)
	m.status = "Left"
	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)
	return {"ok": True}


# Password reset codes (no email): an Admin creates a one-time code and passes it on ----


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def create_reset_code(cooperative, member):
	me = get_membership(cooperative, owner=True)
	m = frappe.db.get_value("Coop Member", member, ["name", "user", "cooperative", "full_name", "phone"], as_dict=True)
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
		"full_name": m.full_name, "phone": m.phone}


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
	get_membership(cooperative, admin=True)
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
	get_membership(doc.cooperative, admin=True)
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
	for c in frappe.get_all(
		"Coop Contribution",
		filters={"member": member, "status": ["!=", "Pending"]},
		fields=["name", "amount", "contribution_date", "status", "payment_method", "creation"],
	):
		entries.append(dict(doctype="Coop Contribution", name=c.name, type="Contribution", date=c.contribution_date,
			amount=c.amount, direction="in", status=c.status, method=c.payment_method, creation=c.creation))
	for loan in frappe.get_all(
		"Coop Loan",
		filters={"member": member, "status": ["in", ["Pending", "Sent", "Active", "Repaid", "Rejected"]]},
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
		filters={"member": member, "status": ["in", ["Pending", "Sent", "Completed", "Rejected"]]},
		fields=["name", "amount", "requested_on", "sent_on", "status", "creation"],
	):
		entries.append(dict(doctype="Coop Withdrawal", name=w.name, type="Withdrawal", date=w.sent_on or w.requested_on,
			amount=w.amount, direction="out", status=w.status, creation=w.creation))

	entries.sort(key=lambda e: (str(e["date"]), str(e["creation"])), reverse=True)
	if limit:
		return {"entries": entries[: cint(limit)]}

	member_doc = frappe.db.get_value(
		"Coop Member",
		member,
		["name", "full_name", "phone", "role", "status", "joined_on", "payout_bank_name", "payout_account_number",
			"payout_account_name", "payout_changed_on", "security_hold_until"],
		as_dict=True,
	)
	balances = member_balances(member)
	return {
		"member": member_doc,
		"balances": balances,
		"standing": contribution_standing(member, balances.total_contributed),
		"loans": _loans_with_schedule({"member": member, "status": ["in", ["Pending", "Sent", "Active"]]}),
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
