# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, now_datetime

from coopvault.ledger import refresh_all


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

		becoming_confirmed = self.status == "Confirmed" and (
			self.is_new() or self.get_doc_before_save().status != "Confirmed"
		)
		if self.is_new() or becoming_confirmed:
			if loan.status != "Active":
				frappe.throw(_("Repayments can only be made against an active loan"))
			if flt(self.amount) > flt(loan.balance) + 0.005:
				currency = frappe.db.get_value("Cooperative", loan.cooperative, "currency")
				frappe.throw(
					_("Repayment exceeds the outstanding loan balance of {0}").format(
						fmt_money(loan.balance, currency=currency)
					)
				)

		if self.has_value_changed("status") and self.status in ("Confirmed", "Rejected"):
			self.reviewed_by = self.reviewed_by or frappe.session.user
			self.reviewed_on = now_datetime()

	def on_update(self):
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.loan)

	def on_trash(self):
		if self.status == "Confirmed":
			frappe.throw(_("Confirmed repayments cannot be deleted. Reject it instead."))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.loan)
