"""Approval rules for every cooperative, approval requests for anything already waiting, and member
numbers for existing members (in the order they joined). Idempotent."""

import frappe
from frappe import _

from coopvault import approvals
from coopvault.api.coop import next_member_number


def execute():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		approvals.ensure_default_rules(coop)

	def ask(cooperative, action, reference, requested_by, amount=0, beneficiary=None, summary=None):
		approvals.request(cooperative, action, reference, amount=amount, beneficiary=beneficiary, summary=summary,
			requested_by=requested_by, notify=False)

	for m in frappe.get_all("Coop Member", filters={"status": "Pending"}, fields=["name", "cooperative", "user", "full_name"]):
		ask(m.cooperative, "Member admission", ("Coop Member", m.name), m.user,
			summary=_("{0} wants to join").format(m.full_name))
	for l in frappe.get_all("Coop Loan", filters={"status": "Pending"},
			fields=["name", "cooperative", "user", "member", "member_name", "principal"]):
		ask(l.cooperative, "Loan", ("Coop Loan", l.name), l.user, l.principal, l.member,
			_("{0} asked to borrow {1}").format(l.member_name, frappe.utils.fmt_money(l.principal)))
	for w in frappe.get_all("Coop Withdrawal", filters={"status": "Pending"},
			fields=["name", "cooperative", "user", "member", "member_name", "amount"]):
		ask(w.cooperative, "Withdrawal", ("Coop Withdrawal", w.name), w.user, w.amount, w.member,
			_("{0} asked to withdraw {1}").format(w.member_name, frappe.utils.fmt_money(w.amount)))
	for j in frappe.get_all("Coop Journal Entry", filters={"status": "Draft"},
			fields=["name", "cooperative", "prepared_by", "total_debit", "remarks"]):
		ask(j.cooperative, "Manual journal", ("Coop Journal Entry", j.name), j.prepared_by, j.total_debit,
			summary=_("Journal: {0}").format(j.remarks))
	for so in frappe.get_all("Coop Share Out", filters={"status": "Draft"},
			fields=["name", "cooperative", "prepared_by", "to_members"]):
		ask(so.cooperative, "Share-out", ("Coop Share Out", so.name), so.prepared_by, so.to_members,
			summary=_("Year-end share-out"))

	for m in frappe.get_all("Coop Member", filters={"status": ["!=", "Pending"], "member_number": ["is", "not set"]},
			fields=["name", "cooperative"], order_by="joined_on asc, creation asc"):
		frappe.db.set_value("Coop Member", m.name, "member_number", next_member_number(m.cooperative),
			update_modified=False)
