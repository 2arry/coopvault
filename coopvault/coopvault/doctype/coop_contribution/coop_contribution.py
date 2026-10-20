# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from coopvault.ledger import refresh_all


class CoopContribution(Document):
	def validate(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Contribution amount must be greater than zero"))
		member = frappe.db.get_value("Coop Member", self.member, ["cooperative", "status"], as_dict=True)
		if not member or member.cooperative != self.cooperative:
			frappe.throw(_("Member does not belong to this cooperative"))
		if self.is_new() and member.status != "Active":
			frappe.throw(_("Only active members can make contributions"))
		if self.has_value_changed("status") and self.status in ("Confirmed", "Rejected"):
			self.reviewed_by = self.reviewed_by or frappe.session.user
			self.reviewed_on = now_datetime()

	def on_update(self):
		refresh_all(member=self.member, cooperative=self.cooperative)

	def on_trash(self):
		if self.status == "Confirmed":
			frappe.throw(_("Confirmed contributions cannot be deleted. Reject it instead."))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative)
