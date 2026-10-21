import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, flt, today

from coopvault import paystack
from coopvault.api import get_membership, is_super_user, require_login
from coopvault.ledger import (
	contribution_standing,
	cooperative_totals,
	loan_schedule,
	member_balances,
	period_start,
	refresh_all,
	shift_period,
)

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
	"accept_online_payments",
	"fee_bearer",
]
COOP_EDITABLE_FIELDS = [
	f for f in COOP_PUBLIC_FIELDS if f not in ("name", "status", "account_verified", "settlement_bank_code")
]
BANK_FIELDS = ("bank_name", "account_number", "account_name")


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
	fields = ["payout_bank_code", "payout_bank_name", "payout_account_number", "payout_account_name"]
	row = frappe.db.get_value(
		"Coop Member", {"user": user, "payout_account_number": ["is", "set"]}, fields, as_dict=True
	)
	return dict(row) if row else {}


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
	for field in COOP_EDITABLE_FIELDS:
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
	if existing:
		if existing.status == "Left":
			frappe.db.set_value("Coop Member", existing.name, "status", "Pending")
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
	return {"name": coop.name, "status": "Pending"}


def _loans_with_schedule(filters):
	loans = frappe.get_all(
		"Coop Loan",
		filters=filters,
		fields=["name", "status", "principal", "total_payable", "amount_repaid", "balance", "due_date",
			"disbursed_on", "tenure_months", "purpose", "application_date"],
		order_by="creation desc",
	)
	for loan in loans:
		schedule = loan_schedule(loan)
		if schedule:
			schedule.pop("rows")
		loan.schedule = schedule
	return loans


@frappe.whitelist()
def get_dashboard(cooperative):
	from coopvault.api.payments import autosave_status, config, dedicated_account_status

	membership = get_membership(cooperative)
	coop = frappe.db.get_value("Cooperative", cooperative, COOP_PUBLIC_FIELDS + ["join_code"], as_dict=True)
	if not membership.is_admin:
		coop.pop("join_code")

	data = {
		"cooperative": coop,
		"membership": membership,
		"pool": cooperative_totals(cooperative),
		"payments": config(cooperative),
		"announcements": frappe.get_all(
			"Coop Announcement",
			filters={"cooperative": cooperative},
			fields=["name", "title", "message", "pinned", "posted_by", "creation"],
			order_by="pinned desc, creation desc",
			limit=5,
		),
	}

	if membership.name:
		balances = member_balances(membership.name)
		data["balances"] = balances
		data["standing"] = contribution_standing(membership.name, balances.total_contributed)
		data["active_loans"] = _loans_with_schedule(
			{"member": membership.name, "status": ["in", ["Pending", "Active"]]}
		)
		data["recent"] = get_statement(cooperative, limit=6)["entries"]
		data["autosave"] = autosave_status(membership.name)
		data["dedicated_account"] = dedicated_account_status(membership.name)

	if membership.is_admin:
		data["period"] = period_summary(cooperative)
		data["pending"] = {
			"members": frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Pending"}),
			"contributions": frappe.db.count(
				"Coop Contribution", {"cooperative": cooperative, "status": "Pending"}
			),
			"loans": frappe.db.count("Coop Loan", {"cooperative": cooperative, "status": "Pending"}),
			"repayments": frappe.db.count(
				"Coop Loan Repayment", {"cooperative": cooperative, "status": "Pending"}
			),
		}
	return data


@frappe.whitelist(methods=["POST"])
def update_cooperative(cooperative, **kwargs):
	get_membership(cooperative, admin=True)
	coop = frappe.get_doc("Cooperative", cooperative)
	for field in COOP_EDITABLE_FIELDS:
		if field in kwargs:
			if field in BANK_FIELDS and coop.account_verified and kwargs[field] != coop.get(field):
				# A verified account can only be replaced through set_bank_account
				continue
			value = kwargs[field]
			coop.set(field, (value or None) if field == "start_date" else value)
	coop.save(ignore_permissions=True)
	refresh_all(cooperative=cooperative)
	for m in frappe.get_all("Coop Member", filters={"cooperative": cooperative}, pluck="name"):
		refresh_all(member=m)
	return get_dashboard(cooperative)["cooperative"]


@frappe.whitelist(methods=["POST"])
def regenerate_join_code(cooperative):
	from coopvault.coopvault.doctype.cooperative.cooperative import generate_join_code

	get_membership(cooperative, admin=True)
	code = generate_join_code()
	frappe.db.set_value("Cooperative", cooperative, "join_code", code)
	return {"join_code": code}


@frappe.whitelist(methods=["POST"])
def set_bank_account(cooperative, bank_code, account_number):
	"""Verify the cooperative's bank account with Paystack and settle online payments into it."""
	get_membership(cooperative, admin=True)
	if not paystack.is_enabled():
		frappe.throw(_("Online payments are not set up, so enter the bank details manually"))
	account_name = paystack.resolve_account(account_number, bank_code)
	coop = frappe.get_doc("Cooperative", cooperative)
	coop.update(
		{
			"settlement_bank_code": bank_code,
			"bank_name": paystack.bank_name(bank_code, coop.currency) or coop.bank_name,
			"account_number": account_number.strip(),
			"account_name": account_name,
			"account_verified": 1,
		}
	)
	coop.paystack_subaccount_code = paystack.upsert_subaccount(coop)
	coop.save(ignore_permissions=True)
	return get_dashboard(cooperative)["cooperative"]


