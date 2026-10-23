"""Approvals from the app: the queue, decisions, and each cooperative's approval rules."""

import json

import frappe
from frappe import _
from frappe.utils import flt

from coopvault import approvals
from coopvault.api import ROLES, get_membership


@frappe.whitelist()
def pending(cooperative):
	me = get_membership(cooperative, admin=True)
	return approvals.pending(cooperative, me.user)


@frappe.whitelist()
def get(name):
	doc = frappe.get_doc("Coop Approval", name)
	me = get_membership(doc.cooperative, allow_inactive=True)
	if not me.is_admin and frappe.session.user != doc.requested_by:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return approvals.info(doc)


@frappe.whitelist(methods=["POST"])
def decide(name, approve, note=None):
	"""Approve or reject the current stage. The action's own rules then take over (a loan becomes
	ready to send, a journal is posted, and so on)."""
	doc = frappe.get_doc("Coop Approval", name)
	get_membership(doc.cooperative, admin=True)
	approve = approve in (True, 1, "1", "true", "True")
	if not approve and not (note or "").strip() and doc.action != "Member admission":
		frappe.throw(_("Say why, so they know what to change"))
	return approvals.info(approvals.decide(name, approve, (note or "").strip() or None))


@frappe.whitelist()
def for_reference(reference_doctype, reference_name):
	doc = frappe.get_doc(reference_doctype, reference_name)
	cooperative = doc.get("cooperative")
	me = get_membership(cooperative, allow_inactive=True)
	mine = doc.get("member") == me.name or doc.get("user") == me.user
	if not (me.is_admin or mine):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return approvals.for_reference(reference_doctype, reference_name)


# Rules ----------------------------------------------------------------------------------------


@frappe.whitelist()
def rules(cooperative):
	get_membership(cooperative, admin=True)
	approvals.ensure_default_rules(cooperative)
	out = []
	for r in frappe.get_all("Coop Approval Rule", filters={"cooperative": cooperative},
			fields=["name", "action", "min_amount", "enabled"], order_by="action asc, min_amount asc"):
		r.stages = [{"label": s.label, "roles": approvals.roles_of(s.roles)}
			for s in frappe.get_doc("Coop Approval Rule", r.name).stages]
		out.append(r)
	return {"rules": out, "actions": approvals.ACTIONS, "roles": [r for r in ROLES if r != "Member"]}


@frappe.whitelist(methods=["POST"])
def save_rule(cooperative, action, stages, min_amount=0, name=None, enabled=1):
	"""Create or change a rule. stages: [{"label": "Final approval", "roles": ["Chairman", "Admin"]}]."""
	get_membership(cooperative, capability="settings")
	if action not in approvals.ACTIONS:
		frappe.throw(_("Unknown action"))
	stages = json.loads(stages) if isinstance(stages, str) else stages
	clean = []
	for i, st in enumerate(stages or [], 1):
		roles = [r for r in (st.get("roles") or []) if r in ROLES and r != "Member"]
		if not roles:
			frappe.throw(_("Choose who can approve stage {0}").format(i))
		clean.append({"label": (st.get("label") or "").strip() or _("Stage {0}").format(i), "roles": ", ".join(roles)})
	if not clean:
		frappe.throw(_("A rule needs at least one stage"))
	if len(clean) > 4:
		frappe.throw(_("Use at most four stages"))
	doc = frappe.get_doc("Coop Approval Rule", name) if name else frappe.new_doc("Coop Approval Rule")
	if name and doc.cooperative != cooperative:
		frappe.throw(_("Rule not found"))
	clash = frappe.db.exists("Coop Approval Rule", {"cooperative": cooperative, "action": action,
		"min_amount": flt(min_amount), "name": ["!=", name or ""]})
	if clash:
		frappe.throw(_("There's already a rule for this from the same amount. Change that one instead."))
	doc.update({"cooperative": cooperative, "action": action, "min_amount": flt(min_amount),
		"enabled": int(bool(int(enabled))), "stages": clean})
	doc.save(ignore_permissions=True)
	_keep_base_rule(cooperative, action)
	return rules(cooperative)


@frappe.whitelist(methods=["POST"])
def delete_rule(name):
	doc = frappe.get_doc("Coop Approval Rule", name)
	get_membership(doc.cooperative, capability="settings")
	others = frappe.get_all("Coop Approval Rule", filters={"cooperative": doc.cooperative, "action": doc.action,
		"name": ["!=", name], "enabled": 1}, pluck="min_amount")
	if not any(flt(m) == 0 for m in others):
		frappe.throw(_("Every action needs a rule that applies from zero. Change this one instead of deleting it."))
	doc.delete(ignore_permissions=True)
	return rules(doc.cooperative)


def _keep_base_rule(cooperative, action):
	"""Requests of any size need a rule: keep one that starts from zero."""
	if not frappe.db.exists("Coop Approval Rule", {"cooperative": cooperative, "action": action, "min_amount": 0,
			"enabled": 1}):
		frappe.throw(_("Keep an enabled rule for {0} that applies from zero").format(_(action).lower()))
