# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today

from coopvault import postings
from coopvault.ledger import cooperative_totals, member_balances, refresh_all
from coopvault.utils import money_text

TRANSITIONS = {
	"Pending": {"Sent", "Rejected", "Cancelled"},
	"Sent": {"Completed"},
}


class CoopWithdrawal(Document):
	def validate(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Withdrawal amount must be greater than zero"))
		member = frappe.db.get_value("Coop Member", self.member, ["cooperative", "status"], as_dict=True)
		if not member or member.cooperative != self.cooperative:
			frappe.throw(_("Member does not belong to this cooperative"))
		currency = frappe.db.get_value("Cooperative", self.cooperative, "currency")

		previous = None if self.is_new() else self.get_doc_before_save().status
		if previous and previous != self.status and self.status not in TRANSITIONS.get(previous, ()):
			frappe.throw(_("A {0} withdrawal can't be changed to {1}").format(_(previous).lower(), _(self.status).lower()))

		if self.is_new() or (self.status == "Sent" and previous == "Pending"):
			b = member_balances(self.member, exclude_withdrawal=None if self.is_new() else self.name)
			if flt(self.amount) > b.withdrawable + 0.005:
				frappe.throw(
					_("You can take out at most {0}. Savings that back a loan stay in the pool until it's repaid.").format(
						money_text(b.withdrawable, currency)
					)
				)
		if self.status == "Sent" and previous == "Pending":
			pool = cooperative_totals(self.cooperative, exclude_withdrawal=self.name).pool_balance
			if flt(self.amount) > pool + 0.005:
				frappe.throw(_("The pool only has {0} right now").format(money_text(pool, currency)))
			if not (self.transfer_reference or "").strip():
				frappe.throw(_("Enter the transfer reference from your bank app"))
			payout = frappe.db.get_value(
				"Coop Member", self.member, ["payout_bank_name", "payout_account_number", "payout_account_name"],
				as_dict=True,
			)
			if not payout.payout_account_number:
				frappe.throw(_("This member hasn't added a bank account to receive the money"))
			self.update(payout)
			self.sent_on = today()
			self.reviewed_by = frappe.session.user
		if self.status == "Completed" and previous == "Sent":
			self.received_on = self.received_on or today()

	def on_update(self):
		postings.withdrawal(self)
		refresh_all(member=self.member, cooperative=self.cooperative)
