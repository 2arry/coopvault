"""Remove one cooperative and everything recorded under it. bench --site X execute ... --kwargs {cooperative}"""
import frappe


def run(cooperative):
	coop = frappe.get_doc("Cooperative", cooperative)
	members = frappe.get_all("Coop Member", filters={"cooperative": cooperative}, pluck="name")
	counts = {}
	for dt in ("Coop Payment Transaction", "Coop Loan Repayment", "Coop Contribution", "Coop Withdrawal", "Coop Loan",
			"Coop Announcement", "Coop Notification"):
		if frappe.db.has_column(dt, "cooperative"):
			counts[dt] = frappe.db.count(dt, {"cooperative": cooperative})
			frappe.db.delete(dt, {"cooperative": cooperative})
	if members:
		for dt in ("Coop Payment Authorization", "Coop Dedicated Account"):
			if frappe.db.exists("DocType", dt) and frappe.db.has_column(dt, "member"):
				counts[dt] = frappe.db.count(dt, {"member": ["in", members]})
				frappe.db.delete(dt, {"member": ["in", members]})
	counts["Coop Member"] = len(members)
	frappe.db.delete("Coop Member", {"cooperative": cooperative})
	frappe.db.delete("Coop Contribution Plan", {"parent": cooperative})
	frappe.db.delete("Cooperative", {"name": cooperative})
	frappe.db.commit()
	print("removed", coop.cooperative_name, counts)
