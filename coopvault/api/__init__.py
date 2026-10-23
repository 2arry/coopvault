"""Whitelisted API consumed by the Cooperative360 PWA.

All endpoints check cooperative membership / roles explicitly and then work with
ignore_permissions, so regular members never need desk access.

Roles inside a cooperative (one per member):
- Admin: sets the group up (rules, bank account, roles, approval rules, password resets) and can
  do everything below.
- Chairman: oversight; signs off big approvals; reads the books.
- Accountant: the books (journals, closing, share-out), records offline payments.
- Treasurer: moves money (sends approved loans and withdrawals), records offline payments.
- Loan Officer: reviews loan applications and follows up arrears.
- Member: their own savings, loans and withdrawals.
Approvals follow each cooperative's rules (see coopvault/approvals.py). Nobody approves their own
request, anything that benefits them, or two stages of the same request.
"""

import frappe
from frappe import _, _lt

ROLES = ("Member", "Treasurer", "Accountant", "Loan Officer", "Chairman", "Admin")
STAFF_ROLES = ROLES[1:]
SUPER_ROLES = ("System Manager", "Coop Manager")

# What each role can do (Admins can do everything)
CAPABILITIES = {
	"settings": ("Admin",),
	"reset": ("Admin",),
	"members": ("Admin", "Chairman", "Treasurer", "Accountant", "Loan Officer"),
	"suspend": ("Admin", "Chairman"),
	"pay_out": ("Admin", "Treasurer"),
	"record": ("Admin", "Treasurer", "Accountant"),
	"books": ("Admin", "Chairman", "Accountant", "Treasurer"),
	"journal": ("Admin", "Accountant", "Treasurer"),
	"close": ("Admin", "Accountant"),
	"loans": ("Admin", "Chairman", "Treasurer", "Loan Officer"),
	"announce": ("Admin", "Chairman", "Treasurer"),
	"opening": ("Admin", "Treasurer", "Accountant"),
}
DENIED = {
	"settings": _lt("Only the cooperative's admins can do this"),
	"reset": _lt("Only the cooperative's admins can create reset codes"),
	"suspend": _lt("Only an admin or the chairman can suspend or reinstate members"),
	"pay_out": _lt("Only the treasurer or an admin sends money"),
	"record": _lt("Only the treasurer, accountant or an admin records payments"),
	"books": _lt("Only the cooperative's officers can see the books"),
	"journal": _lt("Only the accountant, treasurer or an admin prepares journal entries"),
	"close": _lt("Only the accountant or an admin can close the books or prepare a share-out"),
	"loans": _lt("Only the loan officer, treasurer, chairman or an admin can do this"),
	"announce": _lt("Only the chairman, treasurer or an admin can post announcements"),
	"opening": _lt("Only the treasurer, accountant or an admin can propose opening balances"),
	"members": _lt("Only the cooperative's officers can do this"),
}


def can(me, capability):
	return bool(me.get("is_super")) or (me.get("status") == "Active" and me.get("role") in CAPABILITIES[capability])


def require(me, capability):
	if not can(me, capability):
		frappe.throw(str(DENIED.get(capability, _("You don't have permission to do this"))), frappe.PermissionError)
	return me


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to continue"), frappe.AuthenticationError)
	return frappe.session.user


def is_super_user(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(SUPER_ROLES))


def get_membership(cooperative, user=None, admin=False, owner=False, allow_inactive=False, capability=None):
	"""Return the current user's Coop Member row for a cooperative, enforcing access.

	admin=True requires any officer role ("staff"); owner=True requires Admin; capability=... requires
	a role that has it (see CAPABILITIES).
	Super users get a synthetic Admin membership for cooperatives they do not belong to.
	"""
	user = user or require_login()
	if not cooperative or not frappe.db.exists("Cooperative", cooperative):
		frappe.throw(_("Cooperative not found"), frappe.DoesNotExistError)

	member = frappe.db.get_value(
		"Coop Member",
		{"cooperative": cooperative, "user": user},
		["name", "cooperative", "user", "full_name", "role", "status"],
		as_dict=True,
	)
	if not member and is_super_user(user):
		return frappe._dict(
			name=None, cooperative=cooperative, user=user, role="Admin", status="Active", is_super=True,
			is_admin=True, is_owner=True, capabilities=list(CAPABILITIES),
		)
	if not member:
		frappe.throw(_("You are not a member of this cooperative"), frappe.PermissionError)
	if member.status != "Active" and not allow_inactive:
		frappe.throw(_("Your membership is {0}").format(_(member.status)), frappe.PermissionError)
	super_user = is_super_user(user)
	active = member.status == "Active"
	member.is_admin = active and (member.role in STAFF_ROLES or super_user)
	member.is_owner = active and (member.role == "Admin" or super_user)
	member.is_super = super_user
	if owner and not member.is_owner:
		frappe.throw(_("Only the cooperative's admins can do this"), frappe.PermissionError)
	if admin and not member.is_admin:
		frappe.throw(_("Only the cooperative's officers can do this"), frappe.PermissionError)
	if capability:
		require(member, capability)
	member.capabilities = [c for c in CAPABILITIES if can(member, c)]
	return member


def ensure_not_self(me, member, action=None):
	"""Separation of duties: nobody approves or sends money to themselves."""
	if me.name and me.name == member:
		frappe.throw(
			action or _("Another admin or the treasurer has to do this, because it's your own request"),
			frappe.PermissionError,
		)


def is_admin_of(cooperative, user=None):
	try:
		return get_membership(cooperative, user=user).is_admin or is_super_user(user)
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return False
