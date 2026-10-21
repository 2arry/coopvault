"""A small stand-in for api.paystack.co used by the Cooperative360 tests (shapes follow Paystack's OpenAPI spec)."""
import json
import secrets
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TX = {}
MANDATES = {}
LOG = []
CLOSED = []
BANKS = [
	{"name": "Access Bank", "code": "044", "slug": "access-bank", "active": True},
	{"name": "Guaranty Trust Bank", "code": "058", "slug": "guaranty-trust-bank", "active": True},
	{"name": "Kuda Bank", "code": "50211", "slug": "kuda-bank", "active": True},
	{"name": "Opay", "code": "999992", "slug": "paycom", "active": True},
	{"name": "Wema Bank", "code": "035", "slug": "wema-bank", "active": True},
]
ACCOUNTS = {"0123456789": "UNITY SAVINGS CIRCLE", "0987654321": "TUNDE BAKARE", "0555555555": "CHIOMA OKAFOR"}


def now():
	return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tx_data(t):
	return {
		"id": t["id"], "status": t["status"], "reference": t["reference"], "amount": t["amount"],
		"currency": t.get("currency", "NGN"), "channel": t.get("channel", "card"), "paid_at": t.get("paid_at"),
		"gateway_response": t.get("gateway_response", "Successful"), "fees": int(t["amount"] * 0.015),
		"metadata": t.get("metadata"),
		"customer": {"email": t["email"], "customer_code": "CUS_" + t["email"].split("@")[0][:10]},
		"authorization": t.get("authorization") or {
			"authorization_code": "AUTH_" + t["reference"][-8:], "reusable": True, "channel": "card",
			"brand": "visa", "card_type": "visa DEBIT", "last4": "4081", "exp_month": "12", "exp_year": "2030",
			"bank": "TEST BANK", "signature": "SIG_" + t["email"][:6],
		},
	}


