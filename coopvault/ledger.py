"""Balance calculations for cooperatives, members and loans.

Everything here is recomputed from the source documents (confirmed contributions,
confirmed repayments and loans) instead of being incremented, so the stored totals
can always be rebuilt and never drift.
"""

import frappe
from frappe.utils import add_days, add_months, cint, flt, getdate, today

OPEN_LOAN_STATES = ("Pending", "Active")
PERIOD_MONTHS = {"Monthly": 1, "Quarterly": 3}


def _sum(doctype, field, filters):
	value = frappe.get_all(doctype, filters=filters, fields=[{"SUM": field, "as": "total"}])
	return flt(value[0].total) if value else 0.0


def member_balances(member, exclude_loan=None):
	"""Return the savings / borrowing position of a Coop Member.

	borrow_limit = contributed x multiplier - (balance still owed on active loans + pending requests).
	With the default multiplier of 1 a member can never owe more than they have contributed.
	"""
	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	multiplier = flt(frappe.db.get_value("Cooperative", cooperative, "loan_multiplier"))

	contributed = _sum("Coop Contribution", "amount", {"member": member, "status": "Confirmed"})
	repaid = _sum("Coop Loan Repayment", "amount", {"member": member, "status": "Confirmed"})

	loan_filters = {"member": member, "status": ["in", OPEN_LOAN_STATES]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	loans = frappe.get_all("Coop Loan", filters=loan_filters, fields=["status", "principal", "balance"])

	outstanding = sum(flt(l.balance) for l in loans if l.status == "Active")
	pending = sum(flt(l.principal) for l in loans if l.status == "Pending")
	exposure = outstanding + pending

	return frappe._dict(
		total_contributed=contributed,
		total_repaid=repaid,
		loan_outstanding=outstanding,
		pending_loans=pending,
		exposure=exposure,
		multiplier=multiplier,
		borrow_limit=max(0.0, contributed * multiplier - exposure),
	)


def cooperative_totals(cooperative, exclude_loan=None):
	contributions = _sum("Coop Contribution", "amount", {"cooperative": cooperative, "status": "Confirmed"})
	repayments = _sum("Coop Loan Repayment", "amount", {"cooperative": cooperative, "status": "Confirmed"})

	loan_filters = {"cooperative": cooperative, "status": ["in", ("Active", "Repaid")]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	disbursed = _sum("Coop Loan", "principal", loan_filters)
	outstanding = _sum("Coop Loan", "balance", {"cooperative": cooperative, "status": "Active"})
	members = frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Active"})

	return frappe._dict(
		member_count=members,
		total_contributions=contributions,
		total_repayments=repayments,
		total_disbursed=disbursed,
		loans_outstanding=outstanding,
		pool_balance=contributions + repayments - disbursed,
	)


# Contribution schedule ------------------------------------------------------------


def period_start(date, frequency):
	date = getdate(date)
	if frequency == "Weekly":
		return add_days(date, -date.weekday())
	if frequency == "Quarterly":
		return date.replace(month=(date.month - 1) // 3 * 3 + 1, day=1)
	return date.replace(day=1)


def shift_period(start, frequency, n=1):
	if frequency == "Weekly":
		return add_days(start, 7 * n)
	return getdate(add_months(start, PERIOD_MONTHS[frequency] * n))


def _periods_between(first, current, frequency):
	if frequency == "Weekly":
		return (current - first).days // 7 + 1
	months = (current.year - first.year) * 12 + current.month - first.month
	return months // PERIOD_MONTHS[frequency] + 1


def contribution_standing(member, contributed=None):
	"""Where a member stands against the cooperative's contribution schedule.

	Every period (week / month / quarter) since the member joined is owed in advance and
	must be paid before it ends. Confirmed contributions fill the oldest periods first.
	state: up_to_date (this period covered), due (only this period outstanding) or behind.
	"""
	m = frappe.db.get_value(
		"Coop Member", member, ["cooperative", "joined_on", "creation", "status"], as_dict=True
	)
	coop = frappe.db.get_value(
		"Cooperative",
		m.cooperative,
		["contribution_amount", "contribution_frequency", "start_date"],
		as_dict=True,
	)
	amount, frequency = flt(coop.contribution_amount), coop.contribution_frequency
	if frequency not in ("Weekly", "Monthly", "Quarterly") or amount <= 0 or m.status != "Active":
		return frappe._dict(tracked=False)

	if contributed is None:
		contributed = _sum("Coop Contribution", "amount", {"member": member, "status": "Confirmed"})

	anchor = getdate(m.joined_on or m.creation)
	if coop.start_date and getdate(coop.start_date) > anchor:
		anchor = getdate(coop.start_date)
	first = period_start(anchor, frequency)
	current = period_start(today(), frequency)
	elapsed = max(0, _periods_between(first, current, frequency)) if current >= first else 0

	covered = int((flt(contributed) + 0.005) // amount)
	current_end = add_days(shift_period(current, frequency), -1)

	if elapsed == 0:
		state = "not_started"
	elif covered >= elapsed:
		state = "up_to_date"
	elif covered == elapsed - 1:
		state = "due"
	else:
		state = "behind"

	return frappe._dict(
		tracked=True,
		frequency=frequency,
		amount=amount,
		state=state,
		periods_elapsed=elapsed,
		periods_covered=covered,
		periods_behind=max(0, elapsed - 1 - covered),
		expected_to_date=elapsed * amount,
		paid=flt(contributed),
		due_now=max(0.0, elapsed * amount - flt(contributed)),
		arrears=max(0.0, (elapsed - 1) * amount - flt(contributed)),
		paid_through=add_days(shift_period(first, frequency, covered), -1) if covered else None,
		current_period_start=current,
		current_period_end=current_end,
		next_due_date=current_end if covered < elapsed else shift_period(first, frequency, covered),
		started_on=first,
	)


def loan_schedule(loan):
	"""Equal monthly instalments from the disbursement date; returns what is due next."""
	if loan.get("status") != "Active" or not loan.get("disbursed_on"):
		return None
	count = max(1, cint(loan.tenure_months))
	total, repaid = flt(loan.total_payable), flt(loan.amount_repaid)
	instalment = total / count
	start, now = getdate(loan.disbursed_on), getdate(today())

	rows, next_row, overdue = [], None, 0.0
	for k in range(1, count + 1):
		due = getdate(add_months(start, k))
		cumulative = total if k == count else instalment * k
		row = frappe._dict(number=k, due_date=due, amount=instalment, paid=repaid + 0.005 >= cumulative)
		rows.append(row)
		if due < now:
			overdue = max(overdue, cumulative - repaid)
		if not next_row and not row.paid:
			next_row = frappe._dict(due_date=due, amount=min(cumulative - repaid, total - repaid))

	return frappe._dict(
		instalment=instalment,
		instalments=count,
		paid_instalments=sum(1 for r in rows if r.paid),
		next_due_date=next_row.due_date if next_row else None,
		next_amount=next_row.amount if next_row else 0.0,
		overdue_amount=max(0.0, overdue),
		is_overdue=overdue > 0.005,
		rows=rows,
	)


# Stored totals --------------------------------------------------------------------


def refresh_member(member):
	if not member or not frappe.db.exists("Coop Member", member):
		return
	b = member_balances(member)
	frappe.db.set_value(
		"Coop Member",
		member,
		{
			"total_contributed": b.total_contributed,
			"total_repaid": b.total_repaid,
			"loan_outstanding": b.loan_outstanding,
			"borrow_limit": b.borrow_limit,
		},
		update_modified=False,
	)


def refresh_cooperative(cooperative):
	if not cooperative or not frappe.db.exists("Cooperative", cooperative):
		return
	frappe.db.set_value("Cooperative", cooperative, cooperative_totals(cooperative), update_modified=False)


def refresh_loan(loan_name):
	"""Recompute repaid amount and balance of a loan from its confirmed repayments."""
	loan = frappe.db.get_value("Coop Loan", loan_name, ["name", "status", "total_payable"], as_dict=True)
	if not loan:
		return
	repaid = _sum("Coop Loan Repayment", "amount", {"loan": loan_name, "status": "Confirmed"})
	values = {"amount_repaid": repaid, "balance": max(0.0, flt(loan.total_payable) - repaid)}
	if loan.status == "Active" and values["balance"] <= 0:
		values["status"] = "Repaid"
	elif loan.status == "Repaid" and values["balance"] > 0:
		values["status"] = "Active"
	frappe.db.set_value("Coop Loan", loan_name, values, update_modified=False)


def refresh_all(member=None, cooperative=None, loan=None):
	if loan:
		refresh_loan(loan)
	refresh_member(member)
	refresh_cooperative(cooperative)


def on_member_change(doc, method=None):
	refresh_all(member=doc.name, cooperative=doc.cooperative)


def refresh_all_cooperatives():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		for member in frappe.get_all("Coop Member", filters={"cooperative": coop}, pluck="name"):
			refresh_member(member)
		refresh_cooperative(coop)
