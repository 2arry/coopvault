"""Double-entry books for each cooperative.

Every money event posts a balanced journal entry (debits = credits). Posted entries are never
edited or deleted: a mistake is put right with a linked reversal entry. Balances the app shows
(a member's savings, the cash available to lend, the year's surplus) are read from the journal
lines, so the app and the books can't disagree.

The accounts the app posts to are found by their purpose (account_type) and are created with
the cooperative (ensure_chart). Member savings and loans carry the member on every line, which
gives each member their own sub-ledger.

Money in:   Dr Bank (or Paystack balance)          Cr Members' savings / Loans to members + Loan interest
Money out:  Dr Members' savings / Loans to members Cr Bank
Fees:       Dr Payment processing charges          Cr Bank   (what Paystack kept)
"""

import frappe
from frappe import _
from frappe.utils import flt, formatdate, getdate, today

CASH_TYPES = ("Bank", "Cash", "Paystack Clearing")
DEBIT_NORMAL = ("Asset", "Expense")
POSTED = ("Posted", "Reversed")  # both still count: a reversal entry cancels the original

# code, name, root type, purpose
CHART = [
	("1100", "Bank account", "Asset", "Bank"),
	("1110", "Cash at hand", "Asset", "Cash"),
	("1120", "Paystack balance (awaiting settlement)", "Asset", "Paystack Clearing"),
	("1200", "Loans to members", "Asset", "Loan Receivable"),
	("2100", "Members' savings", "Liability", "Member Savings"),
	("2200", "Unallocated receipts", "Liability", "Unallocated Receipts"),
	("3100", "Reserve fund", "Equity", "Reserve Fund"),
	("3200", "Retained surplus", "Equity", "Retained Surplus"),
	("3300", "Opening balance equity", "Equity", "Opening Balance Equity"),
	("4100", "Loan interest", "Income", "Interest Income"),
	("4200", "Late payment penalties", "Income", "Penalty Income"),
	("4300", "Fees received", "Income", "Fee Income"),
	("4900", "Other income", "Income", "Other Income"),
	("5100", "Payment processing charges", "Expense", "Payment Charges"),
	("5200", "Bank charges", "Expense", "Bank Charges"),
	("5300", "Running costs", "Expense", "Operating Expense"),
]


# Chart of accounts ----------------------------------------------------------------------


