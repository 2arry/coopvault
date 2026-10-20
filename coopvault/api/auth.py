import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import validate_email_address
from frappe.utils.password import check_password, update_password

from coopvault.api import require_login


def _issue_token(user_name):
	"""Return (api_key, api_secret) for token auth, creating them once per user."""
	user = frappe.get_doc("User", user_name)
	secret = user.get_password("api_secret", raise_exception=False) if user.api_secret else None
	if user.api_key and secret:
		return user.api_key, secret
	user.api_key = user.api_key or frappe.generate_hash(length=15)
	secret = frappe.generate_hash(length=15)
	user.api_secret = secret
	user.flags.ignore_permissions = True
	user.save(ignore_permissions=True)
	return user.api_key, secret


def _session_payload(user_name):
	api_key, api_secret = _issue_token(user_name)
	return {"token": f"{api_key}:{api_secret}", "user": get_profile(user_name)}


def get_profile(user_name=None):
	user_name = user_name or frappe.session.user
	u = frappe.db.get_value(
		"User", user_name, ["name", "email", "full_name", "first_name", "mobile_no", "user_image"], as_dict=True
	)
	from coopvault.api import is_super_user

	u.is_super_user = is_super_user(user_name)
	return u


def _resolve_user(login):
	login = (login or "").strip()
	user = frappe.db.get_value("User", {"email": login}) or frappe.db.get_value("User", {"mobile_no": login})
	if not user and frappe.db.exists("User", login):
		user = login
	return user


def _check_phone_free(phone, user=None):
	if phone and frappe.db.exists("User", {"mobile_no": phone, "name": ["!=", user or ""]}):
		frappe.throw(_("This phone number is already used by another account"))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 5)
def login(email, password):
	user = _resolve_user(email)
	if not user or user in ("Guest",):
		frappe.throw(_("Invalid email or password"), frappe.AuthenticationError)
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("This account is disabled"), frappe.AuthenticationError)
	try:
		check_password(user, password)
	except frappe.AuthenticationError:
		frappe.throw(_("Invalid email or password"), frappe.AuthenticationError)
	return _session_payload(user)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 10)
def signup(full_name, email, password, phone=None):
	if not frappe.db.get_single_value("CoopVault Settings", "allow_signup"):
		frappe.throw(_("Sign up is disabled. Ask your cooperative admin for an account."))
	email = (email or "").strip().lower()
	validate_email_address(email, throw=True)
	if frappe.db.exists("User", {"email": email}):
		frappe.throw(_("An account with this email already exists. Please log in."))
	if len(password or "") < 8:
		frappe.throw(_("Password must be at least 8 characters"))
	phone = (phone or "").strip() or None
	_check_phone_free(phone)

	names = (full_name or "").strip().split(" ", 1)
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
	user.flags.ignore_password_policy = True
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
		phone = phone.strip() or None
		_check_phone_free(phone, user.name)
		user.mobile_no = phone
	user.save(ignore_permissions=True)
	for m in frappe.get_all("Coop Member", filters={"user": user.name}, pluck="name"):
		frappe.db.set_value("Coop Member", m, {"full_name": user.full_name, "phone": user.mobile_no})
	return get_profile()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=5, seconds=60 * 10)
def change_password(old_password, new_password):
	user = require_login()
	try:
		check_password(user, old_password)
	except frappe.AuthenticationError:
		frappe.throw(_("Current password is incorrect"))
	if len(new_password or "") < 8:
		frappe.throw(_("Password must be at least 8 characters"))
	update_password(user, new_password)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def logout_all_devices():
	"""Rotate the API secret so every device has to log in again."""
	user = frappe.get_doc("User", require_login())
	user.api_secret = frappe.generate_hash(length=15)
	user.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=3, seconds=60 * 10)
def forgot_password(email):
	from frappe.core.doctype.user.user import reset_password

	try:
		reset_password(email)
	except Exception:
		frappe.clear_messages()
	# Always answer the same way so the endpoint cannot be used to discover accounts
	return {"ok": True}
