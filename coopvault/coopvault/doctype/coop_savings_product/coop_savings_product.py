# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from coopvault import savings


class CoopSavingsProduct(Document):
	def validate(self):
		savings.validate_product(self)

	def on_trash(self):
		if self.kind == savings.REGULAR or frappe.db.exists("Coop Journal Line", {"account": self.account}):
			frappe.throw(_("Savings products that hold money can't be deleted. Switch it off instead."))
