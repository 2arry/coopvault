"""Approvals: nothing sensitive happens on one person's say-so.

Each cooperative has rules per action (admitting a member, a loan, a withdrawal, an offline
payment, a manual journal, a share-out). A rule is a list of stages, each naming the roles that
may approve it, and can start from an amount, so big loans can need the Chairman as well.

Separation of duties, always: nobody approves a request they made, one that benefits them, or
two stages of the same request. If nobody else in the group holds a stage's roles, an Admin covers
it (and the step says so).

When the last stage approves (or any stage rejects), the action's handler finishes the job:
the member becomes active, the loan is ready to send, the journal is posted, and so on.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

ACTIONS = ("Member admission", "Loan", "Withdrawal", "Offline payment", "Manual journal", "Share-out")

# (applies from amount, [(stage label, roles)])
DEFAULT_RULES = {
	"Member admission": [(0, [("Approve the member", "Admin, Chairman, Treasurer")])],
	"Loan": [
		(0, [("Review", "Loan Officer, Treasurer, Admin")]),
		(500000, [("Review", "Loan Officer, Treasurer, Admin"), ("Final approval", "Chairman, Admin")]),
	],
	"Withdrawal": [(0, [("Approve", "Treasurer, Chairman, Admin")])],
	"Offline payment": [(0, [("Check it arrived", "Treasurer, Accountant, Admin")])],
	"Manual journal": [(0, [("Approve", "Accountant, Treasurer, Chairman, Admin")])],
	"Share-out": [(0, [("Approve", "Admin, Chairman")])],
}

HANDLERS = {
	"Member admission": "coopvault.api.coop.on_admission_decided",
	"Loan": "coopvault.api.finance.on_loan_decided",
	"Withdrawal": "coopvault.api.finance.on_withdrawal_decided",
	"Offline payment": "coopvault.api.finance.on_offline_payment_decided",
	"Manual journal": "coopvault.api.accounts.on_journal_decided",
	"Share-out": "coopvault.api.accounts.on_share_out_decided",
}


def roles_of(text):
	return [r.strip() for r in (text or "").split(",") if r.strip()]


# Rules ------------------------------------------------------------------------------------


def ensure_default_rules(cooperative):
	have = set(frappe.get_all("Coop Approval Rule", filters={"cooperative": cooperative}, pluck="action"))
	for action, rules in DEFAULT_RULES.items():
		if action in have:
			continue
		for min_amount, stages in rules:
			frappe.get_doc(
				{
					"doctype": "Coop Approval Rule",
					"cooperative": cooperative,
					"action": action,
					"min_amount": min_amount,
					"enabled": 1,
					"stages": [{"label": label, "roles": roles} for label, roles in stages],
				}
			).insert(ignore_permissions=True)


def stages_for(cooperative, action, amount=0):
	"""The stages of the rule that applies: the enabled rule with the highest starting amount at or
	below this amount."""
	rules = frappe.get_all(
		"Coop Approval Rule",
		filters={"cooperative": cooperative, "action": action, "enabled": 1, "min_amount": ["<=", flt(amount)]},
		fields=["name"],
		order_by="min_amount desc",
		limit=1,
	)
	if rules:
		rule = frappe.get_doc("Coop Approval Rule", rules[0].name)
		return [(s.label or _("Stage {0}").format(i), s.roles) for i, s in enumerate(rule.stages, 1)]
	chosen = [stages for min_amount, stages in DEFAULT_RULES[action] if flt(amount) >= min_amount]
	return chosen[-1]


# Requests ---------------------------------------------------------------------------------


def _holders(cooperative, roles, exclude_users):
	return [
		u for u in frappe.get_all("Coop Member", filters={"cooperative": cooperative, "status": "Active",
			"role": ["in", roles or [""]]}, pluck="user")
		if u not in exclude_users
	]


def open_request(reference_doctype, reference_name):
	name = frappe.db.get_value("Coop Approval", {"reference_doctype": reference_doctype,
		"reference_name": reference_name, "status": "Pending"})
	return frappe.get_doc("Coop Approval", name) if name else None


def request(cooperative, action, reference, amount=0, beneficiary=None, summary=None, requested_by=None, notify=True):
	"""Start an approval for a document. Returns the existing one if it's already waiting."""
	existing = open_request(*reference)
	if existing:
		return existing
	requested_by = requested_by or frappe.session.user
	excluded = {requested_by}
	if beneficiary:
		excluded.add(frappe.db.get_value("Coop Member", beneficiary, "user"))
	steps = [
		{"stage_no": i, "label": label, "roles": roles, "status": "Waiting"}
		for i, (label, roles) in enumerate(stages_for(cooperative, action, amount), 1)
	]
	doc = frappe.get_doc(
		{
			"doctype": "Coop Approval",
			"cooperative": cooperative,
			"action": action,
			"status": "Pending",
			"summary": summary,
			"amount": flt(amount),
			"reference_doctype": reference[0],
			"reference_name": reference[1],
			"requested_by": requested_by,
			"beneficiary": beneficiary,
			"current_stage": 1,
			"steps": steps,
		}
	).insert(ignore_permissions=True)
	if notify:
		_notify_approvers(doc)
	return doc


def _current(doc):
	return next((s for s in doc.steps if s.stage_no == doc.current_stage), None)


def _excluded(doc):
	"""People who can't decide the current stage: whoever asked, whoever benefits, earlier deciders."""
	out = {doc.requested_by} | {s.decided_by for s in doc.steps if s.decided_by and s.stage_no < doc.current_stage}
	if doc.beneficiary:
		out.add(frappe.db.get_value("Coop Member", doc.beneficiary, "user"))
	return out


