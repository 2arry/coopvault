import hashlib
import hmac

import frappe
from frappe import _
from frappe.auth import LoginAttemptTracker
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, cint, get_datetime, now_datetime, validate_email_address
from frappe.utils.password import check_password, update_password

from coopvault import sessions
from coopvault.api import require_login
from coopvault.notify import notify, staff_of
from coopvault.utils import check_hold, check_password_strength, mask_account, normalize_phone, require_password

INVALID_LOGIN = _("That email/phone and password don't match")
RESET_MAX_ATTEMPTS = 5


def _session_payload(user_name):
	return {"token": sessions.create(user_name), "user": get_profile(user_name)}


def get_profile(user_name=None):
	user_name = user_name or frappe.session.user
	u = frappe.db.get_value(
		"User", user_name, ["name", "email", "full_name", "first_name", "mobile_no", "user_image"], as_dict=True
	)
	from coopvault.api import is_super_user

	u.is_super_user = is_super_user(user_name)
	u.payout_account = frappe.db.get_value(
		"Coop Member",
		{"user": user_name, "payout_account_number": ["is", "set"]},
		["payout_bank_name", "payout_bank_code", "payout_account_number", "payout_account_name", "payout_changed_on"],
		as_dict=True,
		order_by="modified desc",
	)
	return u


def _resolve_user(login):
	"""Find a member's account by email or phone number (any common Nigerian format)."""
	login = (login or "").strip()
	if not login:
		return None
	if "@" in login:
		return frappe.db.get_value("User", {"email": login.lower()})
	phone = normalize_phone(login)
	return phone and frappe.db.get_value("User", {"mobile_no": phone})


def _app_user(user):
	"""Only member accounts sign in to the app; staff and system accounts use the desk."""
	if not user or user in ("Administrator", "Guest"):
		return False
	row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(row and row.enabled and row.user_type == "Website User")


def _tracker(key):
	settings = frappe.get_system_settings
	return LoginAttemptTracker(
		key=f"coopvault:{key}",
		max_consecutive_login_attempts=cint(settings("allow_consecutive_login_attempts")) or 10,
		lock_interval=max(cint(settings("allow_login_after_fail")), 60),
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 5)
def login(email, password):
	user = _resolve_user(email)
	tracker = _tracker(user or (email or "").strip().lower())
	if not tracker.is_user_allowed():
		frappe.throw(_("Too many attempts. Wait a minute and try again."), frappe.RateLimitExceededError)
	ok = _app_user(user)
	if ok:
		try:
			check_password(user, password)
		except frappe.AuthenticationError:
			ok = False
	if not ok:
		tracker.add_failure_attempt()
		frappe.throw(INVALID_LOGIN, frappe.AuthenticationError)
	tracker.add_success_attempt()
	return _session_payload(user)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 10)
def signup(full_name, email, password, phone=None):
	if not frappe.db.get_single_value("CoopVault Settings", "allow_signup"):
		frappe.throw(_("Sign up is closed. Ask your cooperative admin for an account."))
	full_name = (full_name or "").strip()
	if len(full_name) < 2:
		frappe.throw(_("Enter your full name"))
	email = (email or "").strip().lower()
	validate_email_address(email, throw=True)
	phone = normalize_phone(phone)
	if frappe.db.exists("User", {"email": email}) or (phone and frappe.db.exists("User", {"mobile_no": phone})):
		# Same answer for email and phone so the form can't be used to check who is a member
		frappe.throw(_("We couldn't create an account with those details. If you already have one, log in instead."))
	check_password_strength(password, full_name, email)

	names = full_name.split(" ", 1)
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": names[0],
			"last_name": names[1] if len(names) > 1 else "",
			"mobile_no": phone,
			"user_type": "Website User",
			"send_welcome_email": 0,
			"enabled": 1,
		}
	)
	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True  # checked above with the member's own details
	user.insert(ignore_permissions=True)
	update_password(user.name, password)
	return _session_payload(user.name)


@frappe.whitelist()
def me():
	require_login()
	from coopvault.api.coop import my_cooperatives

	return {"user": get_profile(), "cooperatives": my_cooperatives()}


@frappe.whitelist(methods=["POST"])
def update_profile(full_name=None, phone=None):
	user = frappe.get_doc("User", require_login())
	if full_name:
		names = full_name.strip().split(" ", 1)
		user.first_name = names[0]
		user.last_name = names[1] if len(names) > 1 else ""
	if phone is not None:
		phone = normalize_phone(phone)
		if phone and frappe.db.exists("User", {"mobile_no": phone, "name": ["!=", user.name]}):
			frappe.throw(_("That phone number can't be used"))
		user.mobile_no = phone
	user.save(ignore_permissions=True)
	for m in frappe.get_all("Coop Member", filters={"user": user.name}, pluck="name"):
		frappe.db.set_value("Coop Member", m, {"full_name": user.full_name, "phone": user.mobile_no})
	return get_profile()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 10)
