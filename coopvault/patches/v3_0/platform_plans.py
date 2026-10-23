"""Plans for the groups that were here before subscriptions.

Default plans are created; every existing group goes on the default plan with three months
covered (its admins see the date in Manage → Settings → Plan), logged as a platform action.
Inactive groups stay as they are. Idempotent.
"""

import frappe
from frappe.utils import add_months, today

from coopvault import plans

EARLY_MONTHS = 3


def execute():
	plans.ensure_default_plans()
	default = plans.default_plan()
	for c in frappe.get_all("Cooperative", fields=["name", "status", "plan", "paid_until", "trial_ends_on", "creation"]):
		if c.plan or c.paid_until or c.trial_ends_on:
			continue
		until = add_months(today(), EARLY_MONTHS)
		frappe.db.set_value("Cooperative", c.name, {"plan": default, "paid_until": until,
			"approved_on": c.creation if c.status == "Active" else None}, update_modified=False)
		frappe.get_doc({"doctype": "Coop Platform Action", "cooperative": c.name, "action": "Subscription extended",
			"note": f"Joined before subscriptions: {EARLY_MONTHS} months included", "done_by": "Administrator",
			"plan": default, "paid_until": until}).insert(ignore_permissions=True)
