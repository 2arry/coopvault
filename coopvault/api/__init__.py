"""Whitelisted API consumed by the CoopVault PWA.

All endpoints check cooperative membership / roles explicitly and then work with
ignore_permissions, so regular members never need desk access.
"""

import frappe
from frappe import _

ADMIN_ROLES = ("Admin", "Treasurer")
SUPER_ROLES = ("System Manager", "Coop Manager")


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to continue"), frappe.AuthenticationError)
	return frappe.session.user


def is_super_user(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(SUPER_ROLES))


def get_membership(cooperative, user=None, admin=False, allow_inactive=False):
	"""Return the current user's Coop Member row for a cooperative, enforcing access.

	Super users get a synthetic admin membership for cooperatives they do not belong to.
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
		return frappe._dict(name=None, cooperative=cooperative, user=user, role="Admin", status="Active", is_super=True)
	if not member:
		frappe.throw(_("You are not a member of this cooperative"), frappe.PermissionError)
	if member.status != "Active" and not allow_inactive:
		frappe.throw(_("Your membership is {0}").format(_(member.status)), frappe.PermissionError)
	member.is_admin = member.role in ADMIN_ROLES or is_super_user(user)
	if admin and not member.is_admin:
		frappe.throw(_("Only cooperative admins can do this"), frappe.PermissionError)
	return member


def is_admin_of(cooperative, user=None):
	try:
		return get_membership(cooperative, user=user).is_admin or is_super_user(user)
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return False
