"""Short-lived login sessions for the PWA.

The app sends `Authorization: Session <token>`. Only a SHA-256 hash of the token is stored, so a
database leak doesn't leak working logins. Sessions end after a period of inactivity, after a
maximum age, on logout, and when the password changes. Frappe API keys are never handed out.
"""

import hashlib
import secrets
from datetime import timedelta

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

SCHEME = "session"
TOUCH_EVERY = timedelta(hours=1)


def _hash(token):
	return hashlib.sha256(token.encode()).hexdigest()


def _limits():
	s = frappe.get_cached_doc("CoopVault Settings")
	return cint(s.session_idle_days) or 7, cint(s.session_max_days) or 30


def create(user):
	token = secrets.token_urlsafe(32)
	now = now_datetime()
	_idle, max_days = _limits()
	frappe.get_doc(
		{
			"doctype": "Coop Session",
			"user": user,
			"token_hash": _hash(token),
			"device": (frappe.get_request_header("User-Agent") or "")[:140],
			"ip_address": frappe.local.request_ip,
			"last_seen": now,
			"expires_on": add_to_date(now, days=max_days),
		}
	).insert(ignore_permissions=True)
	return token


def _token_from_request():
	parts = (frappe.get_request_header("Authorization") or "").split(" ")
	if len(parts) == 2 and parts[0].lower() == SCHEME and parts[1]:
		return parts[1]
	return None


def authenticate():
	"""auth_hooks entry: sign the request in from a valid session token."""
	token = _token_from_request()
	if not token:
		return
	row = frappe.db.get_value(
		"Coop Session",
		{"token_hash": _hash(token)},
		["name", "user", "last_seen", "expires_on", "revoked"],
		as_dict=True,
	)
	if not row or row.revoked:
		return
	now = now_datetime()
	idle_days, _max = _limits()
	if get_datetime(row.expires_on) <= now or get_datetime(row.last_seen) + timedelta(days=idle_days) <= now:
		return
	if not frappe.db.get_value("User", row.user, "enabled"):
		return

	# set_user() clears the request's form_dict; keep it, as Frappe's own API-key auth does
	form_dict = frappe.local.form_dict
	frappe.set_user(row.user)
	frappe.local.form_dict = form_dict
	frappe.local.coopvault_session = row.name
	if now - get_datetime(row.last_seen) > TOUCH_EVERY:
		frappe.db.set_value("Coop Session", row.name, "last_seen", now, update_modified=False)
		# GET requests are rolled back at the end, so keep the activity timestamp explicitly
		frappe.db.commit()


def current():
	return getattr(frappe.local, "coopvault_session", None)


def revoke(name):
	"""End one login. The device it was on also stops getting push notifications, so a lost or
	shared phone doesn't keep showing payments on its lock screen."""
	if name:
		frappe.db.set_value("Coop Session", name, "revoked", 1, update_modified=False)
		frappe.db.delete("Coop Push Subscription", {"session": name})


def revoke_all(user, keep=None):
	filters = {"user": user, "revoked": 0}
	if keep:
		filters["name"] = ["!=", keep]
	for name in frappe.get_all("Coop Session", filters=filters, pluck="name"):
		revoke(name)
	# Devices subscribed before subscriptions were tied to a login
	frappe.db.delete("Coop Push Subscription", {"user": user, "session": ["is", "not set"]})


def ended(names):
	"""Which of these logins are over: signed out, past their maximum age, or idle too long."""
	names = [n for n in names if n]
	if not names:
		return set()
	idle_days, _max = _limits()
	now = now_datetime()
	rows = frappe.get_all("Coop Session", filters={"name": ["in", names]}, fields=["name", "revoked", "expires_on", "last_seen"])
	over = {
		r.name for r in rows
		if r.revoked or get_datetime(r.expires_on) <= now or get_datetime(r.last_seen) + timedelta(days=idle_days) <= now
	}
	return over | (set(names) - {r.name for r in rows})


def cleanup():
	"""Daily: forget sessions that ended a while ago."""
	subs = frappe.get_all("Coop Push Subscription", filters={"session": ["is", "set"]}, fields=["name", "session"])
	gone = ended([s.session for s in subs])
	for s in subs:
		if s.session in gone:
			frappe.db.delete("Coop Push Subscription", s.name)
	cutoff = add_to_date(now_datetime(), days=-30)
	frappe.db.delete("Coop Session", {"expires_on": ["<", cutoff]})
	frappe.db.delete("Coop Session", {"revoked": 1, "modified": ["<", cutoff]})
