# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CoopAnnouncement(Document):
	def before_insert(self):
		self.posted_by = self.posted_by or frappe.session.user