def approver_roles(doc, step=None):
	"""Roles that can decide a stage now. If nobody else in the group holds the stage's roles, an Admin
	covers it; worked out each time, so appointing (say) a Chairman later takes over from the Admin."""
	step = step or _current(doc)
	roles = roles_of(step.roles)
	if _holders(doc.cooperative, roles, _excluded(doc)):
		return roles, False
	return ["Admin"], "Admin" not in roles


def why_not(doc, user=None):
	"""Why this user can't decide the current stage, or None if they can."""
	user = user or frappe.session.user
	if doc.status != "Pending":
		return _("This has already been {0}").format(_(doc.status).lower())
	step = _current(doc)
	me = frappe.db.get_value("Coop Member", {"cooperative": doc.cooperative, "user": user}, ["name", "role", "status"],
		as_dict=True)
	if not me or me.status != "Active":
		return _("Only the cooperative's officers can approve this")
	if user == doc.requested_by:
		return _("You asked for this, so someone else has to approve it")
	if doc.beneficiary and me.name == doc.beneficiary:
		return _("This is for you, so someone else has to approve it")
	if any(s.decided_by == user for s in doc.steps if s.stage_no < doc.current_stage):
		return _("You approved an earlier stage, so someone else approves this one")
	roles, _covering = approver_roles(doc, step)
	if me.role not in roles:
		return _("Waiting for: {0}").format(", ".join(roles))
	return None


def decide(name, approve, note=None):
	doc = frappe.get_doc("Coop Approval", name, for_update=True)
	reason = why_not(doc)
	if reason:
		frappe.throw(reason, frappe.PermissionError)
	step = _current(doc)
	step.fallback = 1 if approver_roles(doc, step)[1] else 0
	step.status = "Approved" if approve else "Rejected"
	step.decided_by = frappe.session.user
	step.decided_on = now_datetime()
	step.note = note
	if not approve:
		doc.status = "Rejected"
		doc.completed_on = now_datetime()
	elif doc.current_stage < len(doc.steps):
		doc.current_stage += 1
	else:
		doc.status = "Approved"
		doc.completed_on = now_datetime()
	doc.save(ignore_permissions=True)
	if doc.status in ("Approved", "Rejected"):
		frappe.get_attr(HANDLERS[doc.action])(doc, doc.status == "Approved", note)
	else:
		_notify_approvers(doc)
	return doc


def decide_for(reference_doctype, reference_name, approve, note=None):
	doc = open_request(reference_doctype, reference_name)
	if not doc:
		frappe.throw(_("Nothing is waiting for approval here"))
	return decide(doc.name, approve, note)


def cancel(reference_doctype, reference_name):
	for name in frappe.get_all("Coop Approval", filters={"reference_doctype": reference_doctype,
			"reference_name": reference_name, "status": "Pending"}, pluck="name"):
		frappe.db.set_value("Coop Approval", name, {"status": "Cancelled", "completed_on": now_datetime()})


def _notify_approvers(doc):
	from coopvault.notify import notify

	step = _current(doc)
	roles, _covering = approver_roles(doc, step)
	users = _holders(doc.cooperative, roles, _excluded(doc))
	stage = _(" ({0}, stage {1} of {2})").format(step.label, doc.current_stage, len(doc.steps)) if len(doc.steps) > 1 else ""
	notify(users, doc.summary or _("Waiting for your approval"), _("Waiting for your approval{0}. Open Manage → Review.").format(stage),
		link="/admin?tab=reviews", cooperative=doc.cooperative, kind="approval")


# For the app ------------------------------------------------------------------------------


def info(doc, user=None):
	"""An approval as the app shows it: stages, who decided, and whether this user can act."""
	users = {s.decided_by for s in doc.steps if s.decided_by} | {doc.requested_by}
	names = dict(frappe.get_all("User", filters={"name": ["in", [u for u in users if u] or [""]]},
		fields=["name", "full_name"], as_list=True))
	reason = why_not(doc, user) if doc.status == "Pending" else None
	live_roles, covering = approver_roles(doc) if doc.status == "Pending" else (None, False)
	nobody = doc.status == "Pending" and not _holders(doc.cooperative, live_roles, _excluded(doc))
	return {
		"name": doc.name,
		"action": doc.action,
		"status": doc.status,
		"summary": doc.summary,
		"amount": doc.amount,
		"reference_doctype": doc.reference_doctype,
		"reference_name": doc.reference_name,
		"requested_by_name": names.get(doc.requested_by),
		"current_stage": doc.current_stage,
		"can_decide": doc.status == "Pending" and not reason,
		"why_not": reason,
		# Nobody can decide it at the moment (e.g. the only admin asked for it): the group needs another officer
		"stuck": nobody,
		"steps": [
			{"stage_no": s.stage_no, "label": s.label, "status": s.status,
				"roles": ", ".join(live_roles) if s.stage_no == doc.current_stage and live_roles else s.roles,
				"fallback": bool(covering) if s.stage_no == doc.current_stage else bool(s.fallback),
				"decided_by_name": names.get(s.decided_by), "decided_on": s.decided_on, "note": s.note}
			for s in doc.steps
		],
		"creation": doc.creation,
	}


def for_reference(reference_doctype, reference_name, user=None):
	name = frappe.db.get_value("Coop Approval", {"reference_doctype": reference_doctype, "reference_name": reference_name},
		"name", order_by="creation desc")
	return info(frappe.get_doc("Coop Approval", name), user) if name else None


def pending(cooperative, user=None):
	"""Every approval waiting in a cooperative, with whether this user can decide it."""
	return [info(frappe.get_doc("Coop Approval", n), user) for n in frappe.get_all("Coop Approval",
		filters={"cooperative": cooperative, "status": "Pending"}, pluck="name", order_by="creation asc")]
