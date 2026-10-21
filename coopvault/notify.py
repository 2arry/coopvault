"""In-app notifications, with Web Push to phones that installed the app and allowed it.

No email or SMS: everything a member needs to act on lands in their CoopVault inbox, and
browsers/phones that subscribed also get a push message.
"""

import base64
import hashlib
import json

import frappe

STAFF_ROLES = ("Admin", "Treasurer")


def members_of(cooperative, roles=None, exclude_users=()):
	filters = {"cooperative": cooperative, "status": "Active"}
	if roles:
		filters["role"] = ["in", roles]
	users = frappe.get_all("Coop Member", filters=filters, pluck="user")
	return [u for u in users if u not in exclude_users]


def staff_of(cooperative, exclude_users=()):
	return members_of(cooperative, STAFF_ROLES, exclude_users)


def notify(users, title, body=None, link=None, cooperative=None, kind=None, push=True):
	"""Store a notification for each user and queue push messages."""
	if isinstance(users, str):
		users = [users]
	users = [u for u in dict.fromkeys(users) if u and u not in ("Guest", "Administrator")]
	if not users:
		return
	for user in users:
		frappe.get_doc(
			{
				"doctype": "Coop Notification",
				"user": user,
				"cooperative": cooperative,
				"kind": kind,
				"title": title,
				"body": body,
				"link": link,
			}
		).insert(ignore_permissions=True)
	if push and frappe.db.exists("Coop Push Subscription", {"user": ["in", users]}):
		frappe.enqueue(
			"coopvault.notify.send_push",
			queue="short",
			enqueue_after_commit=True,
			users=users,
			payload={"title": title, "body": body or "", "url": link or "/notifications", "tag": kind or "coopvault"},
		)


# Web Push --------------------------------------------------------------------------


def _b64url(raw):
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ensure_vapid_keys():
	"""Create the server's VAPID key pair once (install / migrate)."""
	settings = frappe.get_single("CoopVault Settings")
	if settings.vapid_public_key and settings.get_password("vapid_private_key", raise_exception=False):
		return
	from cryptography.hazmat.primitives import serialization
	from py_vapid import Vapid01

	vapid = Vapid01()
	vapid.generate_keys()
	private_pem = vapid.private_key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
	).decode()
	public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
	settings.vapid_public_key = _b64url(public)
	settings.vapid_private_key = private_pem
	settings.flags.ignore_permissions = True
	settings.save()


def public_key():
	return frappe.db.get_single_value("CoopVault Settings", "vapid_public_key")


def endpoint_hash(endpoint):
	return hashlib.sha256((endpoint or "").encode()).hexdigest()


def send_push(users, payload):
	from py_vapid import Vapid01
	from pywebpush import WebPushException, webpush

	settings = frappe.get_single("CoopVault Settings")
	pem = settings.get_password("vapid_private_key", raise_exception=False)
	if not pem:
		return
	vapid = Vapid01.from_pem(pem.encode())
	claims = {"sub": settings.vapid_subject or "mailto:admin@example.com"}
	subs = frappe.get_all(
		"Coop Push Subscription", filters={"user": ["in", users]}, fields=["name", "endpoint", "p256dh", "auth"]
	)
	for sub in subs:
		try:
			webpush(
				subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
				data=json.dumps(payload),
				vapid_private_key=vapid,
				vapid_claims=dict(claims),
				ttl=24 * 60 * 60,
				timeout=10,
			)
		except WebPushException as e:
			status = getattr(e.response, "status_code", None)
			if status in (404, 410):
				# The browser unsubscribed or the app was uninstalled
				frappe.db.delete("Coop Push Subscription", sub.name)
			else:
				frappe.log_error(title="Push notification failed", message=str(e)[:2000])
		except Exception:
			frappe.log_error(title="Push notification failed", message=frappe.get_traceback())
	frappe.db.commit()


def cleanup():
	"""Daily: drop read notifications older than 90 days."""
	cutoff = frappe.utils.add_days(frappe.utils.today(), -90)
	frappe.db.delete("Coop Notification", {"read": 1, "creation": ["<", cutoff]})
