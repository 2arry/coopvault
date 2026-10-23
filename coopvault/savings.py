"""Savings products, and sharing one payment across what a member owes.

Every group has one "Regular contributions" product: the dues members owe each period, booked to
Members' savings (2100). A group can add voluntary products (a Christmas fund, school fees, a
target savings plan). Each voluntary product has its own liability account in the books, so what
members hold in it is read from the journal like any other balance.

A product says whether its savings count towards the borrowing limit and whether they can be
withdrawn any time or only when the member leaves the group.

One payment can be shared out ("Split"): contributions due first, then loan instalments that are
due, then whatever is left goes to savings (the group's chosen product; regular contributions by
default, which counts as paying ahead).
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from coopvault import accounting

REGULAR = "Regular contributions"
VOLUNTARY = "Voluntary savings"
ANY_TIME = "Any time"
ON_LEAVING = "Only when leaving"
TOL = 0.005
DUE_WINDOW_DAYS = 31  # a loan instalment falling due within this many days counts as due now
FIELDS = ["name", "product_name", "kind", "enabled", "description", "account", "withdrawal_rule",
	"counts_for_borrowing"]


# Products --------------------------------------------------------------------------------


def ensure_regular(cooperative):
	"""The group's regular contributions product (created on first use)."""
	name = frappe.db.get_value("Coop Savings Product", {"cooperative": cooperative, "kind": REGULAR})
	if name:
		return name
	return frappe.get_doc(
		{
			"doctype": "Coop Savings Product",
			"cooperative": cooperative,
			"product_name": _("Regular contributions"),
			"kind": REGULAR,
			"enabled": 1,
			"description": _("The contribution every member makes each period"),
			"withdrawal_rule": ANY_TIME,
			"counts_for_borrowing": 1,
		}
	).insert(ignore_permissions=True).name


def regular(cooperative):
	cache = getattr(frappe.local, "coop_regular_savings", None)
	if cache is None:
		cache = frappe.local.coop_regular_savings = {}
	if cooperative not in cache:
		cache[cooperative] = ensure_regular(cooperative)
	return cache[cooperative]


def products(cooperative, enabled_only=False):
	"""The group's savings products, regular contributions first."""
	regular(cooperative)
	filters = {"cooperative": cooperative}
	if enabled_only:
		filters["enabled"] = 1
	rows = frappe.get_all("Coop Savings Product", filters=filters, fields=FIELDS, order_by="creation asc")
	return sorted(rows, key=lambda p: p.kind != REGULAR)


def get(cooperative, product):
	"""A product of this group (the regular one when none is given)."""
	name = product or regular(cooperative)
	p = frappe.db.get_value("Coop Savings Product", name, FIELDS + ["cooperative"], as_dict=True)
	if not p or p.cooperative != cooperative:
		frappe.throw(_("Savings product not found"))
	return p


def account_for(cooperative, product):
	return get(cooperative, product).account or accounting.account(cooperative, "Member Savings")


def _next_code(cooperative):
	used = set(frappe.get_all("Coop Account", filters={"cooperative": cooperative}, pluck="account_code"))
	for code in [*range(2110, 2200, 10), *range(2101, 2200)]:
		if str(code) not in used:
			return str(code)
	frappe.throw(_("There's no room left in the chart for another savings product"))


def validate_product(doc):
	doc.product_name = (doc.product_name or "").strip()
	if not doc.product_name:
		frappe.throw(_("Give the savings product a name"))
	if frappe.db.exists("Coop Savings Product", {"cooperative": doc.cooperative, "product_name": doc.product_name,
			"name": ["!=", doc.name or ""]}):
		frappe.throw(_("There's already a savings product called {0}").format(doc.product_name))
	before = None if doc.is_new() else doc.get_doc_before_save()
	if before and before.kind != doc.kind:
		frappe.throw(_("A savings product can't change between regular and voluntary"))
	if doc.kind == REGULAR:
		if frappe.db.exists("Coop Savings Product", {"cooperative": doc.cooperative, "kind": REGULAR,
				"name": ["!=", doc.name or ""]}):
			frappe.throw(_("A group has one regular contributions product. Add voluntary savings instead."))
		doc.enabled = 1
		doc.account = accounting.account(doc.cooperative, "Member Savings")
	elif not doc.account:
		# Each voluntary product keeps its own liability account, so the books show what's in it
		doc.account = frappe.get_doc(
			{
				"doctype": "Coop Account",
				"cooperative": doc.cooperative,
				"account_code": _next_code(doc.cooperative),
				"account_name": doc.product_name,
				"root_type": "Liability",
				"account_type": "Member Savings",
				"is_system": 1,
			}
		).insert(ignore_permissions=True).name
	elif before and before.product_name != doc.product_name:
		frappe.db.set_value("Coop Account", doc.account, "account_name", doc.product_name)


