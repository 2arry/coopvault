# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today

from coopvault import postings, savings
from coopvault.ledger import cooperative_totals, member_balances, refresh_all
from coopvault.utils import money_text

TRANSITIONS = {
	"Pending": {"Approved", "Rejected", "Cancelled"},
	"Approved": {"Sent", "Rejected", "Cancelled"},
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

		if self.leaving:
			self.savings_product = None
		else:
			self.savings_product = savings.get(self.cooperative, self.savings_product).name
		if self.is_new() or (self.status in ("Approved", "Sent") and previous in ("Pending", "Approved")):
			self.check_available(currency)
		if self.status == "Sent" and previous == "Approved":
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

	def check_available(self, currency):
		b = member_balances(self.member, exclude_withdrawal=None if self.is_new() else self.name)
		if self.leaving:
			if b.exposure + b.pledged > 0.005:
				frappe.throw(_("Repay your loans (and be released from any guarantees) before taking everything out"))
			if flt(self.amount) > b.leaving_amount + 0.005:
				frappe.throw(_("You have {0} to take out").format(money_text(b.leaving_amount, currency)))
			return
		row = next((r for r in b.products if r.product == self.savings_product), None)
		if row and row.locked:
			frappe.throw(_("Savings in {0} can only be taken out when you leave the group").format(row.product_name))
		available = row.withdrawable if row else 0.0
		if flt(self.amount) > available + 0.005:
			frappe.throw(
				_("You can take out at most {0} from {1}. Savings that back a loan or a guarantee stay in the pool until it's repaid.").format(
					money_text(available, currency), row.product_name if row else _("savings")
				)
			)

	def on_update(self):
		postings.withdrawal(self)
		refresh_all(member=self.member, cooperative=self.cooperative)