class H(BaseHTTPRequestHandler):
	def log_message(self, *a):
		pass

	def send(self, code, body):
		raw = json.dumps(body).encode()
		self.send_response(code)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(raw)))
		self.end_headers()
		self.wfile.write(raw)

	def body(self):
		n = int(self.headers.get("Content-Length") or 0)
		return json.loads(self.rfile.read(n) or b"{}")

	def handle_any(self, method):
		u = urlparse(self.path)
		path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
		body = self.body() if method in ("POST", "PUT") else {}
		if not path.startswith("/_test"):
			LOG.append({"method": method, "path": path, "query": q, "body": body,
				"auth": self.headers.get("Authorization")})
			if self.headers.get("Authorization") != "Bearer sk_test_fake_secret":
				return self.send(401, {"status": False, "message": "Invalid key"})

		if path == "/transaction/initialize":
			ref = body.get("reference") or secrets.token_hex(6)
			TX[ref] = {"id": len(TX) + 1, "reference": ref, "amount": int(body["amount"]), "email": body["email"],
				"currency": body.get("currency", "NGN"), "metadata": body.get("metadata"), "status": "abandoned",
				"subaccount": body.get("subaccount"), "bearer": body.get("bearer"), "channels": body.get("channels")}
			code = "ac_" + ref[-10:].lower()
			return self.send(200, {"status": True, "message": "Authorization URL created", "data": {
				"authorization_url": f"https://checkout.paystack.com/{code}", "access_code": code, "reference": ref}})
		if path.startswith("/transaction/verify/"):
			t = TX.get(path.rsplit("/", 1)[1])
			if not t:
				return self.send(400, {"status": False, "message": "Transaction reference not found"})
			return self.send(200, {"status": True, "message": "Verification successful", "data": tx_data(t)})
		if path == "/transaction/charge_authorization":
			ref = body.get("reference") or secrets.token_hex(6)
			declined = body.get("authorization_code") == "AUTH_DECLINE"
			pending = body.get("authorization_code", "").startswith("AUTH_DD")
			TX[ref] = {"id": len(TX) + 1, "reference": ref, "amount": int(body["amount"]), "email": body["email"],
				"status": "failed" if declined else ("ongoing" if pending else "success"),
				"paid_at": None if (declined or pending) else now(), "subaccount": body.get("subaccount"),
				"gateway_response": "Declined" if declined else "Approved",
				"channel": "direct_debit" if pending else "card"}
			return self.send(200, {"status": True, "message": "Charge attempted", "data": tx_data(TX[ref])})
		if path == "/bank":
			return self.send(200, {"status": True, "data": BANKS, "meta": {"next": None}})
		if path == "/bank/resolve":
			# Like Paystack's test bank (001), any 10-digit number resolves
			name = ACCOUNTS.get(q.get("account_number")) or (q.get("bank_code") == "001" and f"TEST ACCOUNT {q.get('account_number', '')[-4:]}")
			if not name:
				return self.send(422, {"status": False, "message": "Could not resolve account name"})
			return self.send(200, {"status": True, "data": {"account_number": q["account_number"], "account_name": name}})
		if path == "/subaccount" and method == "POST":
			return self.send(200, {"status": True, "data": {"subaccount_code": "ACCT_" + secrets.token_hex(5), **body}})
		if path.startswith("/subaccount/") and method == "PUT":
			return self.send(200, {"status": True, "data": {"subaccount_code": path.rsplit("/", 1)[1], **body}})
		if path == "/dedicated_account/assign":
			if not body.get("phone"):
				return self.send(400, {"status": False, "message": "Customer phone number is required"})
			return self.send(200, {"status": True, "message": "Assign dedicated account in progress"})
		if path.startswith("/dedicated_account/") and method == "DELETE":
			CLOSED.append(path.rsplit("/", 1)[1])
			return self.send(200, {"status": True, "message": "Managed Account Successfully Unassigned"})
		if path.startswith("/customer/") and method == "GET" and not path.startswith("/customer/authorization"):
			code = path.rsplit("/", 1)[1]
			return self.send(200, {"status": True, "data": {"customer_code": code,
				"dedicated_accounts": [{"id": 9000 + len(code), "active": True, "account_number": "9" + code[-9:]}]}})
		if path == "/customer/authorization/initialize":
			ref = secrets.token_hex(7)
			MANDATES[ref] = {"email": body["email"], "active": False}
			return self.send(200, {"status": True, "data": {"redirect_url": f"https://link.paystack.com/{ref}",
				"access_code": ref, "reference": ref}})
		if path.startswith("/customer/authorization/verify/"):
			ref = path.rsplit("/", 1)[1]
			m = MANDATES.get(ref)
			if not m or not m["active"]:
				return self.send(404, {"status": False, "message": "Authorization does not exist"})
			return self.send(200, {"status": True, "data": {"authorization_code": "AUTH_DD" + ref[:6],
				"channel": "direct_debit", "bank": "Guaranty Trust Bank", "active": True,
				"customer": {"code": "CUS_x", "email": m["email"]}}})
		if path == "/customer/authorization/deactivate":
			return self.send(200, {"status": True, "message": "Authorization has been deactivated"})

		# Test controls
		if path == "/_test/pay":
			t = TX[body["reference"]]
			t.update(status=body.get("status", "success"), paid_at=now(), channel=body.get("channel", "card"))
			if "amount" in body:
				t["amount"] = int(body["amount"])
			return self.send(200, {"ok": True, "data": tx_data(t)})
		if path == "/_test/transfer":
			ref = body["reference"]
			TX[ref] = {"id": len(TX) + 1, "reference": ref, "amount": int(body["amount"]), "email": body["email"],
				"status": "success", "paid_at": now(), "channel": "dedicated_nuban",
				"authorization": {"channel": "dedicated_nuban", "reusable": False,
					"receiver_bank_account_number": body["account_number"], "receiver_bank": "Test Bank"}}
			return self.send(200, {"ok": True, "data": tx_data(TX[ref])})
		if path == "/_test/mandate":
			MANDATES[body["reference"]]["active"] = True
			return self.send(200, {"ok": True})
		if path == "/_test/closed":
			return self.send(200, CLOSED)
		if path == "/_test/log":
			return self.send(200, LOG)
		if path == "/_test/tx":
			return self.send(200, TX.get(q.get("reference")) or {})
		return self.send(404, {"status": False, "message": f"No fake for {method} {path}"})

	def do_GET(self):
		self.handle_any("GET")

	def do_POST(self):
		self.handle_any("POST")

	def do_PUT(self):
		self.handle_any("PUT")

	def do_DELETE(self):
		self.handle_any("DELETE")


if __name__ == "__main__":
	ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8899), H).serve_forever()