# A member's savings by product -------------------------------------------------------------


def _pending_withdrawals(member, exclude_withdrawal=None):
	filters = {"member": member, "status": ["in", ["Pending", "Approved"]]}
	if exclude_withdrawal:
		filters["name"] = ["!=", exclude_withdrawal]
	return frappe.get_all("Coop Withdrawal", filters=filters, fields=["amount", "savings_product", "leaving"])


def position(member, hold=0.0, exclude_withdrawal=None):
	"""What a member holds in each product and what can come out of it.

	hold: savings that must stay because they back the member's loans, loan requests or the
	guarantees they've given. Only savings that count towards borrowing back those; savings that
	can only be withdrawn on leaving cover the hold first, so they don't lock anything else up.
	"""
	m = frappe.db.get_value("Coop Member", member, ["cooperative", "status"], as_dict=True)
	left = m.status == "Left"
	by_account = accounting.member_savings_by_account(member)
	rows = []
	for p in products(m.cooperative):
		rows.append(frappe._dict(
			product=p.name, product_name=p.product_name, kind=p.kind, enabled=p.enabled,
			withdrawal_rule=p.withdrawal_rule, counts_for_borrowing=p.counts_for_borrowing,
			balance=flt(by_account.pop(p.account, 0), 2), requested=0.0,
		))
	# Anything on another savings account (none normally) counts as regular contributions
	rows[0].balance = flt(rows[0].balance + sum(by_account.values()), 2)

	by_product = {r.product: r for r in rows}
	for w in _pending_withdrawals(member, exclude_withdrawal):
		if w.leaving:
			left_to_take = flt(w.amount)
			for r in rows:
				take = min(left_to_take, max(0.0, r.balance - r.requested))
				r.requested += take
				left_to_take -= take
		else:
			(by_product.get(w.savings_product) or rows[0]).requested += flt(w.amount)

	for r in rows:
		r.requested = flt(r.requested, 2)
		r.free = max(0.0, flt(r.balance - r.requested, 2))
		r.locked = r.withdrawal_rule == ON_LEAVING and not left
	counting_free = flt(sum(r.free for r in rows if r.counts_for_borrowing), 2)
	room = max(0.0, flt(counting_free - flt(hold), 2))
	open_counting = flt(sum(r.free for r in rows if r.counts_for_borrowing and not r.locked), 2)
	for r in rows:
		if r.locked:
			r.withdrawable = 0.0
		elif r.counts_for_borrowing:
			r.withdrawable = min(r.free, room)
		else:
			r.withdrawable = r.free
	withdrawable = flt(sum(r.withdrawable for r in rows if not r.counts_for_borrowing) + min(open_counting, room), 2)
	return frappe._dict(
		rows=rows,
		total=flt(sum(r.balance for r in rows), 2),
		requested=flt(sum(r.requested for r in rows), 2),
		counting_free=counting_free,
		withdrawable=withdrawable,
		# What can back a guarantee: savings that count for borrowing and aren't held already
		pledgeable=room,
		# Leaving takes everything at once, but only when nothing is held back
		leaving_amount=flt(sum(r.free for r in rows), 2) if flt(hold) <= TOL else 0.0,
	)


def dues_paid(member):
	"""Confirmed contributions to the regular product (what counts against the contribution plan)."""
	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	value = frappe.db.sql(
		"""select coalesce(sum(amount), 0) from `tabCoop Contribution`
		where member = %s and status = 'Confirmed'
			and (savings_product is null or savings_product = '' or savings_product = %s)""",
		(member, regular(cooperative)),
	)
	return flt(value[0][0], 2)


def withdrawal_lines(doc):
	"""The savings accounts a sent withdrawal comes out of: its product, or when someone leaves,
	every product in turn (regular contributions take any remainder)."""
	if not doc.get("leaving"):
		return [(account_for(doc.cooperative, doc.savings_product), flt(doc.amount))]
	by_account = accounting.member_savings_by_account(doc.member)
	remaining, lines = flt(doc.amount), []
	for p in products(doc.cooperative):
		take = min(remaining, max(0.0, flt(by_account.get(p.account))))
		if take > TOL:
			lines.append((p.account, flt(take, 2)))
			remaining = flt(remaining - take, 2)
	if remaining > TOL:
		lines.append((account_for(doc.cooperative, None), remaining))
	return lines


