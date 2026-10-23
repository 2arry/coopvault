# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from coopvault import plans


class CoopPlan(Document):
	def validate(self):
		plans.validate_plan(self)
