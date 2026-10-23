"""Shared helpers for the API test suites. They run against the staging site only:

	bench --site coopvault.test serve --port 8100      (never without --site: that serves production)
	python dev/tests/fake_paystack.py 8899
	python dev/tests/<suite>.py
"""
import hashlib
import hmac
import json
import secrets
import subprocess
import sys

import requests

BASE = "http://127.0.0.1:8100/api/method/coopvault.api."
SITE = "coopvault.test"
FAKE = "http://127.0.0.1:8899"
SECRET = "sk_test_fake_secret"
BENCH = "/home/ubuntu/start-paymnet/backend"
RUN = secrets.token_hex(3)
PW = "Coop#Test2026"
N = int(RUN, 16) % 10**6
results = []


def paystack_fee(amount):
	"""What the fake Paystack keeps from a charge of `amount` naira (same as Paystack's pricing)."""
	return round(min(amount * 0.015 + (100 if amount >= 2500 else 0), 2000), 2)


def check(name, cond, detail=""):
	results.append(bool(cond))
	print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))


def raw(path, token=None, method="POST", **data):
	h = {"Accept": "application/json", "Host": SITE}
	if token:
		h["Authorization"] = "Session " + token
	if method == "POST":
		return requests.post(BASE + path, json=data, headers=h, timeout=60)
	return requests.get(BASE + path, params=data, headers=h, timeout=60)


def call(path, token=None, method="POST", **data):
	r = raw(path, token, method, **data)
	if r.status_code != 200:
		print("FAILED", path, r.status_code, r.text[:1500])
		sys.exit(1)
	return r.json().get("message")


def get(path, token=None, **data):
	return call(path, token, "GET", **data)


def err(path, token=None, method="POST", **data):
	"""Call expecting an error; returns (status, message)."""
	r = raw(path, token, method, **data)
	body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
	msgs = body.get("_server_messages")
	msg = json.loads(json.loads(msgs)[0])["message"] if msgs else body.get("exception") or ""
	return r.status_code, msg


def rejected(label, path, token=None, method="POST", contains=None, **data):
	status, msg = err(path, token, method, **data)
	ok = status != 200 and (contains is None or contains.lower() in (msg or "").lower())
	check(label, ok, f"{status}: {msg}")
	return msg


def webhook(event, data):
	payload = json.dumps({"event": event, "data": data}).encode()
	sig = hmac.new(SECRET.encode(), payload, hashlib.sha512).hexdigest()
	return requests.post(BASE + "payments.paystack_webhook", data=payload, timeout=60,
		headers={"x-paystack-signature": sig, "Content-Type": "application/json", "Host": SITE})


def fake(path, **body):
	return requests.post(FAKE + path, json=body, timeout=30).json()


def fake_get(path, **params):
	return requests.get(FAKE + path, params=params, timeout=30).json()


def console(code):
	"""Run Python against the staging site (for setup that has no API, e.g. backdating)."""
	out = subprocess.run(["bench", "--site", SITE, "console"], input=code + "\nfrappe.db.commit()\n", cwd=BENCH,
		capture_output=True, text=True)
	if out.returncode or "Error" in out.stdout:
		print(out.stdout[-2000:], out.stderr[-2000:])
		sys.exit(1)
	return out.stdout


def signup(key, i, name, phone=True):
	return call("auth.signup", full_name=name, email=f"{key}.{RUN}@example.com", password=PW,
		phone=f"0803{N:06d}{i}" if phone else None)


def pay(token, cooperative, amount, purpose="Contribution", loan=None, **extra):
	"""A checkout payment completed through the fake Paystack."""
	init = call("payments.initialize", token, cooperative=cooperative, purpose=purpose, amount=amount, loan=loan, **extra)
	fake("/_test/pay", reference=init["reference"])
	return call("payments.verify", token, reference=init["reference"])


_operator = {}


def operator():
	"""A platform operator for this run (approves the test groups)."""
	if "token" not in _operator:
		u = call("auth.signup", full_name="Ola Operator", email=f"ops.{RUN}@example.com", password=PW, phone=None)
		console(f"""
user = frappe.get_doc('User', '{u["user"]["name"]}')
user.add_roles('Platform Operator')
""")
		_operator["token"] = call("auth.login", email=f"ops.{RUN}@example.com", password=PW)["token"]
	return _operator["token"]


def new_cooperative(admin_token, name, members=(), **rules):
	"""A cooperative with active members: members = [(token, role), ...]. Returns (coop, {token: member})."""
	settings = {"contribution_amount": 10000, "contribution_frequency": "Monthly", "interest_rate": 5,
		"max_loan_tenure_months": 6, "fee_bearer": "Member", **rules}
	coop = call("coop.create_cooperative", admin_token, cooperative_name=f"{name} {RUN}", **settings)
	if coop.get("status") == "Pending review":
		call("platform.review_group", operator(), name=coop["name"], approve=1)
	ids = {}
	for token, _role in members:
		call("coop.join_cooperative", token, join_code=coop["join_code"])
	listing = get("coop.list_members", admin_token, cooperative=coop["name"])
	for token, role in members:
		me = get("auth.me", token)["user"]["name"]
		m = next(x for x in listing if x["user"] == me)
		call("coop.update_member", admin_token, member=m["name"], status="Active")
		if role != "Member":
			call("coop.update_member", admin_token, member=m["name"], role=role)
		ids[token] = m["name"]
	return coop, ids


def guard():
	"""Refuse to run unless this is the staging site with the fake Paystack; reset rate limits."""
	if get("payments.config")["public_key"] != "pk_test_fake_public":
		print("ABORT: not the staging site")
		sys.exit(2)
	db = json.load(open(f"{BENCH}/sites/{SITE}/site_config.json"))["db_name"]
	subprocess.run(f"redis-cli -p 13000 --scan --pattern '{db}|rl:*' | xargs -r -d '\n' redis-cli -p 13000 del",
		shell=True, capture_output=True)


def done(label=""):
	print(f"{sum(results)}/{len(results)} passed (run {RUN}{', ' + label if label else ''})")
	sys.exit(0 if all(results) else 1)
