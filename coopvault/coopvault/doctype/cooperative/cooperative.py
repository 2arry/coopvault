# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import secrets
import string

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

FREQUENCIES = ("Weekly", "Monthly", "Quarterly", "Flexible")


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

	def after_insert(self):
		from coopvault.accounting import ensure_chart

		ensure_chart(self.name)

	def validate(self):
		if not 0 <= flt(self.loan_multiplier) <= 1:
			frappe.throw(_("Borrow limit multiplier must be between 0 and 1: members cannot borrow more than they have saved"))
		if not 0 <= flt(self.interest_rate) <= 100:
			frappe.throw(_("Interest rate must be between 0 and 100"))
		if (self.max_loan_tenure_months or 0) < 1:
			self.max_loan_tenure_months = 1
		self.currency = (self.currency or "NGN").upper()
		self.sync_plans()

	def sync_plans(self):
		"""Keep a dated contribution plan and mirror the one in force today on the header fields."""
		if not self.contribution_plans:
			self.append(
				"contribution_plans",
				{
					"effective_from": self.start_date or getdate(self.creation or today()),
					"amount": flt(self.contribution_amount),
					"frequency": self.contribution_frequency or "Monthly",
					"changed_by": frappe.session.user,
				},
			)
		for row in self.contribution_plans:
			if row.frequency not in FREQUENCIES:
				frappe.throw(_("Choose how often members contribute"))
			if flt(row.amount) < 0:
				frappe.throw(_("Contribution amount can't be negative"))
		self.contribution_plans.sort(key=lambda r: getdate(r.effective_from))
		for i, row in enumerate(self.contribution_plans, 1):
			row.idx = i
		current = self.contribution_plans[0]
		for row in self.contribution_plans:
			if getdate(row.effective_from) <= getdate(today()):
				current = row
		self.contribution_amount = flt(current.amount)
		self.contribution_frequency = current.frequency

	def on_trash(self):
		for doctype in ("Coop Contribution", "Coop Loan", "Coop Loan Repayment", "Coop Withdrawal"):
			if frappe.db.exists(doctype, {"cooperative": self.name}):
				frappe.throw(_("Cannot delete a cooperative that has financial records"))
		for doctype in ("Coop Member", "Coop Announcement"):
			frappe.db.delete(doctype, {"cooperative": self.name})
