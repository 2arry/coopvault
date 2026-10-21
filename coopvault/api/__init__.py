"""Whitelisted API consumed by the CoopVault PWA.

All endpoints check cooperative membership / roles explicitly and then work with
ignore_permissions, so regular members never need desk access.

Roles inside a cooperative:
- Admin: runs the group (members, roles, rules, bank account, password resets) and can do
  everything a Treasurer can.
- Treasurer: handles money (approves join requests, sends loans and withdrawals, proposes
  opening balances, posts announcements). Can't change roles or settings.
- Member: their own savings, loans and withdrawals.
Nobody can approve or send money to themselves.
"""

import frappe
from frappe import _

STAFF_ROLES = ("Admin", "Treasurer")
SUPER_ROLES = ("System Manager", "Coop Manager")


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to continue"), frappe.AuthenticationError)
	return frappe.session.user


def is_super_user(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(SUPER_ROLES))


def get_membership(cooperative, user=None, admin=False, owner=False, allow_inactive=False):
	"""Return the current user's Coop Member row for a cooperative, enforcing access.

	admin=True requires Admin or Treasurer ("staff"); owner=True requires Admin.
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
			is_admin=True, is_owner=True,
		)
	if not member:
		frappe.throw(_("You are not a member of this cooperative"), frappe.PermissionError)
	if member.status != "Active" and not allow_inactive:
		frappe.throw(_("Your membership is {0}").format(_(member.status)), frappe.PermissionError)
	super_user = is_super_user(user)
	active = member.status == "Active"
	member.is_admin = active and (member.role in STAFF_ROLES or super_user)
	member.is_owner = active and (member.role == "Admin" or super_user)
	if owner and not member.is_owner:
		frappe.throw(_("Only the cooperative's admins can do this"), frappe.PermissionError)
	if admin and not member.is_admin:
		frappe.throw(_("Only the cooperative's admins or treasurer can do this"), frappe.PermissionError)
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
