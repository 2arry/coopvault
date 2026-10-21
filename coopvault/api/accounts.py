"""The books: chart of accounts, journals, financial reports, closing and the year-end share-out.

Staff (Admins and the Treasurer) read the books. Manual journals are prepared by one staff member
and only count once a different one approves them. Closing the books and approving a share-out
are for Admins, and the person who prepared a share-out can't approve it.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, formatdate, getdate, now_datetime, today

from coopvault import accounting
from coopvault.accounting import CASH_TYPES, DEBIT_NORMAL
from coopvault.api import get_membership
from coopvault.ledger import refresh_all
from coopvault.notify import members_of, notify
from coopvault.utils import money_text

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
	"November", "December"]


def _staff(cooperative):
	return get_membership(cooperative, admin=True)


def _currency(cooperative):
	return frappe.db.get_value("Cooperative", cooperative, "currency") or "NGN"


def fiscal_year(cooperative, on=None):
	"""(start, end) of the financial year containing a date."""
	on = getdate(on or today())
	month = MONTHS.index(frappe.db.get_value("Cooperative", cooperative, "fiscal_year_start") or "January") + 1
	start = on.replace(month=month, day=1)
	if start > on:
		start = start.replace(year=start.year - 1)
	return start, add_days(add_months(start, 12), -1)


def _period(cooperative, from_date=None, to_date=None):
	start, end = fiscal_year(cooperative)
	return getdate(from_date or start), getdate(to_date or min(getdate(end), getdate(today())))


def _sum_root(rows, root):
	return flt(sum(r.closing for r in rows if r.root_type == root), 2)


# Overview -------------------------------------------------------------------------------


@frappe.whitelist()
def summary(cooperative):
	_staff(cooperative)
	start, end = fiscal_year(cooperative)
	year = accounting.account_balances(cooperative, from_date=start, to_date=today())
	movement = lambda root: flt(sum((r.credit - r.debit) if root == "Income" else (r.debit - r.credit)
		for r in year if r.root_type == root), 2)
	kind = lambda *k: accounting.type_balance(cooperative, k)
	income, expenses = movement("Income"), movement("Expense")
	locked = accounting.books_closed_until(cooperative)
	first = frappe.db.sql("""select min(posting_date) from `tabCoop Journal Entry`
		where cooperative = %s and status in ('Posted', 'Reversed')""", cooperative)[0][0]
	share_from = add_days(locked, 1) if locked else first
	# Suggest sharing out last financial year if it has ended and isn't closed yet, else up to yesterday
	last_year_end = add_days(start, -1)
	suggested_end = last_year_end if share_from and getdate(last_year_end) >= getdate(share_from) else add_days(today(), -1)
	return {
		"currency": _currency(cooperative),
		"bank": kind("Bank"),
		"cash": kind("Cash"),
		"paystack": kind("Paystack Clearing"),
		"cash_total": accounting.cash_balance(cooperative),
		"loans_out": kind("Loan Receivable"),
		"members_savings": -kind("Member Savings"),
		"unallocated": -kind("Unallocated Receipts"),
		"reserve": -kind("Reserve Fund"),
		"retained": -kind("Retained Surplus"),
		"year_start": start,
		"year_end": end,
		"income": income,
		"expenses": expenses,
		"surplus": flt(income - expenses, 2),
		"books_closed_until": accounting.books_closed_until(cooperative),
		"drafts": frappe.db.count("Coop Journal Entry", {"cooperative": cooperative, "status": "Draft"}),
		"share_out_draft": frappe.db.get_value("Coop Share Out", {"cooperative": cooperative, "status": "Draft"}),
		"share_out_from": share_from,
		"suggested_share_out_end": suggested_end,
		"reserve_percent": flt(frappe.db.get_value("Cooperative", cooperative, "reserve_percent") or 25),
	}


# Chart of accounts ----------------------------------------------------------------------


@frappe.whitelist()
def chart(cooperative, as_of=None):
	_staff(cooperative)
	accounting.ensure_chart(cooperative)
	return {"currency": _currency(cooperative), "accounts": accounting.account_balances(cooperative, to_date=as_of)}


@frappe.whitelist(methods=["POST"])
def save_account(cooperative, account_name, root_type=None, account_code=None, account_type=None, description=None,
		name=None, disabled=0):
	get_membership(cooperative, owner=True)
	if name:
		doc = frappe.get_doc("Coop Account", name)
		if doc.cooperative != cooperative:
			frappe.throw(_("Account not found"))
	else:
		doc = frappe.new_doc("Coop Account")
		doc.cooperative = cooperative
		doc.root_type = root_type
		doc.account_code = account_code or _next_code(cooperative, root_type)
	doc.account_name = account_name
	if not doc.is_system:
		doc.account_type = account_type or None
		doc.disabled = cint(disabled)
		if root_type:
			doc.root_type = root_type
	if account_code:
		doc.account_code = account_code
	doc.description = description
	doc.save(ignore_permissions=True)
	return doc.as_dict()


def _next_code(cooperative, root_type):
	base = {"Asset": 1000, "Liability": 2000, "Equity": 3000, "Income": 4000, "Expense": 5000}[root_type]
	codes = [cint(c) for c in frappe.get_all("Coop Account", filters={"cooperative": cooperative}, pluck="account_code")
		if str(c).isdigit() and base <= cint(c) < base + 1000]
	return str(max(codes + [base]) + 10)


# Journals ---------------------------------------------------------------------------------

JOURNAL_FIELDS = ["name", "posting_date", "voucher_type", "status", "remarks", "total_debit", "reference_doctype",
	"reference_name", "member", "prepared_by", "approved_by", "reverses", "reversed_by", "creation"]


@frappe.whitelist()
def journals(cooperative, voucher_type=None, status=None, member=None, start=0, limit=50):
	_staff(cooperative)
	filters = {"cooperative": cooperative}
	if voucher_type:
		filters["voucher_type"] = voucher_type
	if status:
		filters["status"] = status
	if member:
		filters["member"] = member
	rows = frappe.get_all("Coop Journal Entry", filters=filters, fields=JOURNAL_FIELDS,
		order_by="posting_date desc, creation desc", start=cint(start), limit=min(cint(limit) or 50, 200))
	_add_names(rows)
	return rows


def _add_names(rows):
	users = {r.get("prepared_by") for r in rows} | {r.get("approved_by") for r in rows}
	names = dict(frappe.get_all("User", filters={"name": ["in", [u for u in users if u] or [""]]}, fields=["name", "full_name"],
		as_list=True))
	members = {r.get("member") for r in rows if r.get("member")}
	mnames = dict(frappe.get_all("Coop Member", filters={"name": ["in", list(members) or [""]]}, fields=["name", "full_name"],
		as_list=True))
	for r in rows:
		r["prepared_by_name"] = names.get(r.get("prepared_by"))
		r["approved_by_name"] = names.get(r.get("approved_by"))
		r["member_name"] = mnames.get(r.get("member"))


@frappe.whitelist()
def get_journal(name):
	je = frappe.get_doc("Coop Journal Entry", name)
	_staff(je.cooperative)
	data = {f: je.get(f) for f in JOURNAL_FIELDS + ["cooperative", "reversal_reason", "approved_on"]}
	_add_names([data])
	accounts = {a.name: a for a in frappe.get_all("Coop Account", filters={"cooperative": je.cooperative},
		fields=["name", "account_code", "account_name", "root_type"])}
	members = dict(frappe.get_all("Coop Member", filters={"cooperative": je.cooperative}, fields=["name", "full_name"],
		as_list=True))
	data["lines"] = [
		{"account": l.account, "account_code": accounts[l.account].account_code, "account_name": accounts[l.account].account_name,
			"debit": l.debit, "credit": l.credit, "member": l.member, "member_name": members.get(l.member), "loan": l.loan,
			"remarks": l.remarks}
		for l in je.lines
	]
	data["currency"] = _currency(je.cooperative)
	data["can_approve"] = je.status == "Draft" and je.prepared_by != frappe.session.user
	return data


def _lines(lines):
	lines = json.loads(lines) if isinstance(lines, str) else lines
	return [
		{"account": l.get("account"), "debit": flt(l.get("debit"), 2), "credit": flt(l.get("credit"), 2),
			"member": l.get("member") or None, "remarks": l.get("remarks")}
		for l in lines or []
	]


@frappe.whitelist(methods=["POST"])
def create_journal(cooperative, posting_date, remarks, lines):
	"""A manual journal (e.g. bank charges, an expense, a correction). It waits as a draft until a
	different staff member approves it."""
	me = _staff(cooperative)
	if not (remarks or "").strip():
		frappe.throw(_("Say what this entry is for"))
	doc = accounting.make_entry(cooperative, "Manual Journal", _lines(lines), posting_date=posting_date,
		remarks=remarks.strip(), status="Draft", system=False)
	if not doc:
		frappe.throw(_("Enter at least one debit and one credit"))
	notify([u for u in _staff_users(cooperative) if u != me.user], _("A journal entry needs approval"),
		_("{0}: {1}").format(me.full_name, remarks.strip()), link=f"/admin/books/journal/{doc.name}",
		cooperative=cooperative, kind="journal-approval")
	return get_journal(doc.name)


def _staff_users(cooperative):
	from coopvault.notify import staff_of

	return staff_of(cooperative)


@frappe.whitelist(methods=["POST"])
def approve_journal(name):
	je = frappe.get_doc("Coop Journal Entry", name)
	_staff(je.cooperative)
	if je.status != "Draft":
		frappe.throw(_("Only draft entries wait for approval"))
	if je.prepared_by == frappe.session.user:
		frappe.throw(_("Someone other than the person who prepared it has to approve it"), frappe.PermissionError)
	je.status = "Posted"
	je.approved_by = frappe.session.user
	je.approved_on = now_datetime()
	je.flags.ignore_permissions = True
	je.save()
	_refresh_members(je)
	notify(je.prepared_by, _("Your journal entry {0} was approved").format(je.name), link=f"/admin/books/journal/{je.name}",
		cooperative=je.cooperative, kind="journal-approval")
	return get_journal(name)


@frappe.whitelist(methods=["POST"])
def reject_journal(name, note=None):
	je = frappe.get_doc("Coop Journal Entry", name)
	_staff(je.cooperative)
	if je.status != "Draft":
		frappe.throw(_("Only draft entries can be rejected"))
	je.status = "Rejected"
	je.approved_by = frappe.session.user
	je.approved_on = now_datetime()
	je.reversal_reason = note
	je.flags.ignore_permissions = True
	je.save()
	notify(je.prepared_by, _("Your journal entry {0} was rejected").format(je.name), note,
		link=f"/admin/books/journal/{je.name}", cooperative=je.cooperative, kind="journal-approval")
	return get_journal(name)


@frappe.whitelist(methods=["POST"])
def reverse_journal(name, reason):
	"""Undo a manual journal. Automatic entries are undone by reversing the payment, loan or
	withdrawal that made them, so the documents and the books stay in step."""
	je = frappe.get_doc("Coop Journal Entry", name)
	_staff(je.cooperative)
	if je.voucher_type != "Manual Journal":
		frappe.throw(_("Only manual journals can be reversed here"))
	if not (reason or "").strip():
		frappe.throw(_("Say why it's being reversed"))
	accounting.reverse(name, reason.strip())
	_refresh_members(je)
	return get_journal(name)


def _refresh_members(je):
	for member in {l.member for l in je.lines if l.member}:
		refresh_all(member=member)
	refresh_all(cooperative=je.cooperative)


# Reports ----------------------------------------------------------------------------------


@frappe.whitelist()
def trial_balance(cooperative, from_date=None, to_date=None):
	_staff(cooperative)
	start, end = _period(cooperative, from_date, to_date)
	rows = [r for r in accounting.account_balances(cooperative, from_date=start, to_date=end)
		if r.opening or r.debit or r.credit or r.closing]
	for r in rows:
		r.closing_debit = max(0.0, r.net_debit)
		r.closing_credit = max(0.0, -r.net_debit)
	return {
		"currency": _currency(cooperative), "from_date": start, "to_date": end, "rows": rows,
		"total_debit": flt(sum(r.closing_debit for r in rows), 2),
		"total_credit": flt(sum(r.closing_credit for r in rows), 2),
		"period_debit": flt(sum(r.debit for r in rows), 2),
		"period_credit": flt(sum(r.credit for r in rows), 2),
	}


@frappe.whitelist()
def income_statement(cooperative, from_date=None, to_date=None):
	_staff(cooperative)
	start, end = _period(cooperative, from_date, to_date)
	rows = accounting.account_balances(cooperative, from_date=start, to_date=end)
	# Movements in the period only: share-out closing entries are left out so the year's real
	# income and costs still show after it has been closed
	closing = _closing_movements(cooperative, start, end)
	income = [dict(name=r.name, account_code=r.account_code, account_name=r.account_name,
		amount=flt(r.credit - r.debit + closing.get(r.name, 0), 2)) for r in rows if r.root_type == "Income"]
	expenses = [dict(name=r.name, account_code=r.account_code, account_name=r.account_name,
		amount=flt(r.debit - r.credit - closing.get(r.name, 0), 2)) for r in rows if r.root_type == "Expense"]
	total_income = flt(sum(r["amount"] for r in income), 2)
	total_expenses = flt(sum(r["amount"] for r in expenses), 2)
	return {
		"currency": _currency(cooperative), "from_date": start, "to_date": end,
		"income": [r for r in income if r["amount"]], "expenses": [r for r in expenses if r["amount"]],
		"total_income": total_income, "total_expenses": total_expenses, "surplus": flt(total_income - total_expenses, 2),
	}


def _closing_movements(cooperative, start, end):
	"""Debits minus credits that share-out closing entries made on each account in the period."""
	rows = frappe.db.sql(
		"""select l.account, sum(l.debit - l.credit) as amount
		from `tabCoop Journal Line` l join `tabCoop Journal Entry` j on j.name = l.parent
		where j.cooperative = %(c)s and j.voucher_type = 'Share-out' and l.is_posted = 1
			and l.root_type in ('Income', 'Expense') and l.posting_date between %(s)s and %(e)s
		group by l.account""",
		{"c": cooperative, "s": start, "e": end},
		as_dict=True,
	)
	return {r.account: flt(r.amount, 2) for r in rows}


@frappe.whitelist()
def balance_sheet(cooperative, as_of=None):
	_staff(cooperative)
	as_of = getdate(as_of or today())
	rows = accounting.account_balances(cooperative, to_date=as_of)
	pick = lambda root: [dict(name=r.name, account_code=r.account_code, account_name=r.account_name, amount=r.closing)
		for r in rows if r.root_type == root and r.closing]
	assets, liabilities, equity = pick("Asset"), pick("Liability"), pick("Equity")
	# Income less costs not yet closed into retained surplus by a share-out
	unclosed = flt(_sum_root(rows, "Income") - _sum_root(rows, "Expense"), 2)
	if unclosed:
		equity.append(dict(name=None, account_code="", account_name=_("Surplus for the year so far"), amount=unclosed))
	total = lambda items: flt(sum(i["amount"] for i in items), 2)
	return {
		"currency": _currency(cooperative), "as_of": as_of,
		"assets": assets, "liabilities": liabilities, "equity": equity,
		"total_assets": total(assets), "total_liabilities": total(liabilities), "total_equity": total(equity),
		"balanced": abs(total(assets) - total(liabilities) - total(equity)) < 0.01,
	}


CASH_FLOW_LABELS = {
	"Contribution": "Savings received",
	"Loan Repayment": "Loan repayments received",
	"Unallocated Receipt": "Transfers not yet matched",
	"Loan Disbursement": "Loans paid out",
	"Withdrawal": "Withdrawals paid out",
	"Payment Charges": "Payment processing fees",
	"Refund": "Refunds",
	"Manual Journal": "Other (manual entries)",
	"Reversal": "Reversed payments",
	"Allocation": "Allocations",
	"Share-out": "Share-out",
}


@frappe.whitelist()
def cash_flow(cooperative, from_date=None, to_date=None):
	"""Where the money came from and went (direct method), across bank, cash and Paystack balance."""
	_staff(cooperative)
	start, end = _period(cooperative, from_date, to_date)
	opening = accounting.cash_balance(cooperative, upto=add_days(start, -1))
	rows = frappe.db.sql(
		"""select j.voucher_type, sum(l.debit) as cash_in, sum(l.credit) as cash_out
		from `tabCoop Journal Line` l join `tabCoop Journal Entry` j on j.name = l.parent
		where l.cooperative = %(c)s and l.is_posted = 1 and l.account_type in %(cash)s
			and l.posting_date between %(s)s and %(e)s
		group by j.voucher_type""",
		{"c": cooperative, "cash": CASH_TYPES, "s": start, "e": end},
		as_dict=True,
	)
	items = [
		{"voucher_type": r.voucher_type, "label": _(CASH_FLOW_LABELS.get(r.voucher_type, r.voucher_type)),
			"amount": flt(flt(r.cash_in) - flt(r.cash_out), 2)}
		for r in rows if flt(r.cash_in) - flt(r.cash_out)
	]
	items.sort(key=lambda i: -i["amount"])
	net = flt(sum(i["amount"] for i in items), 2)
	return {"currency": _currency(cooperative), "from_date": start, "to_date": end, "opening": opening,
		"items": items, "net": net, "closing": flt(opening + net, 2)}


@frappe.whitelist()
def general_ledger(cooperative, account, from_date=None, to_date=None):
	_staff(cooperative)
	acc = frappe.db.get_value("Coop Account", account, ["name", "cooperative", "account_code", "account_name", "root_type"],
		as_dict=True)
	if not acc or acc.cooperative != cooperative:
		frappe.throw(_("Account not found"))
	start, end = _period(cooperative, from_date, to_date)
	sign = 1 if acc.root_type in DEBIT_NORMAL else -1
	opening_rows = frappe.db.sql(
		"""select coalesce(sum(debit - credit), 0) from `tabCoop Journal Line`
		where account = %s and is_posted = 1 and posting_date < %s""", (account, start))
	balance = flt(opening_rows[0][0], 2)
	lines = frappe.db.sql(
		"""select l.parent as journal, l.posting_date, l.debit, l.credit, l.member, l.remarks, j.voucher_type, j.remarks as narration
		from `tabCoop Journal Line` l join `tabCoop Journal Entry` j on j.name = l.parent
		where l.account = %s and l.is_posted = 1 and l.posting_date between %s and %s
		order by l.posting_date, j.creation, l.idx""",
		(account, start, end),
		as_dict=True,
	)
	members = dict(frappe.get_all("Coop Member", filters={"cooperative": cooperative}, fields=["name", "full_name"], as_list=True))
	opening = flt(sign * balance, 2)
	for l in lines:
		balance = flt(balance + flt(l.debit) - flt(l.credit), 2)
		l.balance = flt(sign * balance, 2)
		l.member_name = members.get(l.member)
	return {"currency": _currency(cooperative), "account": acc, "from_date": start, "to_date": end, "opening": opening,
		"lines": lines, "closing": flt(sign * balance, 2)}


# Closing the books -----------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def close_books(cooperative, until):
	"""Stop anything being posted on or before a date (for example after the accounts are audited)."""
	get_membership(cooperative, owner=True)
	until = getdate(until)
	if until >= getdate(today()):
		frappe.throw(_("You can only close days that have ended. Choose yesterday or earlier."))
	current = accounting.books_closed_until(cooperative)
	if current and until <= current:
		frappe.throw(_("The books are already closed up to {0}").format(formatdate(current, "d MMM yyyy")))
	if frappe.db.exists("Coop Journal Entry", {"cooperative": cooperative, "status": "Draft", "posting_date": ["<=", until]}):
		frappe.throw(_("Approve or reject the draft journal entries in that period first"))
	frappe.db.set_value("Cooperative", cooperative, "books_closed_until", until)
	return summary(cooperative)


# Year-end share-out ------------------------------------------------------------------------

BASES = ("Average savings", "Savings at year end", "Interest paid")


def _share_period(cooperative, period_end):
	locked = accounting.books_closed_until(cooperative)
	if locked:
		start = add_days(locked, 1)
	else:
		first = frappe.db.sql("select min(posting_date) from `tabCoop Journal Entry` where cooperative=%s and status in ('Posted', 'Reversed')",
			cooperative)[0][0]
		start = getdate(first or frappe.db.get_value("Cooperative", cooperative, "creation"))
	end = getdate(period_end)
	if end >= getdate(today()):
		frappe.throw(_("A share-out can only cover days that have ended. Choose yesterday or earlier."))
	if end < getdate(start):
		frappe.throw(_("Choose an end date after {0}").format(formatdate(start, "d MMM yyyy")))
	return getdate(start), end


def _savings_by_day(cooperative, end):
	rows = frappe.db.sql(
		"""select member, posting_date, sum(credit - debit) as amount from `tabCoop Journal Line`
		where cooperative = %s and is_posted = 1 and account_type = 'Member Savings' and posting_date <= %s
		group by member, posting_date order by member, posting_date""",
		(cooperative, end),
		as_dict=True,
	)
	by_member = {}
	for r in rows:
		by_member.setdefault(r.member, []).append((getdate(r.posting_date), flt(r.amount)))
	return by_member


def _average_balance(movements, start, end):
	"""Time-weighted average balance over [start, end] from dated movements."""
	days = (end - start).days + 1
	balance = sum(a for d, a in movements if d < start)
	total, cursor = 0.0, start
	for d, a in movements:
		if d < start:
			continue
		total += balance * (d - cursor).days
		balance += a
		cursor = d
	total += balance * ((end - cursor).days + 1)
	return max(0.0, total / days)


def compute_share_out(cooperative, period_end, reserve_percent=None, member_percent=100, basis="Average savings"):
	if basis not in BASES:
		frappe.throw(_("Choose how to share the surplus"))
	start, end = _share_period(cooperative, period_end)
	rows = accounting.account_balances(cooperative, to_date=end)
	income = flt(sum(-r.net_debit for r in rows if r.root_type == "Income"), 2)
	expenses = flt(sum(r.net_debit for r in rows if r.root_type == "Expense"), 2)
	surplus = flt(income - expenses, 2)
	if reserve_percent in (None, ""):
		reserve_percent = flt(frappe.db.get_value("Cooperative", cooperative, "reserve_percent") or 25)
	reserve_percent, member_percent = flt(reserve_percent), flt(member_percent)
	if not (0 <= reserve_percent <= 100 and 0 <= member_percent <= 100):
		frappe.throw(_("Percentages must be between 0 and 100"))
	reserve = flt(surplus * reserve_percent / 100, 2) if surplus > 0 else 0.0
	pool = flt((surplus - reserve) * member_percent / 100, 2) if surplus > 0 else 0.0

	members = frappe.get_all("Coop Member", filters={"cooperative": cooperative, "status": ["in", ["Active", "Suspended", "Left"]]},
		fields=["name", "full_name"])
	if basis == "Interest paid":
		paid = dict(frappe.db.sql(
			"""select member, sum(credit - debit) from `tabCoop Journal Line`
			where cooperative = %s and is_posted = 1 and account_type = 'Interest Income' and posting_date between %s and %s
			group by member""", (cooperative, start, end)))
		values = {m.name: flt(paid.get(m.name)) for m in members}
	else:
		by_day = _savings_by_day(cooperative, end)
		values = {
			m.name: (_average_balance(by_day.get(m.name, []), start, end) if basis == "Average savings"
				else max(0.0, sum(a for _d, a in by_day.get(m.name, []))))
			for m in members
		}
	total_basis = sum(v for v in values.values() if v > 0)
	lines = []
	for m in members:
		v = values.get(m.name, 0)
		if v <= 0 or not total_basis or pool <= 0:
			continue
		share = int(pool * v / total_basis * 100) / 100  # round down to the kobo; leftovers stay retained
		if share > 0:
			lines.append({"member": m.name, "member_name": m.full_name, "basis_value": flt(v, 2), "share_amount": share})
	to_members = flt(sum(l["share_amount"] for l in lines), 2)
	return frappe._dict(
		cooperative=cooperative, period_start=start, period_end=end, basis=basis, income=income, expenses=expenses,
		surplus=surplus, reserve_percent=reserve_percent, reserve_amount=reserve, member_percent=member_percent,
		to_members=to_members, retained=flt(surplus - reserve - to_members, 2),
		lines=sorted(lines, key=lambda l: -l["share_amount"]), currency=_currency(cooperative),
	)


@frappe.whitelist()
def preview_share_out(cooperative, period_end, reserve_percent=None, member_percent=100, basis="Average savings"):
	_staff(cooperative)
	return compute_share_out(cooperative, period_end, reserve_percent, member_percent, basis)


@frappe.whitelist(methods=["POST"])
def create_share_out(cooperative, period_end, reserve_percent=None, member_percent=100, basis="Average savings", note=None):
	me = _staff(cooperative)
	if frappe.db.exists("Coop Share Out", {"cooperative": cooperative, "status": "Draft"}):
		frappe.throw(_("There's already a share-out waiting for approval"))
	if frappe.db.exists("Coop Journal Entry", {"cooperative": cooperative, "status": "Draft",
			"posting_date": ["<=", getdate(period_end)]}):
		frappe.throw(_("Approve or reject the draft journal entries in that period first"))
	result = compute_share_out(cooperative, period_end, reserve_percent, member_percent, basis)
	doc = frappe.get_doc({"doctype": "Coop Share Out", **{k: v for k, v in result.items() if k != "currency"},
		"status": "Draft", "prepared_by": frappe.session.user, "note": note})
	doc.insert(ignore_permissions=True)
	approvers = [u for u in members_of(cooperative, roles=("Admin",)) if u != me.user]
	notify(approvers, _("A year-end share-out needs approval"),
		_("{0} prepared it: {1} to members.").format(me.full_name, money_text(doc.to_members, result.currency)),
		link=f"/admin/books/share-out/{doc.name}", cooperative=cooperative, kind="share-out")
	return get_share_out(doc.name)


@frappe.whitelist()
def get_share_out(name):
	doc = frappe.get_doc("Coop Share Out", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	data = doc.as_dict()
	if not me.is_admin:
		# Members see the totals and their own line
		data["lines"] = [l for l in data["lines"] if l.member == me.name]
	data["currency"] = _currency(doc.cooperative)
	data["prepared_by_name"] = frappe.db.get_value("User", doc.prepared_by, "full_name")
	data["approved_by_name"] = doc.approved_by and frappe.db.get_value("User", doc.approved_by, "full_name")
	data["can_approve"] = doc.status == "Draft" and me.is_owner and doc.prepared_by != frappe.session.user
	return data


@frappe.whitelist()
def share_outs(cooperative):
	get_membership(cooperative)
	return frappe.get_all("Coop Share Out", filters={"cooperative": cooperative, "status": ["!=", "Cancelled"]},
		fields=["name", "status", "period_start", "period_end", "surplus", "to_members", "reserve_amount", "posted_on"],
		order_by="period_end desc")


@frappe.whitelist(methods=["POST"])
def cancel_share_out(name):
	doc = frappe.get_doc("Coop Share Out", name)
	_staff(doc.cooperative)
	if doc.status != "Draft":
		frappe.throw(_("Only a share-out waiting for approval can be cancelled"))
	doc.db_set("status", "Cancelled")
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def approve_share_out(name):
	"""Post the share-out: close the year's income and costs into retained surplus, move the
	reserve into the reserve fund, credit each member's savings with their share, and close the
	books up to the end of the period."""
	doc = frappe.get_doc("Coop Share Out", name, for_update=True)
	me = get_membership(doc.cooperative, owner=True)
	if doc.status != "Draft":
		frappe.throw(_("This share-out has already been dealt with"))
	if doc.prepared_by == frappe.session.user:
		frappe.throw(_("Another admin has to approve the share-out you prepared"), frappe.PermissionError)
	fresh = compute_share_out(doc.cooperative, doc.period_end, doc.reserve_percent, doc.member_percent, doc.basis)
	if abs(fresh.surplus - flt(doc.surplus)) >= 0.01 or abs(fresh.to_members - flt(doc.to_members)) >= 0.01:
		frappe.throw(_("The figures changed since this was prepared. Cancel it and prepare it again."))

	rows = accounting.account_balances(doc.cooperative, to_date=doc.period_end)
	lines = []
	for r in rows:
		if r.root_type in ("Income", "Expense") and abs(r.net_debit) >= 0.005:
			# Bring each income and cost account back to zero
			lines.append({"account": r.name, "debit": -r.net_debit if r.net_debit < 0 else 0,
				"credit": r.net_debit if r.net_debit > 0 else 0})
	lines.append({"type": "Retained Surplus", "credit": doc.retained})
	lines.append({"type": "Reserve Fund", "credit": doc.reserve_amount})
	for l in doc.lines:
		lines.append({"type": "Member Savings", "credit": l.share_amount, "member": l.member,
			"remarks": _("Share of surplus")})
	je = accounting.make_entry(doc.cooperative, "Share-out", lines, posting_date=doc.period_end,
		reference=("Coop Share Out", doc.name), remarks=_("Year-end share-out {0} to {1}").format(
			formatdate(doc.period_start, "d MMM yyyy"), formatdate(doc.period_end, "d MMM yyyy")), system=False)
	doc.db_set({"status": "Posted", "approved_by": frappe.session.user, "posted_on": now_datetime(),
		"journal_entry": je.name if je else None})
	frappe.db.set_value("Cooperative", doc.cooperative, "books_closed_until", doc.period_end)

	currency = _currency(doc.cooperative)
	for l in doc.lines:
		refresh_all(member=l.member)
		user = frappe.db.get_value("Coop Member", l.member, "user")
		notify(user, _("{0} added to your savings").format(money_text(l.share_amount, currency)),
			_("Your share of the group's surplus up to {0}.").format(formatdate(doc.period_end, "d MMM yyyy")),
			link=f"/share-out/{doc.name}", cooperative=doc.cooperative, kind="share-out")
	refresh_all(cooperative=doc.cooperative)
	from coopvault.api.coop import _announce

	_announce(doc.cooperative, _("Year-end share-out"), _(
		"The surplus up to {0} was {1}. {2} went to the reserve fund and {3} was shared among members in proportion to {4}."
	).format(formatdate(doc.period_end, "d MMM yyyy"), money_text(doc.surplus, currency), money_text(doc.reserve_amount, currency),
		money_text(doc.to_members, currency), _(doc.basis).lower()))
	return get_share_out(name)
