"""Scheduled jobs that aren't tied to one API module."""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, today

from coopvault import loans
from coopvault.ledger import TRACKED, contribution_standing, plan_on, plans_for
from coopvault.notify import holders, notify
from coopvault.utils import money_text

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
				_("{0} due in {1} days").format(money_text(s.due_now, coop.currency), REMIND_DAYS_BEFORE_END),
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


def loans_daily():
	"""Daily: late penalties and arrears on active loans, a reminder three days before an
	instalment is due, and a heads-up to the borrower and the loan officers once it's late."""
	soon = add_days(getdate(today()), 3)
	for l in frappe.get_all("Coop Loan", filters={"status": "Active"}, fields=["name", "cooperative", "user", "member_name"]):
		try:
			added = loans.accrue_penalties(l.name)
			loans.refresh(l.name)
			st = loans.state(l.name)
			currency = frappe.db.get_value("Cooperative", l.cooperative, "currency")
			link = f"/loans/{l.name}"
			if added:
				notify(l.user, _("A late fee of {0} was added to your loan").format(money_text(added, currency)),
					_("{0} is overdue. Pay it to stop more fees.").format(money_text(st.overdue_amount, currency)),
					link=link, cooperative=l.cooperative, kind="loan-overdue")
			elif st.next_due_date and getdate(st.next_due_date) == soon:
				notify(l.user, _("{0} due on your loan in 3 days").format(money_text(st.next_amount, currency)),
					link=link, cooperative=l.cooperative, kind="loan-due")
			if st.days_overdue == 1:
				notify(l.user, _("Your loan payment is overdue"), _("{0} is due now.").format(money_text(st.overdue_amount, currency)),
					link=link, cooperative=l.cooperative, kind="loan-overdue")
				notify(holders(l.cooperative, "loans", exclude_users=[l.user]),
					_("{0}'s loan payment is overdue").format(l.member_name), money_text(st.overdue_amount, currency),
					link=link, cooperative=l.cooperative, kind="loan-overdue")
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Loan check failed for {l.name}", message=frappe.get_traceback())

