"""Loans: products, repayment schedules, how repayments are applied, penalties, arrears, guarantors
and restructuring.

Schedules are stored on the loan (Coop Loan Instalment rows). Each instalment has a principal and
an interest part, plus any late penalty charged on it. A repayment is applied oldest instalment
first, and within an instalment penalty, then interest, then principal; the split is stored on
the repayment (penalty_part / interest_part / principal_part) and posted to the books exactly so.

How much of each instalment is paid is worked out from those stored parts: the totals paid of each
kind fill the instalments oldest-first. That stays right when a repayment is reversed, and it's
what the arrears and penalties are measured against.
"""

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, date_diff, flt, getdate, today

METHODS = ("Flat", "Reducing balance", "Interest free")
PERIODS = ("Per month", "Per year", "Whole loan")
OPEN_STATES = ("Pending", "Approved", "Sent", "Active")
TOL = 0.005


# Products --------------------------------------------------------------------------------


def ensure_default_product(cooperative):
	"""Every cooperative starts with a "Standard loan" built from its existing loan settings."""
	existing = frappe.db.get_value("Coop Loan Product", {"cooperative": cooperative}, "name", order_by="creation asc")
	if existing:
		return existing
	coop = frappe.db.get_value("Cooperative", cooperative, ["interest_rate", "max_loan_tenure_months", "loan_multiplier"],
		as_dict=True)
	doc = frappe.get_doc(
		{
			"doctype": "Coop Loan Product",
			"cooperative": cooperative,
			"product_name": _("Standard loan"),
			"enabled": 1,
			"description": _("Borrow against what you've saved and pay back in monthly instalments."),
			"interest_method": "Flat" if flt(coop.interest_rate) else "Interest free",
			"interest_rate": flt(coop.interest_rate),
			"rate_period": "Whole loan",
			"max_tenure_months": cint(coop.max_loan_tenure_months) or 12,
			"min_tenure_months": 1,
			"savings_multiplier": flt(coop.loan_multiplier) if coop.loan_multiplier is not None else 1,
			# Before products, the only limit was the savings rule; keep that freedom for the standard loan
			"max_active_loans": 5,
			"require_up_to_date": 0,
			"grace_days": 3,
		}
	).insert(ignore_permissions=True)
	return doc.name


def validate_product(doc):
	if doc.interest_method not in METHODS or doc.rate_period not in PERIODS:
		frappe.throw(_("Choose how interest is worked out"))
	if doc.interest_method == "Interest free":
		doc.interest_rate = 0
	elif doc.interest_method == "Reducing balance" and doc.rate_period == "Whole loan":
		frappe.throw(_("A reducing-balance rate has to be per month or per year"))
	if not 0 <= flt(doc.savings_multiplier) <= 1:
		frappe.throw(_("Members can borrow at most what they've saved, so the share of savings is between 0 and 1"))
	for field in ("interest_rate", "processing_fee_percent", "penalty_percent", "guarantee_percent"):
		if not 0 <= flt(doc.get(field)) <= 100:
			frappe.throw(_("Percentages must be between 0 and 100"))
	doc.min_tenure_months = max(1, cint(doc.min_tenure_months))
	doc.max_tenure_months = max(doc.min_tenure_months, cint(doc.max_tenure_months) or 12)
	doc.max_active_loans = max(1, cint(doc.max_active_loans))
	if flt(doc.max_amount) and flt(doc.max_amount) < flt(doc.min_amount):
		frappe.throw(_("The maximum amount can't be below the minimum"))


def describe_rate(method, rate, period):
	"""Interest in words, e.g. "2% a month on the reducing balance"."""
	if method == "Interest free" or not flt(rate):
		return _("Interest free")
	per = {"Per month": _(" a month"), "Per year": _(" a year"), "Whole loan": _(" for the whole loan")}[period]
	how = _(" on the reducing balance") if method == "Reducing balance" else _(" flat")
	return f"{flt(rate):g}%{per}{how}"


# Schedules -------------------------------------------------------------------------------


