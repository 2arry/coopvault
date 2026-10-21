# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, formatdate, getdate

from coopvault.accounting import books_closed_until

# Once posted, only these can change (and only through the approval/reversal flows)
MUTABLE = {"status", "approved_by", "approved_on", "reversed_by", "reversal_reason", "modified", "modified_by"}
STATUS_FLOW = {"Draft": {"Posted", "Rejected"}, "Posted": {"Reversed"}}


class CoopJournalEntry(Document):
	def validate(self):
		before = None if self.is_new() else self.get_doc_before_save()
		if before:
			self.check_unchanged(before)
		accounts = {}
		for line in self.lines:
			if line.account not in accounts:
				accounts[line.account] = frappe.db.get_value(
					"Coop Account", line.account, ["cooperative", "account_type", "root_type", "disabled"], as_dict=True
				)
			a = accounts[line.account]
			if not a or a.cooperative != self.cooperative:
				frappe.throw(_("Account {0} doesn't belong to this cooperative").format(line.account))
			if a.disabled and self.is_new():
				frappe.throw(_("Account {0} is disabled").format(line.account))
			line.debit, line.credit = flt(line.debit, 2), flt(line.credit, 2)
			if line.debit < 0 or line.credit < 0 or (line.debit and line.credit):
				frappe.throw(_("Each line is either a debit or a credit, and never negative"))
			if a.account_type in ("Member Savings", "Loan Receivable") and not line.member:
				frappe.throw(_("Lines on {0} must say which member they're for").format(line.account))
			line.account_type, line.root_type = a.account_type, a.root_type
			line.cooperative, line.posting_date = self.cooperative, self.posting_date
			line.is_posted = 1 if self.status in ("Posted", "Reversed") else 0
		self.total_debit = flt(sum(l.debit for l in self.lines), 2)
		self.total_credit = flt(sum(l.credit for l in self.lines), 2)
		if not self.lines or not self.total_debit:
			frappe.throw(_("A journal entry needs at least one debit and one credit"))
		if abs(self.total_debit - self.total_credit) >= 0.005:
			frappe.throw(_("Debits ({0}) and credits ({1}) must be equal").format(self.total_debit, self.total_credit))

		becoming_posted = self.status == "Posted" and (not before or before.status != "Posted")
		locked = books_closed_until(self.cooperative)
		if becoming_posted and locked and getdate(self.posting_date) <= locked and not self.flags.system:
			frappe.throw(_("The books are closed up to {0}").format(formatdate(locked, "d MMM yyyy")))

	def check_unchanged(self, before):
		if self.status != before.status and self.status not in STATUS_FLOW.get(before.status, ()):
			frappe.throw(_("A {0} entry can't become {1}").format(before.status.lower(), self.status.lower()))
		if before.status == "Draft" and self.status == "Draft":
			return  # drafts can still be corrected before approval
		for field in self.meta.fields:
			if field.fieldtype == "Table" or field.fieldname in MUTABLE:
				continue
			if str(self.get(field.fieldname) or "") != str(before.get(field.fieldname) or ""):
				frappe.throw(_("Posted entries can't be changed. Post a reversal instead."))
		key = lambda d: [(l.account, flt(l.debit, 2), flt(l.credit, 2), l.member or "", l.loan or "") for l in d.lines]
		if key(self) != key(before):
			frappe.throw(_("Posted entries can't be changed. Post a reversal instead."))

	def on_trash(self):
		if self.status != "Draft":
			frappe.throw(_("Journal entries are permanent. Post a reversal instead."))