# Sharing a payment out ----------------------------------------------------------------------


def _loan_due(loan):
	"""What's due on an active loan now: overdue instalments plus the next one falling due soon."""
	from coopvault.ledger import loan_schedule

	sched = loan_schedule(loan)
	if not sched:
		return 0.0
	window = getdate(add_days(today(), DUE_WINDOW_DAYS))
	if sched.rows and "owed" in sched.rows[0]:
		due = sum(flt(r.owed) for r in sched.rows if getdate(r.due_date) <= window)
	else:
		due = flt(sched.overdue_amount)
		if sched.next_due_date and getdate(today()) <= getdate(sched.next_due_date) <= window:
			due += flt(sched.next_amount)
	return flt(min(due, flt(loan.balance)), 2)


def due_lines(member):
	"""What a member owes right now, in the order a payment pays it."""
	from coopvault.ledger import contribution_standing

	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	lines = []
	standing = contribution_standing(member)
	if standing.get("tracked") and flt(standing.due_now) > TOL:
		lines.append(frappe._dict(kind="savings", savings_product=regular(cooperative), amount=flt(standing.due_now, 2),
			label=_("Contributions due")))
	loans = frappe.get_all(
		"Coop Loan",
		filters={"member": member, "status": "Active"},
		fields=["name", "status", "balance", "tenure_months", "total_payable", "amount_repaid", "received_on",
			"disbursed_on", "product"],
		order_by="disbursed_on asc, creation asc",
	)
	for ln in loans:
		due = _loan_due(ln)
		if due > TOL:
			product = ln.product and frappe.db.get_value("Coop Loan Product", ln.product, "product_name")
			lines.append(frappe._dict(kind="loan", loan=ln.name, amount=due,
				label=_("{0} instalment").format(product or _("Loan"))))
	return lines


def extra_product(cooperative):
	chosen = frappe.db.get_value("Cooperative", cooperative, "extra_savings_product")
	if chosen and frappe.db.get_value("Coop Savings Product", chosen, "enabled"):
		return chosen
	return regular(cooperative)


def share_out(member, amount):
	"""Share an amount across what's due, the rest to savings. Returns allocation lines."""
	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	remaining, out = flt(amount, 2), []
	for line in due_lines(member):
		take = min(remaining, line.amount)
		if take > TOL:
			out.append({**line, "amount": flt(take, 2)})
			remaining = flt(remaining - take, 2)
	if remaining > TOL:
		add_savings(out, extra_product(cooperative), remaining)
	return out


def add_savings(lines, product, amount):
	for line in lines:
		if line.get("kind") == "savings" and line.get("savings_product") == product:
			line["amount"] = flt(flt(line["amount"]) + flt(amount), 2)
			return lines
	lines.append({"kind": "savings", "savings_product": product, "amount": flt(amount, 2)})
	return lines


def check_allocations(member, allocations, currency=None):
	"""Clean up and check the lines a member chose for one payment."""
	from coopvault.utils import money_text

	cooperative = frappe.db.get_value("Coop Member", member, "cooperative")
	out = []
	for a in allocations or []:
		amount = flt(a.get("amount"), 2)
		if amount <= 0:
			continue
		if a.get("kind") == "loan":
			ln = frappe.db.get_value("Coop Loan", a.get("loan"), ["member", "status", "balance"], as_dict=True)
			if not ln or ln.member != member:
				frappe.throw(_("Loan not found"))
			if ln.status != "Active":
				frappe.throw(_("Loan {0} isn't active").format(a.get("loan")))
			if amount > flt(ln.balance) + TOL:
				frappe.throw(_("That's more than the {0} left on loan {1}").format(money_text(ln.balance, currency),
					a.get("loan")))
			out.append({"kind": "loan", "loan": a.get("loan"), "amount": amount})
		elif a.get("kind") == "savings":
			p = get(cooperative, a.get("savings_product"))
			if not p.enabled:
				frappe.throw(_("{0} isn't open for saving").format(p.product_name))
			add_savings(out, p.name, amount)
		else:
			frappe.throw(_("Choose what each part of the payment is for"))
	if not out:
		frappe.throw(_("Enter an amount for at least one item"))
	return out


def describe(cooperative, line):
	"""A short label for an allocation line, e.g. "Christmas savings" or "Loan LN-0004"."""
	if line.get("kind") == "loan":
		return _("Loan {0}").format(line.get("loan"))
	return frappe.db.get_value("Coop Savings Product", line.get("savings_product") or regular(cooperative),
		"product_name")