def build_schedule(principal, months, method, rate, period, start):
	"""Equal monthly instalments from `start`. Returns rows of principal and interest."""
	principal, months, rate = flt(principal, 2), max(1, cint(months)), flt(rate)
	start = getdate(start)
	rows = []
	if method == "Reducing balance" and rate:
		r = rate / 100 if period == "Per month" else rate / 1200
		emi = principal * r / (1 - (1 + r) ** -months)
		left = principal
		for k in range(1, months + 1):
			interest = flt(left * r, 2)
			part = flt(emi - interest, 2) if k < months else flt(left, 2)
			left = flt(left - part, 2)
			rows.append({"number": k, "due_date": add_months(start, k), "principal": part, "interest": interest})
		return rows
	if method == "Flat" and rate:
		if period == "Per month":
			total_interest = principal * rate / 100 * months
		elif period == "Per year":
			total_interest = principal * rate / 100 * months / 12
		else:
			total_interest = principal * rate / 100
	else:
		total_interest = 0.0
	total_interest = flt(total_interest, 2)
	each_p, each_i = flt(principal / months, 2), flt(total_interest / months, 2)
	for k in range(1, months + 1):
		last = k == months
		rows.append({
			"number": k,
			"due_date": add_months(start, k),
			"principal": flt(principal - each_p * (months - 1), 2) if last else each_p,
			"interest": flt(total_interest - each_i * (months - 1), 2) if last else each_i,
		})
	return rows


def preview(product, principal, months, start=None):
	p = frappe.get_doc("Coop Loan Product", product) if isinstance(product, str) else product
	rows = build_schedule(principal, months, p.interest_method, p.interest_rate, p.rate_period, start or today())
	interest = flt(sum(r["interest"] for r in rows), 2)
	fee = flt(flt(principal) * flt(p.processing_fee_percent) / 100, 2)
	return frappe._dict(
		rows=rows, total_interest=interest, total_payable=flt(flt(principal) + interest, 2), processing_fee=fee,
		you_receive=flt(flt(principal) - fee, 2), first_instalment=flt(rows[0]["principal"] + rows[0]["interest"], 2),
		rate_text=describe_rate(p.interest_method, p.interest_rate, p.rate_period),
	)


def apply_product(loan):
	"""Copy the product's terms onto a new loan and work out what will be paid back."""
	product = frappe.get_doc("Coop Loan Product", loan.product)
	loan.interest_method, loan.interest_rate, loan.rate_period = product.interest_method, product.interest_rate, product.rate_period
	loan.penalty_percent, loan.grace_days = product.penalty_percent, product.grace_days
	p = preview(product, loan.principal, loan.tenure_months, loan.application_date or today())
	loan.interest_amount, loan.total_payable, loan.processing_fee = p.total_interest, p.total_payable, p.processing_fee
	loan.set("instalments", [])
	for row in p.rows:
		loan.append("instalments", row)


def start_schedule(loan):
	"""Once the borrower confirms the money arrived, the instalments run from that day."""
	rows = build_schedule(loan.principal, loan.tenure_months, loan.interest_method or "Flat", loan.interest_rate,
		loan.rate_period or "Whole loan", loan.received_on or today())
	loan.set("instalments", [])
	for row in rows:
		loan.append("instalments", row)
	loan.due_date = rows[-1]["due_date"]


# Applying repayments ---------------------------------------------------------------------


def paid_totals(loan_name, exclude=None):
	"""Penalty, interest and principal paid so far, from confirmed repayments' stored parts."""
	filters = {"loan": loan_name, "status": "Confirmed"}
	if exclude:
		filters["name"] = ["!=", exclude]
	rows = frappe.get_all("Coop Loan Repayment", filters=filters, fields=["penalty_part", "interest_part", "principal_part"])
	return frappe._dict(
		penalty=flt(sum(flt(r.penalty_part) for r in rows), 2),
		interest=flt(sum(flt(r.interest_part) for r in rows), 2),
		principal=flt(sum(flt(r.principal_part) for r in rows), 2),
	)


