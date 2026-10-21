# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today


class CoopMember(Document):
	def validate(self):
		duplicate = frappe.db.exists(
			"Coop Member", {"cooperative": self.cooperative, "user": self.user, "name": ["!=", self.name]}
		)
		if duplicate:
			frappe.throw(_("{0} is already a member of this cooperative").format(self.user))
		if not self.full_name:
			self.full_name = frappe.db.get_value("User", self.user, "full_name") or self.user
		if self.status == "Active" and not self.joined_on:
			self.joined_on = today()

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Left":
			from coopvault.api.payments import close_dedicated_account

			close_dedicated_account(self.name)

	def on_trash(self):
		for doctype in ("Coop Contribution", "Coop Loan"):
			if frappe.db.exists(doctype, {"member": self.name}):
				frappe.throw(_("Cannot delete a member with financial records. Set status to Left instead."))