def period_summary(cooperative):
	"""Collection progress for the current contribution period."""
	coop = frappe.db.get_value(
		"Cooperative", cooperative, ["contribution_amount", "contribution_frequency"], as_dict=True
	)
	if coop.contribution_frequency not in ("Weekly", "Monthly", "Quarterly") or not flt(coop.contribution_amount):
		return None
	start = period_start(today(), coop.contribution_frequency)
	end = add_days(shift_period(start, coop.contribution_frequency), -1)
	members = frappe.get_all(
		"Coop Member", filters={"cooperative": cooperative, "status": "Active"}, pluck="name"
	)
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
		"frequency": coop.contribution_frequency,
		"start": start,
		"end": end,
		"expected": flt(coop.contribution_amount) * len(members),
		"collected": flt(collected[0].total) if collected else 0.0,
		"members": len(members),
		**counts,
	}


@frappe.whitelist()
def contribution_tracker(cooperative):
	"""Who is up to date, who still owes this period and who is behind (admins only)."""
	get_membership(cooperative, admin=True)
	rows = []
	for m in frappe.get_all(
		"Coop Member",
		filters={"cooperative": cooperative, "status": "Active"},
		fields=["name", "full_name", "phone", "role", "joined_on", "total_contributed"],
	):
		standing = contribution_standing(m.name, m.total_contributed)
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
		fields += ["user", "phone", "total_contributed", "loan_outstanding", "borrow_limit", "creation"]
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
	m = frappe.get_doc("Coop Member", member)
	get_membership(m.cooperative, admin=True)

	if role:
		if role not in ("Member", "Treasurer", "Admin"):
			frappe.throw(_("Invalid role"))
		m.role = role
	if status:
		if status not in ("Pending", "Active", "Suspended", "Left"):
			frappe.throw(_("Invalid status"))
		m.status = status

	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)
	return {"name": m.name, "status": m.status, "role": m.role}


@frappe.whitelist(methods=["POST"])
def leave_cooperative(cooperative):
	me = get_membership(cooperative, allow_inactive=True)
	if not me.name:
		frappe.throw(_("You are not a member of this cooperative"))
	b = member_balances(me.name)
	if b.loan_outstanding > 0 or b.pending_loans > 0:
		frappe.throw(_("Please clear your outstanding loans before leaving"))
	m = frappe.get_doc("Coop Member", me.name)
	m.status = "Left"
	_ensure_admin_remains(m)
	m.save(ignore_permissions=True)
	return {"ok": True}


# Announcements -----------------------------------------------------------------


@frappe.whitelist()
def list_announcements(cooperative, limit=20, start=0):
	get_membership(cooperative)
	return frappe.get_all(
		"Coop Announcement",
		filters={"cooperative": cooperative},
		fields=["name", "title", "message", "pinned", "posted_by", "creation"],
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
	return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def delete_announcement(name):
	doc = frappe.get_doc("Coop Announcement", name)
	get_membership(doc.cooperative, admin=True)
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
	else:
		member = me.name
	if not member:
		return {"member": None, "entries": []}

	entries = []
	for c in frappe.get_all(
		"Coop Contribution",
		filters={"member": member},
		fields=["name", "amount", "contribution_date", "status", "payment_method", "creation"],
	):
		entries.append(
			dict(
				doctype="Coop Contribution",
				name=c.name,
				type="Contribution",
				date=c.contribution_date,
				amount=c.amount,
				direction="in",
				status=c.status,
				method=c.payment_method,
				creation=c.creation,
			)
		)
	for loan in frappe.get_all(
		"Coop Loan",
		filters={"member": member, "status": ["in", ["Pending", "Active", "Repaid", "Rejected"]]},
		fields=["name", "principal", "application_date", "disbursed_on", "status", "creation"],
	):
		entries.append(
			dict(
				doctype="Coop Loan",
				name=loan.name,
				type="Loan",
				date=loan.disbursed_on or loan.application_date,
				amount=loan.principal,
				direction="out",
				status=loan.status,
				creation=loan.creation,
			)
		)
	for r in frappe.get_all(
		"Coop Loan Repayment",
		filters={"member": member},
		fields=["name", "loan", "amount", "payment_date", "status", "payment_method", "creation"],
	):
		entries.append(
			dict(
				doctype="Coop Loan Repayment",
				name=r.name,
				type="Repayment",
				loan=r.loan,
				date=r.payment_date,
				amount=r.amount,
				direction="in",
				status=r.status,
				method=r.payment_method,
				creation=r.creation,
			)
		)

	entries.sort(key=lambda e: (str(e["date"]), str(e["creation"])), reverse=True)
	if limit:
		return {"entries": entries[: cint(limit)]}

	member_doc = frappe.db.get_value(
		"Coop Member",
		member,
		["name", "full_name", "phone", "role", "status", "joined_on", "payout_bank_name", "payout_account_number",
			"payout_account_name"],
		as_dict=True,
	)
	balances = member_balances(member)
	return {
		"member": member_doc,
		"balances": balances,
		"standing": contribution_standing(member, balances.total_contributed),
		"loans": _loans_with_schedule({"member": member, "status": ["in", ["Pending", "Active"]]}),
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
	rows.sort(key=lambda r: -flt(r.total_contributed))
	return {"pool": cooperative_totals(cooperative), "members": rows}