def fill(instalments, paid):
	"""How much of each instalment is paid: each kind's total paid fills instalments oldest-first."""
	left = {k: flt(paid[k]) for k in ("penalty", "interest", "principal")}
	out = []
	for row in sorted(instalments, key=lambda r: cint(r.number)):
		done = {}
		for kind, due in (("penalty", flt(row.penalty)), ("interest", flt(row.interest)), ("principal", flt(row.principal))):
			done[kind] = min(due, max(0.0, left[kind]))
			left[kind] = flt(left[kind] - done[kind], 2)
		owed = flt(row.penalty) + flt(row.interest) + flt(row.principal) - sum(done.values())
		out.append(frappe._dict(row=row, paid=done, owed=flt(max(0.0, owed), 2)))
	return out


def allocate(loan, amount, exclude=None):
	"""Split a new repayment: oldest instalment first; within it penalty, then interest, then principal."""
	doc = frappe.get_doc("Coop Loan", loan) if isinstance(loan, str) else loan
	left = flt(amount, 2)
	parts = {"penalty": 0.0, "interest": 0.0, "principal": 0.0}
	for f in fill(doc.instalments, paid_totals(doc.name, exclude)):
		for kind in ("penalty", "interest", "principal"):
			due = flt(flt(f.row.get(kind)) - f.paid[kind], 2)
			take = min(due, left)
			if take > 0:
				parts[kind] = flt(parts[kind] + take, 2)
				left = flt(left - take, 2)
		if left <= TOL:
			break
	if left > TOL:
		# More than the whole loan: shouldn't happen (repayments are capped at the balance)
		parts["principal"] = flt(parts["principal"] + left, 2)
	return frappe._dict(parts)


def state(loan):
	"""Where a loan stands: instalments with what's paid, what's overdue and since when."""
	doc = frappe.get_doc("Coop Loan", loan) if isinstance(loan, str) else loan
	now = getdate(today())
	paid = paid_totals(doc.name)
	rows, overdue, oldest, next_row = [], 0.0, None, None
	for f in fill(doc.instalments, paid):
		r = f.row
		total = flt(flt(r.principal) + flt(r.interest) + flt(r.penalty), 2)
		is_late = f.owed > TOL and getdate(r.due_date) < now
		if is_late:
			overdue += f.owed
			oldest = oldest or getdate(r.due_date)
		if f.owed > TOL and not next_row:
			next_row = frappe._dict(due_date=r.due_date, amount=f.owed)
		rows.append(frappe._dict(
			number=cint(r.number), due_date=r.due_date, principal=flt(r.principal), interest=flt(r.interest),
			penalty=flt(r.penalty), amount=total, owed=f.owed, paid=f.owed <= TOL, late=is_late, settled=cint(r.settled),
			status="Paid" if f.owed <= TOL else ("Overdue" if is_late else ("Part paid" if f.owed < total - TOL else "Due")),
		))
	return frappe._dict(
		rows=rows, paid=paid, overdue_amount=flt(overdue, 2), days_overdue=date_diff(now, oldest) if oldest else 0,
		next_due_date=next_row.due_date if next_row else None, next_amount=next_row.amount if next_row else 0.0,
		penalties_charged=flt(sum(flt(r.penalty) for r in doc.instalments), 2),
	)


def refresh(loan_name):
	"""Store a loan's totals, arrears and status from its schedule and confirmed repayments."""
	doc = frappe.get_doc("Coop Loan", loan_name)
	if not doc.instalments:
		return
	st = state(doc)
	repaid = flt(st.paid.penalty + st.paid.interest + st.paid.principal, 2)
	values = {
		"amount_repaid": repaid,
		"penalties_charged": st.penalties_charged,
		"principal_paid": st.paid.principal,
		"interest_paid": st.paid.interest,
		"penalties_paid": st.paid.penalty,
		"balance": max(0.0, flt(doc.total_payable + st.penalties_charged - repaid, 2)),
		"overdue_amount": st.overdue_amount if doc.status == "Active" else 0,
		"days_overdue": st.days_overdue if doc.status == "Active" else 0,
	}
	if doc.status == "Active" and values["balance"] <= TOL:
		values["status"] = "Repaid"
	elif doc.status == "Repaid" and values["balance"] > TOL:
		values["status"] = "Active"
	frappe.db.set_value("Coop Loan", loan_name, values, update_modified=False)
	if values.get("status") == "Repaid" or doc.status in ("Rejected", "Cancelled"):
		release_guarantees(loan_name)


