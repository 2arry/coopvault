"""Move existing data to the session-based, online-payments-only model."""

import frappe

from coopvault.utils import normalize_phone


def execute():
	# Old builds handed out permanent API keys as login tokens; sessions replace them
	for user in frappe.get_all("User", filters={"user_type": "Website User", "api_key": ["is", "set"]}, pluck="name"):
		frappe.db.set_value("User", user, {"api_key": None, "api_secret": None}, update_modified=False)

	# One canonical phone format so login by phone works however the number is typed
	for user, phone in frappe.get_all("User", filters={"mobile_no": ["is", "set"]}, fields=["name", "mobile_no"], as_list=True):
		canonical = normalize_phone(phone)
		if canonical and canonical != phone and not frappe.db.exists("User", {"mobile_no": canonical}):
			frappe.db.set_value("User", user, "mobile_no", canonical, update_modified=False)
			frappe.db.set_value("Coop Member", {"user": user}, "phone", canonical, update_modified=False)

	# Dated contribution plans: start each existing cooperative with the plan it had
	for name in frappe.get_all("Cooperative", pluck="name"):
		coop = frappe.get_doc("Cooperative", name)
		if not coop.contribution_plans:
			coop.flags.ignore_permissions = True
			coop.save()

	# Manual "I paid" claims aren't accepted any more; close the ones still waiting
	for doctype in ("Coop Contribution", "Coop Loan Repayment"):
		for name in frappe.get_all(
			doctype, filters={"status": "Pending", "payment_method": ["in", ["Bank Transfer", "Cash"]]}, pluck="name"
		):
			frappe.db.set_value(
				doctype,
				name,
				{"status": "Rejected", "review_note": "Manual payment claims were retired. Please pay in the app."},
			)
