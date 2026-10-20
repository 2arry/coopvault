import frappe

ROLES = ["Coop Manager"]


def before_install():
	make_roles()


def after_install():
	make_roles()
	setup_settings()


def after_migrate():
	make_roles()
	setup_settings()


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
