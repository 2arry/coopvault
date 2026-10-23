"""Give the platform console something to show (run after the other demo seeds).

usage: seed_platform.py stage|prod PASSWORD
Ola Operator (ops@example.com) is the demo platform operator. Kunle's "Computer Village Traders
Thrift" waits for review; Adaeze's "Owerri Market Women" is approved on the free Starter plan.
Safe to re-run: existing people and groups are reused.
"""
import json
import os
import subprocess
import sys

import requests

TARGET, PASSWORD = sys.argv[1], sys.argv[2]
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")  # git-ignored
BENCH = "/home/ubuntu/start-paymnet/backend"
if TARGET == "stage":
	BASE, STATE, SITE = "http://127.0.0.1:8100/api/method/coopvault.api.", f"{S}/demo_stage.json", "coopvault.test"
	H = {"Accept": "application/json", "Host": "coopvault.test"}
else:
	BASE, STATE, SITE = ("https://api-payment.secureestate.app/api/method/coopvault.api.", f"{S}/demo_prod.json",
		"api-payment.secureestate.app")
	H = {"Accept": "application/json", "Origin": "https://pay-frappe.web.app"}
state = json.load(open(STATE))


def call(path, token=None, verb="POST", **data):
	h = dict(H)
	if token:
		h["Authorization"] = "Session " + token
	r = requests.post(BASE + path, json=data, headers=h, timeout=60) if verb == "POST" else \
		requests.get(BASE + path, params=data, headers=h, timeout=60)
	if r.status_code != 200:
		print("FAILED", path, r.status_code, r.text[:1000])
		sys.exit(1)
	return r.json().get("message")


def session(key, name=None, phone=None):
	r = requests.post(BASE + "auth.login", json={"email": f"{key}@example.com", "password": PASSWORD}, headers=H, timeout=60)
	if r.status_code == 200:
		return r.json()["message"]["token"]
	return call("auth.signup", full_name=name, email=f"{key}@example.com", password=PASSWORD, phone=phone)["token"]


ops = session("ops", "Ola Operator")
if not call("auth.me", ops, "GET")["user"]["is_platform"]:
	code = "import frappe; frappe.get_doc('User', 'ops@example.com').add_roles('Platform Operator'); frappe.db.commit()"
	subprocess.run(["bench", "--site", SITE, "console"], input=code, cwd=BENCH, capture_output=True, text=True, check=True)
	ops = session("ops")

groups = {g["cooperative_name"]: g for g in call("platform.list_groups", ops, "GET")}
kunle = session("kunle", "Kunle Adeyemi", "08031000012")
if "Computer Village Traders Thrift" not in groups:
	call("coop.create_cooperative", kunle, cooperative_name="Computer Village Traders Thrift", contribution_amount=5000,
		contribution_frequency="Weekly", location="Ikeja, Lagos", registration_number="LSC/2026/1182", expected_members=60,
		description="Phone and laptop traders at Computer Village saving weekly together.")
adaeze = session("adaeze", "Adaeze Nwosu", "08031000013")
if "Owerri Market Women" not in groups:
	c = call("coop.create_cooperative", adaeze, cooperative_name="Owerri Market Women", contribution_amount=2000,
		contribution_frequency="Weekly", location="Owerri, Imo", expected_members=25)
	call("platform.review_group", ops, name=c["name"], approve=1, note="Called Adaeze: a registered market association")
	call("platform.change_plan", ops, name=c["name"], plan="Starter", note="Small group: starting on the free plan")
	state["platform_group"] = c["name"]

json.dump(state, open(STATE, "w"))
print(json.dumps({"operator": "ops@example.com", "groups": sorted(g["cooperative_name"] for g in call("platform.list_groups", ops, "GET"))
	if TARGET == "prod" else len(call("platform.list_groups", ops, "GET"))}))
