"""Small shared helpers: phone numbers, password checks, security holds and per-user throttling."""

import re

import frappe
from frappe import _
from frappe.utils import cint, format_datetime, get_datetime, now_datetime


def normalize_phone(value):
	"""Canonical +234… form, so "0801 234 5678", "08012345678" and "+2348012345678" match.

	Numbers that are not Nigerian are kept as + followed by their digits.
	"""
	raw = (value or "").strip()
	if not raw:
		return None
	digits = re.sub(r"\D", "", raw)
	if not digits:
		return None
	if len(digits) == 11 and digits.startswith("0"):
		return "+234" + digits[1:]
	if len(digits) == 13 and digits.startswith("234"):
		return "+" + digits
	if len(digits) == 10 and digits[0] in "789":
		return "+234" + digits
	return "+" + digits if raw.startswith("+") or len(digits) > 11 else digits


def require_password(password, user=None):
	"""Re-check the signed-in user's password before a sensitive change."""
	from frappe.utils.password import check_password

	user = user or frappe.session.user
	if not password:
		frappe.throw(_("Enter your password to confirm this change"))
	try:
		check_password(user, password)
	except frappe.AuthenticationError:
		frappe.throw(_("That password isn't right"), frappe.AuthenticationError)


def check_password_strength(password, full_name="", email=""):
	"""Apply the site's password policy (System Settings) to a new password."""
	from frappe.core.doctype.user.user import test_password_strength

	if len(password or "") < 8:
		frappe.throw(_("Use at least 8 characters"))
	names = (full_name or "").split()
	result = test_password_strength(
		password, user_data=(names[0] if names else "", "", " ".join(names[1:]), email or "", None)
	)
	feedback = (result or {}).get("feedback") or {}
	if result and not feedback.get("password_policy_validation_passed"):
		hint = feedback.get("warning") or " ".join(feedback.get("suggestions") or [])
		frappe.throw(_("Choose a stronger password. {0}").format(hint or _("Mix words, numbers and symbols.")))


def check_hold(member):
	"""Block money moves for a while after a password reset done by an admin."""
	until = frappe.db.get_value("Coop Member", member, "security_hold_until")
	if until and get_datetime(until) > now_datetime():
		frappe.throw(
			_("For your security this is paused after a password reset. You can do it again after {0}.").format(
				format_datetime(until, "d MMM, h:mm a")
			)
		)


def throttle(action, limit, seconds):
	"""Per-user rate limit (Frappe's own limiter is per IP or per form field)."""
	cache_key = frappe.cache.make_key(f"coopvault:throttle:{action}:{frappe.session.user}")
	if not frappe.cache.get(cache_key):
		frappe.cache.setex(cache_key, seconds, 0)
	if cint(frappe.cache.incrby(cache_key, 1)) > limit:
		frappe.throw(_("Too many attempts. Please wait a few minutes and try again."), frappe.RateLimitExceededError)


def mask_account(number):
	number = str(number or "")
	return f"••{number[-4:]}" if len(number) >= 4 else number
