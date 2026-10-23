"""Savings products for existing cooperatives.

Each cooperative gets its "Regular contributions" product on Members' savings (2100); every
existing contribution and withdrawal belongs to it, so balances and the books don't change.
Transfers to members' account numbers are shared out (dues, loan instalments, then savings) from
now on. Idempotent.
"""

import frappe

from coopvault import savings


def execute():
	for coop in frappe.get_all("Cooperative", pluck="name"):
		regular = savings.ensure_regular(coop)
		for doctype in ("Coop Contribution", "Coop Withdrawal"):
			for name in frappe.get_all(doctype, filters={"cooperative": coop, "savings_product": ["is", "not set"]},
					pluck="name"):
				if doctype == "Coop Withdrawal" and frappe.db.get_value(doctype, name, "leaving"):
					continue
				frappe.db.set_value(doctype, name, "savings_product", regular, update_modified=False)
		if frappe.db.get_value("Cooperative", coop, "auto_allocate") is None:
			frappe.db.set_value("Cooperative", coop, "auto_allocate", 1, update_modified=False)