# Late penalties --------------------------------------------------------------------------


def accrue_penalties(loan_name, on=None):
	"""Charge the product's late penalty on overdue instalments: once when the grace period ends,
	then once for every further month it stays unpaid. Returns what was added."""
	doc = frappe.get_doc("Coop Loan", loan_name)
	rate = flt(doc.penalty_percent)
	if doc.status != "Active" or not rate or not doc.instalments:
		return 0.0
	on = getdate(on or today())
	added = 0.0
	for f in fill(doc.instalments, paid_totals(doc.name)):
		r = f.row
		unpaid = flt(flt(r.principal) + flt(r.interest) - f.paid["principal"] - f.paid["interest"], 2)
		start = add_days(getdate(r.due_date), cint(doc.grace_days))
		if unpaid <= TOL or on <= start:
			continue
		months_late = 1 + max(0, (on.year - start.year) * 12 + on.month - start.month - (1 if on.day < start.day else 0))
		missing = months_late - cint(r.penalty_months)
		if missing > 0:
			charge = flt(unpaid * rate / 100 * missing, 2)
			r.penalty = flt(flt(r.penalty) + charge, 2)
			r.penalty_months = months_late
			added = flt(added + charge, 2)
	if added:
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
		refresh(loan_name)
	return added


# Guarantors ------------------------------------------------------------------------------


def pledged_by(member, exclude_loan=None):
	"""Savings a member has agreed to guarantee on open loans (held until those loans are repaid).
	A request they haven't accepted yet doesn't hold anything."""
	rows = frappe.db.sql(
		"""select coalesce(sum(g.amount), 0) from `tabCoop Loan Guarantor` g join `tabCoop Loan` l on l.name = g.parent
		where g.member = %s and g.status = 'Accepted' and l.status in %s and l.name != %s""",
		(member, OPEN_STATES, exclude_loan or ""),
	)
	return flt(rows[0][0], 2)


def guarantor_blocker(loan_name):
	"""Why a loan can't be approved yet because of its guarantors, or None."""
	doc = frappe.get_doc("Coop Loan", loan_name)
	needed = cint(frappe.db.get_value("Coop Loan Product", doc.product, "guarantors_required")) if doc.product else 0
	if not needed and not doc.guarantors:
		return None
	accepted = [g for g in doc.guarantors if g.status == "Accepted"]
	if any(g.status == "Declined" for g in doc.guarantors):
		return _("A guarantor declined. The borrower needs to ask someone else.")
	if len(accepted) < max(needed, len(doc.guarantors)):
		return _("Waiting for guarantors: {0} of {1} accepted").format(len(accepted), max(needed, len(doc.guarantors)))
	return None


def release_guarantees(loan_name):
	for g in frappe.get_all("Coop Loan Guarantor", filters={"parent": loan_name, "status": ["in", ["Pending", "Accepted"]]},
			pluck="name"):
		frappe.db.set_value("Coop Loan Guarantor", g, "status", "Released", update_modified=False)


# Restructuring ---------------------------------------------------------------------------


