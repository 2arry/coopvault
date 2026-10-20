# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, flt, fmt_money, getdate, now_datetime, today

from coopvault.ledger import cooperative_totals, member_balances, refresh_all


class CoopLoan(Document):
	def validate(self):
		if flt(self.principal) <= 0:
			frappe.throw(_("Loan amount must be greater than zero"))

		coop = frappe.get_cached_doc("Cooperative", self.cooperative)
		member = frappe.db.get_value("Coop Member", self.member, ["cooperative", "status"], as_dict=True)
		if not member or member.cooperative != self.cooperative:
			frappe.throw(_("Member does not belong to this cooperative"))

		if self.is_new() or self.status == "Pending":
			if member.status != "Active":
				frappe.throw(_("Only active members can borrow"))
			self.tenure_months = max(1, self.tenure_months or 1)
			if self.tenure_months > (coop.max_loan_tenure_months or 12):
				frappe.throw(_("Maximum loan tenure is {0} months").format(coop.max_loan_tenure_months))
			self.interest_rate = flt(coop.interest_rate)
			self.interest_amount = flt(self.principal) * flt(self.interest_rate) / 100
			self.total_payable = flt(self.principal) + flt(self.interest_amount)

		previous_status = self.get_doc_before_save().status if not self.is_new() else None
		if self.status in ("Pending", "Active") and previous_status in (None, "Pending"):
			self.check_borrow_limit(coop)
		if self.status == "Active" and previous_status == "Pending":
			self.disburse(coop)

		self.amount_repaid = flt(self.amount_repaid)
		self.balance = max(0.0, flt(self.total_payable) - self.amount_repaid)

		if self.has_value_changed("status") and self.status in ("Active", "Rejected"):
			self.reviewed_by = frappe.session.user
			self.reviewed_on = now_datetime()

	def check_borrow_limit(self, coop):
		b = member_balances(self.member, exclude_loan=None if self.is_new() else self.name)
		if flt(self.principal) > b.borrow_limit + 0.005:
			frappe.throw(
				_(
					"Loan of {0} exceeds the borrowing limit. This member has contributed {1} and can borrow at most {2} right now."
				).format(
					fmt_money(self.principal, currency=coop.currency),
					fmt_money(b.total_contributed, currency=coop.currency),
					fmt_money(b.borrow_limit, currency=coop.currency),
				),
				title=_("Borrowing limit exceeded"),
			)

	def disburse(self, coop):
		pool = cooperative_totals(self.cooperative, exclude_loan=self.name).pool_balance
		if flt(self.principal) > pool + 0.005:
			frappe.throw(
				_("The cooperative pool only has {0} available. This loan cannot be disbursed yet.").format(
					fmt_money(pool, currency=coop.currency)
				)
			)
		self.disbursed_on = self.disbursed_on or today()
		self.due_date = add_months(getdate(self.disbursed_on), self.tenure_months or 1)

	def on_update(self):
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.name)

	def on_trash(self):
		if self.status not in ("Pending", "Rejected", "Cancelled"):
			frappe.throw(_("Only pending, rejected or cancelled loans can be deleted"))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative)