def set_payout_account(account_number, password=None, bank_code=None, bank_name=None, account_name=None):
	"""Where loans and withdrawals are sent. Needs the password and tells the member and their admins."""
	from coopvault import paystack

	user = require_login()
	require_password(password)
	memberships = frappe.get_all("Coop Member", filters={"user": user}, fields=["name", "cooperative", "status"])
	for m in memberships:
		check_hold(m.name)
	account_number = (account_number or "").strip()
	if paystack.is_enabled():
		if not bank_code:
			frappe.throw(_("Choose your bank"))
		account_name = paystack.resolve_account(account_number, bank_code)
		bank_name = paystack.bank_name(bank_code) or bank_name
	elif not (bank_name and account_number and account_name):
		frappe.throw(_("Enter your bank, account number and account name"))

	values = {
		"payout_bank_code": bank_code,
		"payout_bank_name": bank_name,
		"payout_account_number": account_number,
		"payout_account_name": account_name,
		"payout_changed_on": now_datetime(),
	}
	full_name = frappe.db.get_value("User", user, "full_name")
	for m in memberships:
		frappe.db.set_value("Coop Member", m.name, values)
		if m.status == "Active":
			notify(
				staff_of(m.cooperative, exclude_users=[user]),
				_("{0} changed their payout account").format(full_name),
				_("Now {0} {1} ({2}). Check with them before sending money if this is unexpected.").format(
					bank_name, mask_account(account_number), account_name
				),
				link="/admin?tab=members",
				cooperative=m.cooperative,
				kind="payout-changed",
			)
	notify(
		user,
		_("Your payout account was changed"),
		_("Loans and withdrawals will go to {0} {1}. If this wasn't you, change your password now.").format(
			bank_name, mask_account(account_number)
		),
		link="/profile",
		kind="payout-changed",
	)
	return get_profile()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=5, seconds=60 * 10)
def change_password(old_password, new_password):
	user = require_login()
	try:
		check_password(user, old_password)
	except frappe.AuthenticationError:
		frappe.throw(_("Current password is incorrect"))
	u = frappe.db.get_value("User", user, ["full_name", "email"], as_dict=True)
	check_password_strength(new_password, u.full_name, u.email)
	update_password(user, new_password)
	sessions.revoke_all(user, keep=sessions.current())
	notify(user, _("Your password was changed"), _("Other devices have been logged out."), kind="security", push=False)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def logout():
	require_login()
	sessions.revoke(sessions.current())
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def logout_all_devices():
	user = require_login()
	sessions.revoke_all(user)
	return {"ok": True}


# Password reset without email: an admin of the member's cooperative creates a one-time code
# (coop.create_reset_code) and passes it on in person or on WhatsApp.


def _reset_hash(code):
	return hashlib.sha256((code or "").replace("-", "").strip().upper().encode()).hexdigest()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60 * 10)
def reset_password_with_code(login, code, new_password):
	user = _resolve_user(login)
	invalid = _("That code isn't valid. Ask your admin for a new one.")
	if not _app_user(user):
		frappe.throw(invalid)
	reset = frappe.db.get_value(
		"Coop Password Reset",
		{"user": user, "used_on": ["is", "not set"]},
		["name", "code_hash", "expires_on", "attempts", "cooperative", "created_by"],
		as_dict=True,
		order_by="creation desc",
	)
	if not reset or get_datetime(reset.expires_on) < now_datetime() or cint(reset.attempts) >= RESET_MAX_ATTEMPTS:
		frappe.throw(invalid)
	if not hmac.compare_digest(reset.code_hash, _reset_hash(code)):
		frappe.db.set_value("Coop Password Reset", reset.name, "attempts", cint(reset.attempts) + 1)
		frappe.db.commit()
		frappe.throw(invalid)

	u = frappe.db.get_value("User", user, ["full_name", "email"], as_dict=True)
	check_password_strength(new_password, u.full_name, u.email)
	update_password(user, new_password)
	frappe.db.set_value("Coop Password Reset", reset.name, "used_on", now_datetime())
	sessions.revoke_all(user)

	hours = cint(frappe.db.get_single_value("CoopVault Settings", "security_hold_hours")) or 72
	hold_until = add_to_date(now_datetime(), hours=hours)
	for m in frappe.get_all("Coop Member", filters={"user": user}, pluck="name"):
		frappe.db.set_value("Coop Member", m, "security_hold_until", hold_until)
	notify(
		user,
		_("Your password was reset"),
		_("For your security, payout changes, loans and withdrawals are paused for {0} hours.").format(hours),
		kind="security",
		push=False,
	)
	if reset.cooperative:
		notify(
			staff_of(reset.cooperative, exclude_users=[user]),
			_("{0} reset their password with a code").format(u.full_name),
			kind="security",
			cooperative=reset.cooperative,
		)
	return _session_payload(user)