def restructure(loan_name, months, reason=None):
	"""Spread what's still owed over `months` new monthly instalments from next month. No extra
	interest: what's been paid stays where it is, and unpaid principal, interest and penalties move
	to the new instalments."""
	doc = frappe.get_doc("Coop Loan", loan_name)
	if doc.status != "Active":
		frappe.throw(_("Only an active loan can be restructured"))
	months = max(1, cint(months))
	filled = fill(doc.instalments, paid_totals(doc.name))
	keep, owed = [], {"penalty": 0.0, "interest": 0.0, "principal": 0.0}
	for f in filled:
		r = f.row
		if f.owed <= TOL:
			keep.append({k: r.get(k) for k in ("due_date", "principal", "interest", "penalty", "penalty_months", "settled")})
			continue
		paid_any = any(v > TOL for v in f.paid.values())
		if paid_any:
			# Keep the part already paid as a settled instalment, so payments stay where they are
			keep.append({"due_date": r.due_date, "principal": f.paid["principal"], "interest": f.paid["interest"],
				"penalty": f.paid["penalty"], "settled": 1})
		for kind in owed:
			owed[kind] = flt(owed[kind] + flt(r.get(kind)) - f.paid[kind], 2)
	outstanding = flt(sum(owed.values()), 2)
	if outstanding <= TOL:
		frappe.throw(_("Nothing is left to restructure"))
	start = getdate(today())
	each = {k: flt(v / months, 2) for k, v in owed.items()}
	new = []
	for k in range(1, months + 1):
		last = k == months
		new.append({
			"due_date": add_months(start, k),
			**{kind: flt(owed[kind] - each[kind] * (months - 1), 2) if last else each[kind] for kind in owed},
		})
	doc.set("instalments", [])
	for i, row in enumerate(keep + new, 1):
		doc.append("instalments", {"number": i, **row})
	doc.tenure_months = len(doc.instalments)
	doc.due_date = new[-1]["due_date"]
	doc.append("restructures", {"restructured_on": today(), "outstanding": outstanding, "new_months": months,
		"reason": reason, "approved_by": frappe.session.user})
	doc.restructure_months = None
	doc.restructure_reason = None
	doc.flags.ignore_validate = True
	doc.save(ignore_permissions=True)
	refresh(loan_name)
	return doc


# Eligibility -----------------------------------------------------------------------------


def eligibility(member, product, amount=None, months=None):
	"""Can this member take this loan? A checklist the app shows, and the most they can borrow."""
	from coopvault.ledger import contribution_standing, cooperative_totals, member_balances

	p = frappe.get_doc("Coop Loan Product", product)
	m = frappe.db.get_value("Coop Member", member, ["name", "cooperative", "status", "joined_on", "creation",
		"payout_account_number", "security_hold_until"], as_dict=True)
	b = member_balances(member)
	pool = cooperative_totals(m.cooperative).pool_balance
	limit = max(0.0, (b.savings - b.withdrawal_requested - b.pledged) * flt(p.savings_multiplier) - b.exposure)
	if flt(p.max_amount):
		limit = min(limit, flt(p.max_amount))
	checks = []

	def check(key, ok, label):
		checks.append({"key": key, "ok": bool(ok), "label": label})

	check("active", m.status == "Active", _("Active member"))
	if cint(p.min_membership_months):
		joined = getdate(m.joined_on or m.creation)
		ready = add_months(joined, cint(p.min_membership_months))
		check("tenure", getdate(today()) >= ready, _("A member for {0} months (from {1})").format(
			cint(p.min_membership_months), frappe.utils.formatdate(ready, "d MMM yyyy")))
	if cint(p.require_up_to_date):
		standing = contribution_standing(member)
		check("contributions", not standing.get("tracked") or standing.state != "behind", _("Contributions up to date"))
	open_loans = frappe.db.count("Coop Loan", {"member": member, "status": ["in", OPEN_STATES]})
	check("loans", open_loans < cint(p.max_active_loans), _("At most {0} loan(s) at a time").format(cint(p.max_active_loans)))
	check("payout", bool(m.payout_account_number), _("A bank account to receive the money"))
	if amount is not None:
		amount = flt(amount)
		check("amount", flt(p.min_amount) <= amount <= limit + TOL and amount > 0,
			_("Between {0} and {1}").format(_money(p.min_amount, m.cooperative), _money(limit, m.cooperative)))
	if months is not None:
		check("months", cint(p.min_tenure_months) <= cint(months) <= cint(p.max_tenure_months),
			_("{0} to {1} months").format(cint(p.min_tenure_months), cint(p.max_tenure_months)))
	return frappe._dict(
		product=p.name, checks=checks, ok=all(c["ok"] for c in checks), max_amount=flt(limit, 2), pool_balance=pool,
		savings=b.savings, pledged=b.pledged, exposure=b.exposure, security_hold_until=m.security_hold_until,
	)


def _money(amount, cooperative):
	from coopvault.utils import money_text

	return money_text(amount, frappe.db.get_value("Cooperative", cooperative, "currency"))
