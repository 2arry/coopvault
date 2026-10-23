"""Balance calculations for cooperatives, members and loans.

Everything here is recomputed from the source documents (confirmed contributions and repayments,
loans and withdrawals) instead of being incremented, so the stored totals can always be rebuilt
and never drift.
"""

import frappe
from frappe.utils import add_days, add_months, cint, flt, getdate, today

from coopvault import accounting, loans as loan_engine

OPEN_LOAN_STATES = ("Pending", "Approved", "Sent", "Active")
MONEY_OUT_LOAN_STATES = ("Sent", "Active", "Repaid")
OWED_LOAN_STATES = ("Sent", "Active")
WITHDRAWN_STATES = ("Sent", "Completed")
TRACKED = ("Weekly", "Monthly", "Quarterly")
PERIOD_MONTHS = {"Monthly": 1, "Quarterly": 3}


def _sum(doctype, field, filters):
	value = frappe.get_all(doctype, filters=filters, fields=[{"SUM": field, "as": "total"}])
	return flt(value[0].total) if value else 0.0


def member_balances(member, exclude_loan=None, exclude_withdrawal=None):
	"""Savings and borrowing position of a Coop Member.

	savings      = confirmed contributions - withdrawals paid out
	borrow_limit = (savings - pending withdrawals - guarantees given) x multiplier - (owed on loans + pending requests)
	With the default multiplier of 1 nobody can owe more than they have saved. Savings promised as a
	guarantor for someone else's loan are held until that loan is repaid.
	"""
	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	# The most generous loan product sets the headline limit (each product checks its own share)
	shares = frappe.get_all("Coop Loan Product", filters={"cooperative": cooperative, "enabled": 1}, pluck="savings_multiplier")
	multiplier = max(flt(x) for x in shares) if shares else flt(frappe.db.get_value("Cooperative", cooperative, "loan_multiplier"))

	contributed = _sum("Coop Contribution", "amount", {"member": member, "status": "Confirmed"})
	repaid = _sum("Coop Loan Repayment", "amount", {"member": member, "status": "Confirmed"})
	withdrawal_filters = {"member": member}
	if exclude_withdrawal:
		withdrawal_filters["name"] = ["!=", exclude_withdrawal]
	withdrawn = _sum("Coop Withdrawal", "amount", {**withdrawal_filters, "status": ["in", WITHDRAWN_STATES]})
	withdrawal_requested = _sum("Coop Withdrawal", "amount", {**withdrawal_filters, "status": ["in", ["Pending", "Approved"]]})

	loan_filters = {"member": member, "status": ["in", OPEN_LOAN_STATES]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	loans = frappe.get_all("Coop Loan", filters=loan_filters, fields=["status", "principal", "balance"])
	outstanding = sum(flt(l.balance) for l in loans if l.status in OWED_LOAN_STATES)
	pending = sum(flt(l.principal) for l in loans if l.status in ("Pending", "Approved"))
	exposure = outstanding + pending
	# What the group owes the member, from the books: contributions, less withdrawals, plus any
	# surplus shared out to them
	savings = accounting.member_savings(member)
	pledged = loan_engine.pledged_by(member, exclude_loan=exclude_loan)

	return frappe._dict(
		savings=savings,
		total_contributed=contributed,
		total_withdrawn=withdrawn,
		withdrawal_requested=withdrawal_requested,
		total_repaid=repaid,
		loan_outstanding=outstanding,
		pending_loans=pending,
		exposure=exposure,
		multiplier=multiplier,
		pledged=pledged,
		borrow_limit=max(0.0, (savings - withdrawal_requested - pledged) * multiplier - exposure),
		# Savings that aren't backing a loan, a pending request or a guarantee can be taken out
		withdrawable=max(0.0, savings - withdrawal_requested - exposure - pledged),
	)


def cooperative_totals(cooperative, exclude_loan=None, exclude_withdrawal=None):
	contributions = _sum("Coop Contribution", "amount", {"cooperative": cooperative, "status": "Confirmed"})
	repayments = _sum("Coop Loan Repayment", "amount", {"cooperative": cooperative, "status": "Confirmed"})

	loan_filters = {"cooperative": cooperative, "status": ["in", MONEY_OUT_LOAN_STATES]}
	if exclude_loan:
		loan_filters["name"] = ["!=", exclude_loan]
	disbursed = _sum("Coop Loan", "principal", loan_filters)
	outstanding = _sum("Coop Loan", "balance", {"cooperative": cooperative, "status": ["in", OWED_LOAN_STATES]})
	withdrawal_filters = {"cooperative": cooperative, "status": ["in", WITHDRAWN_STATES]}
	if exclude_withdrawal:
		withdrawal_filters["name"] = ["!=", exclude_withdrawal]
	withdrawn = _sum("Coop Withdrawal", "amount", withdrawal_filters)
	members = frappe.db.count("Coop Member", {"cooperative": cooperative, "status": "Active"})

	return frappe._dict(
		member_count=members,
		total_contributions=contributions,
		total_repayments=repayments,
		total_withdrawn=withdrawn,
		total_disbursed=disbursed,
		loans_outstanding=outstanding,
		# Money actually held (bank, cash, Paystack balance) after fees, from the books
		pool_balance=accounting.cash_balance(cooperative),
	)


# Contribution plans and schedule ----------------------------------------------------


def period_start(date, frequency):
	date = getdate(date)
	if frequency == "Weekly":
		return add_days(date, -date.weekday())
	if frequency == "Quarterly":
		return date.replace(month=(date.month - 1) // 3 * 3 + 1, day=1)
	return date.replace(day=1)


def shift_period(start, frequency, n=1):
	if frequency == "Weekly":
		return getdate(add_days(start, 7 * n))
	return getdate(add_months(start, PERIOD_MONTHS[frequency] * n))


def first_full_period(date, frequency):
	"""Start of the first whole period on or after `date`."""
	date = getdate(date)
	start = period_start(date, frequency)
	return start if start == date else shift_period(start, frequency)


def plans_for(cooperative):
	"""Contribution plans in force, oldest first: [{effective_from, amount, frequency}]."""
	rows = frappe.get_all(
		"Coop Contribution Plan",
		filters={"parent": cooperative, "parenttype": "Cooperative"},
		fields=["effective_from", "amount", "frequency"],
		order_by="effective_from asc, idx asc",
	)
	if rows:
		return [frappe._dict(r, effective_from=getdate(r.effective_from)) for r in rows]
	coop = frappe.db.get_value(
		"Cooperative", cooperative, ["contribution_amount", "contribution_frequency", "start_date", "creation"], as_dict=True
	)
	return [
		frappe._dict(
			effective_from=getdate(coop.start_date or coop.creation),
			amount=flt(coop.contribution_amount),
			frequency=coop.contribution_frequency or "Flexible",
		)
	]


def plan_on(plans, date):
	date = getdate(date)
	current = plans[0]
	for p in plans:
		if p.effective_from <= date:
			current = p
	return current


def _periods(plans, anchor, until):
	"""Every whole contribution period from `anchor` until `until`, with the amount due for each."""
	out = []
	for i, plan in enumerate(plans):
		if plan.frequency not in TRACKED or flt(plan.amount) <= 0:
			continue
		seg_end = plans[i + 1].effective_from if i + 1 < len(plans) else None
		start = first_full_period(max(getdate(plan.effective_from), anchor), plan.frequency)
		while start <= until and (seg_end is None or start < seg_end):
			end = add_days(shift_period(start, plan.frequency), -1)
			out.append(frappe._dict(start=start, end=getdate(end), amount=flt(plan.amount), frequency=plan.frequency))
			start = shift_period(start, plan.frequency)
	return out


def contribution_standing(member, contributed=None):
	"""Where a member stands against the cooperative's contribution plan.

	Members owe one contribution per whole period from the first full period after they joined
	(or after the cooperative's start date). Each period uses the plan that applied then, so raising
	the amount never makes past months look unpaid. Confirmed contributions fill the oldest periods
	first. state: up_to_date, due (only the current period is open), behind, or not_started.
	"""
	m = frappe.db.get_value("Coop Member", member, ["cooperative", "joined_on", "creation", "status"], as_dict=True)
	if m.status != "Active":
		return frappe._dict(tracked=False)
	plans = plans_for(m.cooperative)
	now = getdate(today())
	current_plan = plan_on(plans, now)
	if not any(p.frequency in TRACKED and flt(p.amount) > 0 for p in plans):
		return frappe._dict(tracked=False)

	start_date = frappe.db.get_value("Cooperative", m.cooperative, "start_date")
	anchor = getdate(m.joined_on or m.creation)
	if start_date and getdate(start_date) > anchor:
		anchor = getdate(start_date)
	if contributed is None:
		contributed = _sum("Coop Contribution", "amount", {"member": member, "status": "Confirmed"})
	paid = flt(contributed)

	elapsed = _periods(plans, anchor, now)
	current = elapsed[-1] if elapsed and elapsed[-1].end >= now else None
	past = elapsed[:-1] if current else elapsed
	expected = sum(p.amount for p in elapsed)
	expected_past = sum(p.amount for p in past)

	# Fill periods oldest-first, continuing into future periods to know how far ahead someone is
	remaining, covered, paid_through, next_open = paid, 0, None, None
	horizon = add_months(now, 36)
	for p in _periods(plans, anchor, getdate(horizon)):
		if remaining + 0.005 >= p.amount:
			remaining -= p.amount
			covered += 1
			paid_through = p.end
		else:
			next_open = p
			break

	periods_behind = max(0, len(past) - covered)
	if not elapsed:
		state = "not_started"
	elif paid + 0.005 >= expected:
		state = "up_to_date"
	elif paid + 0.005 >= expected_past:
		state = "due"
	else:
		state = "behind"

	first = _periods(plans, anchor, getdate(horizon))[:1]
	shown = current or next_open or (first[0] if first else current_plan)
	return frappe._dict(
		tracked=True,
		frequency=shown.frequency,
		amount=flt(shown.amount),
		state=state,
		periods_elapsed=len(elapsed),
		periods_covered=covered,
		periods_behind=periods_behind,
		expected_to_date=expected,
		paid=paid,
		due_now=max(0.0, expected - paid),
		arrears=max(0.0, expected_past - paid),
		paid_through=paid_through,
		current_period_start=current.start if current else None,
		current_period_end=current.end if current else None,
		# What the next unpaid period still needs, at that period's plan (a scheduled change shows here)
		next_amount=flt(next_open.amount - remaining) if next_open else flt(shown.amount),
		next_due_date=(next_open.start if next_open else None) if state == "up_to_date" else (
			current.end if current else (first[0].start if first else None)
		),
		started_on=first[0].start if first else None,
	)


def loan_schedule(loan):
	"""Where an active loan stands against its instalments (shape kept for the app)."""
	if loan.get("status") != "Active":
		return None
	name = loan.get("name")
	if name and frappe.db.exists("Coop Loan Instalment", {"parent": name}):
		st = loan_engine.state(name)
		rows = st.rows
		return frappe._dict(
			instalment=rows[0].amount if rows else 0.0,
			instalments=len(rows),
			paid_instalments=sum(1 for r in rows if r.paid),
			next_due_date=st.next_due_date,
			next_amount=st.next_amount,
			overdue_amount=st.overdue_amount,
			days_overdue=st.days_overdue,
			is_overdue=st.overdue_amount > 0.005,
			rows=rows,
		)
	return _legacy_schedule(loan)


def _legacy_schedule(loan):
	"""Equal monthly instalments from the day the borrower confirmed the money arrived (loans
	without stored instalments)."""
	start = loan.get("received_on") or loan.get("disbursed_on")
	if not start:
		return None
	count = max(1, cint(loan.tenure_months))
	total, repaid = flt(loan.total_payable), flt(loan.amount_repaid)
	instalment = total / count
	start, now = getdate(start), getdate(today())

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
			"total_contributed": b.savings,
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
	if frappe.db.exists("Coop Loan Instalment", {"parent": loan_name}):
		loan_engine.refresh(loan_name)
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
