# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, flt, fmt_money, getdate, now_datetime, today

from coopvault.ledger import cooperative_totals, member_balances, refresh_all

# Allowed status changes. Sent = money transferred, waiting for the borrower to confirm it arrived.
TRANSITIONS = {
	"Pending": {"Sent", "Rejected", "Cancelled"},
	"Sent": {"Active"},
	"Active": {"Repaid"},
	"Repaid": {"Active"},
}


class CoopLoan(Document):
	def validate(self):
		if flt(self.principal) <= 0:
			frappe.throw(_("Loan amount must be greater than zero"))

		coop = frappe.get_cached_doc("Cooperative", self.cooperative)
		member = frappe.db.get_value("Coop Member", self.member, ["cooperative", "status"], as_dict=True)
		if not member or member.cooperative != self.cooperative:
			frappe.throw(_("Member does not belong to this cooperative"))

		previous = None if self.is_new() else self.get_doc_before_save().status
		if previous and previous != self.status and self.status not in TRANSITIONS.get(previous, ()):
			frappe.throw(_("A {0} loan can't be changed to {1}").format(_(previous).lower(), _(self.status).lower()))

		if self.status == "Pending":
			if member.status != "Active":
				frappe.throw(_("Only active members can borrow"))
			self.tenure_months = max(1, self.tenure_months or 1)
			if self.tenure_months > (coop.max_loan_tenure_months or 12):
				frappe.throw(_("Maximum loan tenure is {0} months").format(coop.max_loan_tenure_months))
			self.interest_rate = flt(coop.interest_rate)
			self.interest_amount = flt(self.principal) * flt(self.interest_rate) / 100
			self.total_payable = flt(self.principal) + flt(self.interest_amount)

		if self.status in ("Pending", "Sent") and previous in (None, "Pending"):
			self.check_borrow_limit(coop)
		if self.status == "Sent" and previous == "Pending":
			self.send(coop)
		if self.status == "Active" and previous == "Sent":
			self.received_on = self.received_on or today()
			self.due_date = add_months(getdate(self.received_on), self.tenure_months or 1)

		self.amount_repaid = flt(self.amount_repaid)
		self.balance = max(0.0, flt(self.total_payable) - self.amount_repaid)

		if self.has_value_changed("status") and self.status in ("Sent", "Rejected"):
			self.reviewed_by = frappe.session.user
			self.reviewed_on = now_datetime()

	def check_borrow_limit(self, coop):
		b = member_balances(self.member, exclude_loan=None if self.is_new() else self.name)
		if flt(self.principal) > b.borrow_limit + 0.005:
			frappe.throw(
				_(
					"Loan of {0} exceeds the borrowing limit. This member has {1} in savings and can borrow at most {2} right now."
				).format(
					fmt_money(self.principal, currency=coop.currency),
					fmt_money(b.savings, currency=coop.currency),
					fmt_money(b.borrow_limit, currency=coop.currency),
				),
				title=_("Borrowing limit exceeded"),
			)

	def send(self, coop):
		pool = cooperative_totals(self.cooperative, exclude_loan=self.name).pool_balance
		if flt(self.principal) > pool + 0.005:
			frappe.throw(
				_("The cooperative pool only has {0} available. This loan cannot be sent yet.").format(
					fmt_money(pool, currency=coop.currency)
				)
			)
		if not (self.transfer_reference or "").strip():
			frappe.throw(_("Enter the transfer reference from your bank app"))
		payout = frappe.db.get_value(
			"Coop Member", self.member, ["payout_bank_name", "payout_account_number", "payout_account_name"], as_dict=True
		)
		if not payout.payout_account_number:
			frappe.throw(_("This member hasn't added the bank account to send the loan to"))
		self.update(payout)
		self.disbursed_on = today()

	def on_update(self):
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.name)

	def on_trash(self):
		if self.status not in ("Pending", "Rejected", "Cancelled"):
			frappe.throw(_("Only pending, rejected or cancelled loans can be deleted"))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative)
