import frappe

from coopvault.notify import ensure_vapid_keys

ROLES = ["Coop Manager"]


def before_install():
	make_roles()


def after_install():
	make_roles()
	setup_settings()
	set_defaults()
	ensure_vapid_keys()
	allow_group_signups()


def after_migrate():
	make_roles()
	setup_settings()
	ensure_vapid_keys()
	allow_group_signups()


def make_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def setup_settings():
	settings = frappe.get_single("CoopVault Settings")
	webhook = frappe.utils.get_url("/api/method/coopvault.api.payments.paystack_webhook")
	if settings.webhook_url != webhook:
		settings.db_set("webhook_url", webhook)


def set_defaults():
	"""Sensible regional defaults for a fresh site; never overrides choices already made."""
	settings = frappe.get_single("System Settings")
	if not settings.time_zone:
		settings.db_set("time_zone", "Africa/Lagos")
	if not settings.country:
		settings.db_set("country", "Nigeria")


def allow_group_signups():
	"""Frappe refuses new accounts after 60 in an hour. A cooperative sharing its invite link at
	a meeting can pass that, so raise it unless the site already chose a limit."""
	if not frappe.conf.get("throttle_user_limit"):
		from frappe.installer import update_site_config

		update_site_config("throttle_user_limit", 1000)
