"""Scheduled jobs that aren't tied to one API module."""

import frappe
from frappe import _
from frappe.utils import date_diff, fmt_money, getdate, today

from coopvault.ledger import TRACKED, contribution_standing, plan_on, plans_for
from coopvault.notify import notify

REMIND_DAYS_BEFORE_END = 3


def remind_due_contributions():
	"""Daily: a nudge three days before the period ends to members who still owe for it."""
	now = getdate(today())
	for coop in frappe.get_all("Cooperative", filters={"status": "Active"}, fields=["name", "currency"]):
		if plan_on(plans_for(coop.name), now).frequency not in TRACKED:
			continue
		for m in frappe.get_all(
			"Coop Member",
			filters={"cooperative": coop.name, "status": "Active"},
			fields=["name", "user", "due_reminded_for", "autosave_enabled"],
		):
			s = contribution_standing(m.name)
			if s.get("state") not in ("due", "behind") or not s.current_period_end:
				continue
			if date_diff(s.current_period_end, now) != REMIND_DAYS_BEFORE_END:
				continue
			if str(m.due_reminded_for or "") == str(s.current_period_start):
				continue
			if m.autosave_enabled and s.state == "due":
				continue  # Autosave will take care of it
			notify(
				m.user,
				_("{0} due in {1} days").format(fmt_money(s.due_now, currency=coop.currency), REMIND_DAYS_BEFORE_END),
				_("Pay in a tap to stay up to date."),
				link="/save",
				cooperative=coop.name,
				kind="due-reminder",
			)
			frappe.db.set_value("Coop Member", m.name, "due_reminded_for", s.current_period_start, update_modified=False)
		frappe.db.commit()


def daily_cleanup():
	from coopvault import notify as notify_module
	from coopvault import sessions

	sessions.cleanup()
	notify_module.cleanup()
	frappe.db.delete("Coop Password Reset", {"expires_on": ["<", frappe.utils.add_days(today(), -30)]})
	frappe.db.commit()
