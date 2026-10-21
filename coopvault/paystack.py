"""Minimal Paystack API client used by Cooperative360.

Self-contained on purpose: the app must install on any bench without other payment apps.
Endpoints follow Paystack's OpenAPI spec (github.com/PaystackOSS/openapi).
"""

import hashlib
import hmac
import math

import frappe
import requests
from frappe import _
from frappe.utils import flt

LIVE_API = "https://api.paystack.co"
BANKS_CACHE_SECONDS = 24 * 60 * 60


class PaystackError(frappe.ValidationError):
	pass


def settings():
	return frappe.get_cached_doc("CoopVault Settings")


def secret_key():
	s = settings()
	if not s.paystack_enabled:
		return None
	return s.get_password("paystack_secret_key", raise_exception=False)


def is_enabled():
	return bool(secret_key())


def is_test_mode():
	return (secret_key() or "").startswith("sk_test_")


def api_base():
	# Overridable from site_config so tests can point at a local fake
	return (frappe.conf.get("coopvault_paystack_api") or LIVE_API).rstrip("/")


def call(method, path, params=None, body=None, throw=True):
	secret = secret_key()
	if not secret:
		frappe.throw(_("Online payments are not set up yet"))
	try:
		res = requests.request(
			method,
			api_base() + path,
			headers={"Authorization": f"Bearer {secret}"},
			params=params,
			json=body,
			timeout=30,
		)
		data = res.json()
	except Exception:
		frappe.log_error(title="Paystack request failed", message=frappe.get_traceback())
		frappe.throw(_("Could not reach Paystack. Please try again."))
	if throw and not data.get("status"):
		frappe.throw(data.get("message") or _("Paystack could not complete the request"), PaystackError)
	return data


def valid_signature(payload: bytes, signature: str) -> bool:
	secret = secret_key()
	if not secret or not signature:
		return False
	expected = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
	return hmac.compare_digest(expected, signature)


def to_subunit(amount):
	return int(round(flt(amount) * 100))


# Pricing --------------------------------------------------------------------------


def checkout_fee(amount):
	"""Fee to add at checkout so the cooperative still receives `amount` after Paystack's cut.

	Uses Paystack's documented pass-on formula: charge = (amount + flat) / (1 - percent),
	where the flat fee only applies once the charge reaches the waiver threshold, and the
	fee never exceeds the cap.
	"""
	s = settings()
	amount = flt(amount)
	pct = flt(s.fee_percent) / 100
	if amount <= 0 or not 0 <= pct < 1:
		return 0.0
	flat, threshold, cap = flt(s.fee_flat), flt(s.fee_flat_waived_below), flt(s.fee_cap)

	charge = amount / (1 - pct)
	if flat and charge >= threshold:
		charge = (amount + flat) / (1 - pct)
	fee = math.ceil(round((charge - amount) * 100, 6)) / 100
	return min(fee, cap) if cap else fee


# Banks ----------------------------------------------------------------------------


TEST_BANK = {"name": "Paystack Test Bank", "code": "001", "slug": "test-bank"}


def list_banks(currency="NGN"):
	"""Banks for account lookups. Test keys also get Paystack's test bank (code 001), which
	resolves any 10-digit number; real banks are limited to 3 lookups a day in test mode."""
	banks = _live_banks(currency)
	if is_test_mode() and currency == "NGN":
		return [TEST_BANK, *banks]
	return banks


def _live_banks(currency):
	key = f"coopvault:paystack_banks:{currency}"
	banks = frappe.cache.get_value(key)
	if banks:
		return banks
	banks, cursor = [], None
	for _page in range(10):
		params = {"currency": currency, "perPage": 100, "use_cursor": "true"}
		if cursor:
			params["next"] = cursor
		data = call("GET", "/bank", params=params)
		banks += [
			{"name": b["name"], "code": b["code"], "slug": b.get("slug")}
			for b in data.get("data") or []
			if b.get("active", True) and not b.get("is_deleted")
		]
		cursor = (data.get("meta") or {}).get("next")
		if not cursor:
			break
	seen, unique = set(), []
	for b in sorted(banks, key=lambda b: b["name"].lower()):
		if b["code"] not in seen:
			seen.add(b["code"])
			unique.append(b)
	frappe.cache.set_value(key, unique, expires_in_sec=BANKS_CACHE_SECONDS)
	return unique


def bank_name(code, currency="NGN"):
	return next((b["name"] for b in list_banks(currency) if b["code"] == code), None)


