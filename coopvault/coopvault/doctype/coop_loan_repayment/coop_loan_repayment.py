# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from coopvault import loans, postings
from coopvault.ledger import refresh_all
from coopvault.utils import money_text


class CoopLoanRepayment(Document):
	def validate(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Repayment amount must be greater than zero"))
		loan = frappe.db.get_value(
			"Coop Loan",
			self.loan,
			["status", "balance", "cooperative", "member", "member_name", "user"],
			as_dict=True,
		)
		self.cooperative, self.member, self.member_name, self.user = (
			loan.cooperative,
			loan.member,
			loan.member_name,
			loan.user,
		)
		if self.status == "Confirmed" and (self.is_new() or self.has_value_changed("status")):
			if loan.status != "Active":
				frappe.throw(_("Repayments can only be made against an active loan"))
			if flt(self.amount) > flt(loan.balance) + 0.005:
				currency = frappe.db.get_value("Cooperative", loan.cooperative, "currency")
				frappe.throw(
					_("Repayment exceeds the outstanding loan balance of {0}").format(
						money_text(loan.balance, currency)
					)
				)
			if frappe.db.exists("Coop Loan Instalment", {"parent": self.loan}):
				# Oldest instalment first; within it penalty, then interest, then principal
				parts = loans.allocate(self.loan, self.amount, exclude=None if self.is_new() else self.name)
				self.penalty_part, self.interest_part, self.principal_part = parts.penalty, parts.interest, parts.principal
		if self.has_value_changed("status") and self.status in ("Confirmed", "Rejected", "Reversed"):
			self.reviewed_by = self.reviewed_by or frappe.session.user
			self.reviewed_on = now_datetime()

	def on_update(self):
		postings.repayment(self)
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.loan)

	def on_trash(self):
		if self.status in ("Confirmed", "Reversed"):
			frappe.throw(_("Confirmed repayments cannot be deleted"))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.loan)
