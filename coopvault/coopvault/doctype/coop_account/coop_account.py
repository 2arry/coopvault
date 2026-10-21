# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# Purposes a cooperative can give its own accounts (e.g. a second bank account still counts as cash)
CUSTOM_PURPOSES = {"Bank", "Cash", "Other Asset", "Other Liability", "Other Income", "Fee Income", "Bank Charges",
	"Operating Expense"}


class CoopAccount(Document):
	def validate(self):
		self.account_code = (self.account_code or "").strip()
		self.account_name = (self.account_name or "").strip()
		if frappe.db.exists(
			"Coop Account", {"cooperative": self.cooperative, "account_code": self.account_code, "name": ["!=", self.name]}
		):
			frappe.throw(_("Account code {0} is already used").format(self.account_code))
		if not self.is_new():
			before = self.get_doc_before_save()
			if before.is_system and (self.root_type != before.root_type or self.account_type != before.account_type
				or self.disabled or self.cooperative != before.cooperative):
				frappe.throw(_("System accounts can be renamed but not changed or disabled"))
			if self.root_type != before.root_type and self.has_entries():
				frappe.throw(_("This account has entries, so its type can't change"))
		if not self.is_system and self.account_type and self.account_type not in CUSTOM_PURPOSES:
			# Member savings, loans and the other automatic purposes stay with the system accounts
			frappe.throw(_("Choose a different purpose for a new account"))

	def has_entries(self):
		return frappe.db.exists("Coop Journal Line", {"account": self.name})

	def on_trash(self):
		if self.is_system or self.has_entries():
			frappe.throw(_("Accounts with entries (or system accounts) can't be deleted. Disable it instead."))