def resolve_account(account_number, bank_code):
	account_number = (account_number or "").strip()
	if not account_number.isdigit() or len(account_number) != 10:
		frappe.throw(_("Enter a 10-digit account number"))
	data = call(
		"GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code}, throw=False
	)
	if not data.get("status"):
		if is_test_mode() and "limit" in (data.get("message") or "").lower():
			frappe.throw(_("Paystack test mode only checks 3 real accounts a day. Choose Paystack Test Bank instead."))
		frappe.throw(_("We couldn't verify that account. Check the number and bank."))
	return data["data"]["account_name"]


# Subaccounts (each cooperative's money settles straight into its own bank account) --


def upsert_subaccount(coop):
	creator = frappe.db.get_value("User", coop.created_by_user, ["email", "full_name", "mobile_no"], as_dict=True)
	bank, number = coop.settlement_bank_code, coop.account_number
	if is_test_mode() and bank == TEST_BANK["code"]:
		# Paystack won't attach a subaccount to its test bank, but accepts its sandbox account
		bank, number = "057", "0000000000"
	body = {
		"business_name": coop.cooperative_name[:100],
		"settlement_bank": bank,
		"account_number": number,
		"percentage_charge": flt(settings().platform_fee_percent),
		"description": f"Cooperative360 cooperative {coop.name}",
	}
	if creator:
		body.update(
			primary_contact_email=creator.email,
			primary_contact_name=creator.full_name,
			primary_contact_phone=creator.mobile_no or "",
		)
	if coop.paystack_subaccount_code:
		data = call("PUT", f"/subaccount/{coop.paystack_subaccount_code}", body=body)
	else:
		data = call("POST", "/subaccount", body=body)
	return data["data"]["subaccount_code"]


# Customers and dedicated virtual accounts -------------------------------------------


def split_name(full_name):
	parts = (full_name or "").strip().split()
	if not parts:
		return "", ""
	return parts[0], " ".join(parts[1:]) or parts[0]


def dedicated_account_provider():
	"""Live keys use the configured provider; test keys must use Paystack's test-bank."""
	provider = settings().dedicated_account_bank or "wema-bank"
	return "test-bank" if is_test_mode() else provider


def assign_dedicated_account(user, subaccount=None, bank_code=None, account_number=None, bvn=None):
	"""Ask Paystack to create a customer and a dedicated account for them (result arrives by webhook)."""
	u = frappe.db.get_value("User", user, ["email", "full_name", "mobile_no"], as_dict=True)
	first, last = split_name(u.full_name)
	body = {
		"email": u.email,
		"first_name": first,
		"last_name": last,
		"phone": u.mobile_no,
		"preferred_bank": dedicated_account_provider(),
		"country": "NG",
	}
	if subaccount:
		body["subaccount"] = subaccount
	if bvn and bank_code and account_number:
		body.update(bvn=bvn, bank_code=bank_code, account_number=account_number)
	return call("POST", "/dedicated_account/assign", body=body)


def deactivate_dedicated_account(account_id=None, customer_code=None):
	"""Switch off a member's account number (when they leave), so transfers bounce at the bank
	instead of landing in a group they're no longer part of. Returns True when Paystack agreed."""
	if not account_id and customer_code:
		customer = call("GET", f"/customer/{customer_code}", throw=False).get("data") or {}
		accounts = customer.get("dedicated_accounts") or [customer.get("dedicated_account") or {}]
		account_id = next((a.get("id") for a in accounts if a and a.get("active", True)), None)
	if not account_id:
		return False
	return bool(call("DELETE", f"/dedicated_account/{account_id}", throw=False).get("status"))


# Transactions -----------------------------------------------------------------------


def initialize_transaction(**body):
	return call("POST", "/transaction/initialize", body=body)["data"]


def verify_transaction(reference):
	data = call("GET", f"/transaction/verify/{reference}", throw=False)
	return data.get("data") if data.get("status") else None


def charge_authorization(**body):
	return call("POST", "/transaction/charge_authorization", body=body, throw=False)


# Direct debit mandates --------------------------------------------------------------


def initialize_mandate(email, callback_url):
	body = {"email": email, "channel": "direct_debit", "callback_url": callback_url}
	return call("POST", "/customer/authorization/initialize", body=body)["data"]


def verify_mandate(reference):
	data = call("GET", f"/customer/authorization/verify/{reference}", throw=False)
	return data.get("data") if data.get("status") else None


def deactivate_authorization(authorization_code):
	return call(
		"POST", "/customer/authorization/deactivate", body={"authorization_code": authorization_code}, throw=False
	)
