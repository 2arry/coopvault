# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, cint, flt, getdate, now_datetime, today

from coopvault import loans, postings
from coopvault.ledger import cooperative_totals, member_balances, refresh_all
from coopvault.utils import money_text

# Allowed status changes. Sent = money transferred, waiting for the borrower to confirm it arrived.
TRANSITIONS = {
	"Pending": {"Approved", "Rejected", "Cancelled"},
	"Approved": {"Sent", "Rejected", "Cancelled"},
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
			self.product = self.product or loans.ensure_default_product(self.cooperative)
			product = frappe.get_cached_doc("Coop Loan Product", self.product)
			if product.cooperative != self.cooperative or not product.enabled:
				frappe.throw(_("Choose one of the group's loan products"))
			self.tenure_months = max(1, self.tenure_months or 1)
			if not cint(product.min_tenure_months) <= self.tenure_months <= cint(product.max_tenure_months):
				frappe.throw(_("{0} is for {1} to {2} months").format(product.product_name, product.min_tenure_months,
					product.max_tenure_months))
			if self.is_new():
				self.check_eligibility(product, coop)
				self.check_guarantors(product, coop)
			loans.apply_product(self)

		if self.status in ("Pending", "Approved", "Sent") and previous in (None, "Pending", "Approved"):
			self.check_borrow_limit(coop)
		if self.status == "Sent" and previous == "Approved":
			self.send(coop)
		if self.status == "Active" and previous == "Sent":
			self.received_on = self.received_on or today()
			if self.product:
				loans.start_schedule(self)
			else:
				self.due_date = add_months(getdate(self.received_on), self.tenure_months or 1)

		self.amount_repaid = flt(self.amount_repaid)
		self.balance = max(0.0, flt(self.total_payable) + flt(self.penalties_charged) - self.amount_repaid)

		if self.has_value_changed("status") and self.status in ("Sent", "Rejected"):
			self.reviewed_by = frappe.session.user
			self.reviewed_on = now_datetime()

	def check_eligibility(self, product, coop):
		result = loans.eligibility(self.member, product.name, self.principal, self.tenure_months)
		failed = [c["label"] for c in result.checks if not c["ok"] and c["key"] not in ("amount", "payout")]
		if failed:
			frappe.throw(_("Not eligible for {0} yet: {1}").format(product.product_name, "; ".join(failed)),
				title=_("Not eligible"))

	def check_guarantors(self, product, coop):
		"""Guarantors are other active members who promise part of their own savings."""
		needed = cint(product.guarantors_required)
		if len(self.guarantors) < needed:
			frappe.throw(_("{0} needs {1} guarantor(s)").format(product.product_name, needed))
		seen = set()
		for g in self.guarantors:
			m = frappe.db.get_value("Coop Member", g.member, ["name", "cooperative", "status", "full_name", "user"], as_dict=True)
			if not m or m.cooperative != self.cooperative or m.status != "Active":
				frappe.throw(_("Guarantors must be active members of the group"))
			if g.member == self.member or g.member in seen:
				frappe.throw(_("Choose different people as guarantors (not yourself)"))
			seen.add(g.member)
			g.member_name, g.user, g.status = m.full_name, m.user, "Pending"
			if flt(g.amount) <= 0:
				frappe.throw(_("Say how much {0} is guaranteeing").format(m.full_name))
			free = member_balances(g.member).pledgeable
			if flt(g.amount) > free + 0.005:
				frappe.throw(_("{0} can guarantee at most {1} right now").format(m.full_name, money_text(free, coop.currency)))
		if needed and sum(flt(g.amount) for g in self.guarantors) + 0.005 < flt(self.principal) * flt(product.guarantee_percent) / 100:
			frappe.throw(_("Guarantors need to cover at least {0} of the loan").format(
				money_text(flt(self.principal) * flt(product.guarantee_percent) / 100, coop.currency)))

	def check_borrow_limit(self, coop):
		b = member_balances(self.member, exclude_loan=None if self.is_new() else self.name)
		multiplier = flt(frappe.db.get_value("Coop Loan Product", self.product, "savings_multiplier")) if self.product else b.multiplier
		b.borrow_limit = max(0.0, (b.lendable - b.pledged) * multiplier - b.exposure)
		if flt(self.principal) > b.borrow_limit + 0.005:
			frappe.throw(
				_(
					"Loan of {0} exceeds the borrowing limit. This member has {1} in savings and can borrow at most {2} right now."
				).format(
					money_text(self.principal, coop.currency),
					money_text(b.savings, coop.currency),
					money_text(b.borrow_limit, coop.currency),
				),
				title=_("Borrowing limit exceeded"),
			)

	def send(self, coop):
		pool = cooperative_totals(self.cooperative, exclude_loan=self.name).pool_balance
		if flt(self.principal) > pool + 0.005:
			frappe.throw(
				_("The cooperative pool only has {0} available. This loan cannot be sent yet.").format(
					money_text(pool, coop.currency)
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

	@property
	def amount_to_send(self):
		"""What the borrower receives: the loan less any processing fee."""
		return flt(flt(self.principal) - flt(self.processing_fee), 2)

	def on_update(self):
		postings.loan(self)
		refresh_all(member=self.member, cooperative=self.cooperative, loan=self.name)

	def on_trash(self):
		if self.status not in ("Pending", "Rejected", "Cancelled"):
			frappe.throw(_("Only pending, rejected or cancelled loans can be deleted"))

	def after_delete(self):
		refresh_all(member=self.member, cooperative=self.cooperative)
