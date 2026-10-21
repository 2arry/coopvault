import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint

from coopvault import sessions
from coopvault.api import require_login
from coopvault.notify import endpoint_hash, public_key

FIELDS = ["name", "title", "body", "link", "kind", "read", "cooperative", "creation"]


@frappe.whitelist()
def list_notifications(limit=30, start=0):
	user = require_login()
	return {
		"items": frappe.get_all(
			"Coop Notification", filters={"user": user}, fields=FIELDS, order_by="creation desc",
			limit=min(cint(limit) or 30, 100), start=cint(start),
		),
		"unread": frappe.db.count("Coop Notification", {"user": user, "read": 0}),
	}


@frappe.whitelist()
def unread_count():
	return frappe.db.count("Coop Notification", {"user": require_login(), "read": 0})


@frappe.whitelist(methods=["POST"])
def mark_read(names=None, all=0):
	user = require_login()
	if cint(all):
		names = frappe.get_all("Coop Notification", filters={"user": user, "read": 0}, pluck="name")
	elif isinstance(names, str):
		names = json.loads(names) if names.startswith("[") else [names]
	for name in names or []:
		if frappe.db.get_value("Coop Notification", name, "user") == user:
			frappe.db.set_value("Coop Notification", name, "read", 1, update_modified=False)
	return {"unread": frappe.db.count("Coop Notification", {"user": user, "read": 0})}


@frappe.whitelist()
def push_config():
	require_login()
	return {"public_key": public_key()}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60 * 10)
def subscribe(subscription, discreet=None):
	"""Save a browser's push subscription (from PushManager.subscribe()), tied to this login so
	signing out removes it."""
	user = require_login()
	sub = json.loads(subscription) if isinstance(subscription, str) else subscription
	endpoint = (sub or {}).get("endpoint") or ""
	keys = (sub or {}).get("keys") or {}
	if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
		frappe.throw(_("Invalid push subscription"))
	h = endpoint_hash(endpoint)
	existing = frappe.db.get_value("Coop Push Subscription", {"endpoint_hash": h})
	values = {
		"user": user,
		"endpoint": endpoint,
		"endpoint_hash": h,
		"p256dh": keys["p256dh"],
		"auth": keys["auth"],
		"user_agent": (frappe.get_request_header("User-Agent") or "")[:140],
		"session": sessions.current(),
	}
	if discreet is not None:
		values["discreet"] = cint(discreet)
	if existing:
		frappe.db.set_value("Coop Push Subscription", existing, values)
	else:
		frappe.get_doc({"doctype": "Coop Push Subscription", **values}).insert(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def set_privacy(endpoint, discreet):
	"""Hide amounts and names on this device's lock screen."""
	user = require_login()
	name = frappe.db.get_value("Coop Push Subscription", {"endpoint_hash": endpoint_hash(endpoint), "user": user})
	if not name:
		frappe.throw(_("Turn on notifications on this device first"))
	frappe.db.set_value("Coop Push Subscription", name, "discreet", cint(discreet))
	return {"discreet": cint(discreet)}


@frappe.whitelist()
def device_status(endpoint):
	user = require_login()
	row = frappe.db.get_value(
		"Coop Push Subscription", {"endpoint_hash": endpoint_hash(endpoint), "user": user}, ["discreet"], as_dict=True
	)
	return {"subscribed": bool(row), "discreet": bool(row and row.discreet)}


@frappe.whitelist(methods=["POST"])
def unsubscribe(endpoint):
	user = require_login()
	name = frappe.db.get_value("Coop Push Subscription", {"endpoint_hash": endpoint_hash(endpoint), "user": user})
	if name:
		frappe.db.delete("Coop Push Subscription", name)
	return {"ok": True}
