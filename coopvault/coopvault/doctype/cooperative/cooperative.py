# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import secrets
import string

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


def generate_join_code(length=6):
	alphabet = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")
	while True:
		code = "".join(secrets.choice(alphabet) for _ in range(length))
		if not frappe.db.exists("Cooperative", {"join_code": code}):
			return code


class Cooperative(Document):
	def before_insert(self):
		if not self.join_code:
			self.join_code = generate_join_code()
		if not self.created_by_user:
			self.created_by_user = frappe.session.user

	def validate(self):
		if not 0 <= flt(self.loan_multiplier) <= 1:
			frappe.throw(_("Borrow limit multiplier must be between 0 and 1: members cannot borrow more than they have contributed"))
		if not 0 <= flt(self.interest_rate) <= 100:
			frappe.throw(_("Interest rate must be between 0 and 100"))
		if (self.max_loan_tenure_months or 0) < 1:
			self.max_loan_tenure_months = 1
		self.currency = (self.currency or "NGN").upper()

	def on_trash(self):
		for doctype in ("Coop Contribution", "Coop Loan", "Coop Loan Repayment"):
			if frappe.db.exists(doctype, {"cooperative": self.name}):
				frappe.throw(_("Cannot delete a cooperative that has financial records"))
		for doctype in ("Coop Member", "Coop Announcement"):
			frappe.db.delete(doctype, {"cooperative": self.name})