def ensure_chart(cooperative):
	"""Create the system accounts a cooperative needs (idempotent)."""
	have = set(
		frappe.get_all("Coop Account", filters={"cooperative": cooperative, "is_system": 1}, pluck="account_type")
	)
	for code, name, root, kind in CHART:
		if kind in have:
			continue
		frappe.get_doc(
			{
				"doctype": "Coop Account",
				"cooperative": cooperative,
				"account_code": code,
				"account_name": name,
				"root_type": root,
				"account_type": kind,
				"is_system": 1,
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Cooperative", cooperative, "chart_ready", 1, update_modified=False)


def account(cooperative, kind):
	"""The system account for a purpose, e.g. account(coop, "Member Savings")."""
	cache = getattr(frappe.local, "coop_accounts", None)
	if cache is None:
		cache = frappe.local.coop_accounts = {}
	key = (cooperative, kind)
	if key not in cache:
		filters = {"cooperative": cooperative, "account_type": kind, "is_system": 1}
		name = frappe.db.get_value("Coop Account", filters)
		if not name:
			ensure_chart(cooperative)
			name = frappe.db.get_value("Coop Account", filters)
		cache[key] = name
	return cache[key]


# Posting --------------------------------------------------------------------------------


def books_closed_until(cooperative):
	value = frappe.db.get_value("Cooperative", cooperative, "books_closed_until")
	return getdate(value) if value else None


def make_entry(
	cooperative,
	voucher_type,
	lines,
	posting_date=None,
	reference=None,
	member=None,
	remarks=None,
	status="Posted",
	system=True,
	reverses=None,
):
	"""Insert a journal entry. lines: [{"type" or "account", "debit", "credit", "member", "loan", "remarks"}].

	System postings (from payments, loans, withdrawals) never land in a closed period: if their
	date is on or before the lock date they're dated today instead. Manual journals are refused.
	"""
	posting_date = getdate(posting_date or today())
	locked = books_closed_until(cooperative)
	if locked and posting_date <= locked:
		if not system:
			frappe.throw(_("The books are closed up to {0}. Choose a later date.").format(formatdate(locked, "d MMM yyyy")))
		posting_date = max(getdate(today()), getdate(frappe.utils.add_days(locked, 1)))

	rows = []
	for line in lines:
		debit, credit = flt(line.get("debit"), 2), flt(line.get("credit"), 2)
		# A negative amount on one side is the same amount on the other side
		if debit < 0:
			debit, credit = 0.0, flt(credit - debit, 2)
		if credit < 0:
			debit, credit = flt(debit - credit, 2), 0.0
		if not debit and not credit:
			continue
		rows.append(
			{
				"account": line.get("account") or account(cooperative, line["type"]),
				"debit": debit,
				"credit": credit,
				"member": line.get("member"),
				"loan": line.get("loan"),
				"remarks": line.get("remarks"),
			}
		)
	if not rows:
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Coop Journal Entry",
			"cooperative": cooperative,
			"posting_date": posting_date,
			"voucher_type": voucher_type,
			"status": status,
			"reference_doctype": reference[0] if reference else None,
			"reference_name": reference[1] if reference else None,
			"member": member,
			"remarks": remarks,
			"lines": rows,
			"prepared_by": frappe.session.user,
			"reverses": reverses,
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.system = system
	doc.insert()
	return doc


def active_entry(reference_doctype, reference_name, voucher_type):
	"""The entry currently in effect for a document (posted and not reversed)."""
	return frappe.db.get_value(
		"Coop Journal Entry",
		{"reference_doctype": reference_doctype, "reference_name": reference_name, "voucher_type": voucher_type,
			"status": "Posted"},
		"name",
	)


def reverse(name, reason):
	"""Cancel a posted entry with an equal and opposite one, dated today. Returns the reversal."""
	je = frappe.get_doc("Coop Journal Entry", name)
	if je.status != "Posted":
		return frappe.get_doc("Coop Journal Entry", je.reversed_by) if je.reversed_by else None
	rev = make_entry(
		je.cooperative,
		"Reversal",
		[
			{"account": l.account, "debit": l.credit, "credit": l.debit, "member": l.member, "loan": l.loan,
				"remarks": l.remarks}
			for l in je.lines
		],
		reference=(je.reference_doctype, je.reference_name) if je.reference_doctype else None,
		member=je.member,
		remarks=_("Reverses {0}: {1}").format(je.name, reason),
		reverses=je.name,
	)
	je.db_set({"status": "Reversed", "reversed_by": rev.name, "reversal_reason": reason})
	return rev


def reverse_for(reference_doctype, reference_name, reason, voucher_types=None):
	for name in frappe.get_all(
		"Coop Journal Entry",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": "Posted",
			"voucher_type": ["in", voucher_types] if voucher_types else ["!=", "Reversal"],
		},
		pluck="name",
	):
		reverse(name, reason)


# Balances -------------------------------------------------------------------------------


def _totals(where, values):
	row = frappe.db.sql(
		f"""select coalesce(sum(debit), 0), coalesce(sum(credit), 0)
		from `tabCoop Journal Line` where is_posted = 1 and {where}""",
		values,
	)
	return flt(row[0][0], 2), flt(row[0][1], 2)


def _date_clause(values, upto=None, after=None):
	clause = ""
	if upto:
		clause += " and posting_date <= %(upto)s"
		values["upto"] = getdate(upto)
	if after:
		clause += " and posting_date > %(after)s"
		values["after"] = getdate(after)
	return clause


def member_savings(member, upto=None):
	"""What the cooperative owes a member: their Members' savings sub-ledger."""
	values = {"member": member}
	debit, credit = _totals("member = %(member)s and account_type = 'Member Savings'" + _date_clause(values, upto), values)
	return flt(credit - debit, 2)


def cash_balance(cooperative, upto=None):
	"""Money the cooperative actually holds: bank, cash and Paystack balance awaiting settlement."""
	return type_balance(cooperative, CASH_TYPES, upto)


def type_balance(cooperative, kinds, upto=None, after=None):
	"""Debit-minus-credit balance of the accounts with these purposes."""
	values = {"cooperative": cooperative, "kinds": tuple(kinds)}
	debit, credit = _totals(
		"cooperative = %(cooperative)s and account_type in %(kinds)s" + _date_clause(values, upto, after), values
	)
	return flt(debit - credit, 2)


def loan_position(loan):
	"""Principal still owed and interest received so far on one loan, from the books."""
	values = {"loan": loan}
	debit, credit = _totals("loan = %(loan)s and account_type = 'Loan Receivable'", values)
	idebit, icredit = _totals("loan = %(loan)s and account_type = 'Interest Income'", values)
	return frappe._dict(principal_outstanding=flt(debit - credit, 2), interest_received=flt(icredit - idebit, 2))


def account_balances(cooperative, from_date=None, to_date=None):
	"""Every account with its opening balance, movements in the period and closing balance.
	Balances are signed the natural way for the account (assets/expenses debit, others credit)."""
	accounts = frappe.get_all(
		"Coop Account",
		filters={"cooperative": cooperative},
		fields=["name", "account_code", "account_name", "root_type", "account_type", "is_system", "disabled"],
		order_by="account_code asc",
	)
	values = {"cooperative": cooperative}
	period = ""
	if from_date:
		values["from_date"] = getdate(from_date)
	if to_date:
		values["to_date"] = getdate(to_date)
		period += " and posting_date <= %(to_date)s"
	rows = frappe.db.sql(
		f"""select account,
			coalesce(sum(case when {'posting_date < %(from_date)s' if from_date else '0'} then debit - credit else 0 end), 0) as opening,
			coalesce(sum(case when {'posting_date >= %(from_date)s' if from_date else '1'} then debit else 0 end), 0) as debit,
			coalesce(sum(case when {'posting_date >= %(from_date)s' if from_date else '1'} then credit else 0 end), 0) as credit
		from `tabCoop Journal Line`
		where is_posted = 1 and cooperative = %(cooperative)s {period}
		group by account""",
		values,
		as_dict=True,
	)
	by_account = {r.account: r for r in rows}
	out = []
	for a in accounts:
		r = by_account.get(a.name) or frappe._dict(opening=0, debit=0, credit=0)
		sign = 1 if a.root_type in DEBIT_NORMAL else -1
		opening = flt(r.opening, 2)
		closing = flt(opening + flt(r.debit) - flt(r.credit), 2)
		out.append(
			frappe._dict(
				a,
				opening=flt(sign * opening, 2),
				debit=flt(r.debit, 2),
				credit=flt(r.credit, 2),
				closing=flt(sign * closing, 2),
				net_debit=closing,
			)
		)
	return out
