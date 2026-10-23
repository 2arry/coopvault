"""Phase 6: the platform console. Onboarding review, plans and limits, subscriptions paid through
Paystack, lapsed subscriptions, suspending and reinstating a group, and the console never
showing a group's money."""
from datetime import date, timedelta

from harness import (
	PW, RUN, call, check, console, done, fake, get, guard, operator, pay, rejected, signup, webhook,
)

guard()
OPS = operator()
people = {k: signup(k, i, n) for i, (k, n) in enumerate([("fo", "Funmi Founder"), ("tr", "Tayo Treasurer"),
	("m1", "Mide Member"), ("m2", "Mo Member"), ("m3", "Mary Member"), ("m4", "Moses Member"), ("df", "Dapo Declined")], 1)}
T = {k: v["token"] for k, v in people.items()}
FOUNDER = T["fo"]

# --- Onboarding ------------------------------------------------------------------------------------
check("operators are flagged for the app", get("auth.me", OPS)["user"]["is_platform"] is True)
check("members aren't", get("auth.me", FOUNDER)["user"]["is_platform"] is False)
coop = call("coop.create_cooperative", FOUNDER, cooperative_name=f"Ikeja Traders {RUN}", contribution_amount=5000,
	contribution_frequency="Monthly", location="Ikeja, Lagos", registration_number="LSC/2025/0417", expected_members=40)
CO = coop["name"]
check("a new group waits for review", coop["status"] == "Pending review")
n = console(f"print('N=%s' % frappe.db.count('Coop Notification', {{'user': 'ops.{RUN}@example.com', 'title': ['like', 'New cooperative to review%']}}))")
check("platform operators are told", int(n.split("N=")[1].split()[0]) >= 1)
rejected("nobody can join a group under review", "coop.join_cooperative", T["m1"], join_code=coop["join_code"],
	contains="not accepting")
call("coop.update_cooperative", FOUNDER, cooperative=CO, description="Traders at Computer Village")
check("the founder can set the group up meanwhile", get("coop.get_dashboard", FOUNDER, cooperative=CO)["cooperative"]["description"]
	== "Traders at Computer Village")
rejected("no members added by hand before approval", "coop.add_member", FOUNDER, cooperative=CO, full_name="X Y",
	phone="08090000000", contains="approved")
rejected("no payments before approval", "payments.initialize", FOUNDER, cooperative=CO, purpose="Contribution",
	amount=5000, contains="isn't open")
rejected("members can't open the console", "platform.overview", FOUNDER, "GET", contains="platform team")

ov = get("platform.overview", OPS)
check("the review queue shows the group", any(g["name"] == CO and g["founder"] == "Funmi Founder" for g in ov["pending"]))
g = get("platform.get_group", OPS, name=CO)
check("the console shows who and where", g["location"] == "Ikeja, Lagos" and g["registration_number"] == "LSC/2025/0417"
	and g["officers"][0]["full_name"] == "Funmi Founder")
money_words = ("savings", "pool", "balance", "contribution", "loan", "repaid", "withdraw", "journal", "account_number")
leaks = [k for k in g if any(w in k.lower() for w in money_words) and k not in ("loan_products",)]
check("the console returns nothing about the group's money", not leaks, leaks)
rejected("operators can't read a group's dashboard", "coop.get_dashboard", OPS, "GET", cooperative=CO,
	contains="not a member")
rejected("or anyone's statement", "coop.get_statement", OPS, "GET", cooperative=CO, contains="not a member")
rejected("declining needs a reason", "platform.review_group", OPS, name=CO, approve=0, note="")
g = call("platform.review_group", OPS, name=CO, approve=1, note="Spoke to Funmi on the phone")
check("approved: open, on the default plan, trial started", g["status"] == "Active" and g["plan"] == "Growth" and
	g["subscription"]["state"] == "trial" and g["trial_ends_on"] == (date.today() + timedelta(days=30)).isoformat(),
	(g["status"], g["plan"], g["subscription"]["state"], g["trial_ends_on"]))
check("the founder is told", any("is approved" in i["title"] for i in get("notifications.list_notifications", FOUNDER)["items"]))
for k in ("tr", "m1", "m2"):
	call("coop.join_cooperative", T[k], join_code=coop["join_code"])
for m in get("coop.list_members", FOUNDER, cooperative=CO):
	if m["status"] == "Pending":
		call("coop.update_member", FOUNDER, member=m["name"], status="Active")
ids = {m["full_name"]: m["name"] for m in get("coop.list_members", FOUNDER, cooperative=CO)}
call("coop.update_member", FOUNDER, member=ids["Tayo Treasurer"], role="Treasurer")
check("members can join once approved", len(ids) == 4)

# --- Plans and limits ------------------------------------------------------------------------------
rejected("members can't edit plans", "platform.save_plan", FOUNDER, plan_name="Hack", price_monthly=0)
plans_ = call("platform.save_plan", OPS, plan_name=f"Tiny {RUN}", price_monthly=1000, max_members=5, max_loan_products=1,
	max_savings_products=1, online_payments=1, dedicated_accounts=0, autosave=0, trial_days=0)
TINY = f"Tiny {RUN}"
check("a new plan", any(p["name"] == TINY for p in plans_))
call("platform.change_plan", OPS, name=CO, plan=TINY, note="Trying the small plan")
call("coop.join_cooperative", T["m3"], join_code=coop["join_code"])
rejected("the plan's member limit holds", "coop.join_cooperative", T["m4"], join_code=coop["join_code"], contains="allows 5 members")
rejected("adding by hand counts too", "coop.add_member", FOUNDER, cooperative=CO, full_name="Extra Person",
	phone=f"0809{int(RUN, 16) % 10**7:07d}", contains="allows 5 members")
rejected("one loan product on this plan", "finance.save_loan_product", FOUNDER, cooperative=CO, product_name="Quick",
	interest_method="Flat", interest_rate=2, rate_period="Per month", contains="allows 1 loan product ")
rejected("no voluntary savings on this plan", "finance.save_savings_product", FOUNDER, cooperative=CO,
	product_name="Christmas", contains="allows 1 savings product ")
cfg = get("payments.config", T["m1"], cooperative=CO)
check("features follow the plan", cfg["autosave"] is False and cfg["dedicated_accounts"] is False and cfg["online"] is True, cfg)
rejected("account numbers aren't on this plan", "payments.request_dedicated_account", T["m1"], cooperative=CO,
	contains="isn't included")
sub = get("subscription.get_subscription", FOUNDER, cooperative=CO)
check("admins see their plan, limits and usage", sub["subscription"]["plan"]["name"] == TINY and sub["usage"]["members"] == 5)
starter = next(p for p in sub["plans"] if p["name"] == "Starter")
check("plans that fit are offered", not starter["problems"] and next(p for p in sub["plans"] if p["name"] == TINY)["current"])
rejected("members can't see the subscription", "subscription.get_subscription", T["m1"], "GET", cooperative=CO)

# --- Paying for months --------------------------------------------------------------------------------
rejected("only the admin pays", "subscription.pay", T["tr"], cooperative=CO, plan="Growth", months=3)
rejected("a whole number of months on offer", "subscription.pay", FOUNDER, cooperative=CO, plan="Growth", months=2)
rejected("a free plan isn't paid for", "subscription.pay", FOUNDER, cooperative=CO, plan="Starter", months=1, contains="free")
init = call("subscription.pay", FOUNDER, cooperative=CO, plan="Growth", months=3)
check("charged the plan price for the months", init["amount"] == 45000 and init["reference"].startswith("SUB-"))
log = [l for l in __import__("requests").get("http://127.0.0.1:8899/_test/log").json() if l["path"] == "/transaction/initialize"][-1]["body"]
check("paid to the platform, not split to the group", "subaccount" not in log and log["amount"] == 4500000, log)
fake("/_test/pay", reference=init["reference"])
res = call("subscription.verify", FOUNDER, reference=init["reference"])
g = get("platform.get_group", OPS, name=CO)
check("paid: Growth, months follow on from the trial", res["status"] == "Success" and g["plan"] == "Growth" and
	g["paid_until"] > g["trial_ends_on"] and g["subscription"]["state"] == "active", (res["status"], g["plan"], g["paid_until"]))
paid_until = g["paid_until"]
webhook("charge.success", {"reference": init["reference"]})
call("subscription.verify", FOUNDER, reference=init["reference"])
check("a payment counts once", get("platform.get_group", OPS, name=CO)["paid_until"] == paid_until)
check("logged and shown to admins", any(a["action"] == "Subscription paid" for a in g["actions"]) and
	len(get("subscription.get_subscription", FOUNDER, cooperative=CO)["payments"]) == 1)
journals = get("accounts.journals", FOUNDER, cooperative=CO)
check("the subscription isn't in the group's books", not any("SUB-" in str(j) for j in (journals or [])))
check("Growth allows a second loan product", any(p["product_name"] == "Quick" for p in call("finance.save_loan_product", FOUNDER,
	cooperative=CO, product_name="Quick", interest_method="Flat", interest_rate=2, rate_period="Per month")))

# --- Money keeps moving; growth pauses when the subscription lapses -----------------------------------
call("auth.set_payout_account", T["m1"], bank_code="001", account_number="0012345701", password=PW)
console(f"frappe.db.set_value('Coop Member', '{ids['Mide Member']}', 'payout_changed_on', '2026-01-01 00:00:00')")
pay(T["m1"], CO, 20000)
long_ago = (date.today() - timedelta(days=40)).isoformat()
console(f"frappe.db.set_value('Cooperative', '{CO}', {{'trial_ends_on': '{long_ago}', 'paid_until': '{long_ago}'}})")
check("lapsed after the grace days", get("subscription.get_subscription", FOUNDER, cooperative=CO)["subscription"]["state"] == "lapsed")
std = get("finance.loan_products", T["m1"], cooperative=CO)[0]
rejected("no new loans while lapsed", "finance.apply_loan", T["m1"], cooperative=CO, amount=5000, tenure_months=2,
	product=std["name"], contains="subscription ended")
rejected("no new members while lapsed", "coop.join_cooperative", T["m4"], join_code=coop["join_code"], contains="subscription")
check("payments still go through", pay(T["m1"], CO, 1000)["status"] == "Success")
w = call("finance.request_withdrawal", T["m1"], cooperative=CO, amount=1000, reason="Still mine")
check("and withdrawals", w["status"] == "Pending")
recent = (date.today() - timedelta(days=3)).isoformat()
console(f"frappe.db.set_value('Cooperative', '{CO}', 'paid_until', '{recent}')")
check("a few days late is grace, not lapsed", get("subscription.get_subscription", FOUNDER, cooperative=CO)["subscription"]["state"] == "grace")
rejected("extending needs a reason", "platform.extend_subscription", OPS, name=CO, days=30, note="")
g = call("platform.extend_subscription", OPS, name=CO, days=30, note="Paid by bank transfer to Cooperative360")
check("extended by the platform", g["paid_until"] == (date.today() + timedelta(days=30)).isoformat() and
	g["subscription"]["state"] == "active", g["paid_until"])

# --- Suspending -----------------------------------------------------------------------------------------
in_flight = call("payments.initialize", T["m1"], cooperative=CO, purpose="Contribution", amount=3000)
rejected("suspending needs a reason", "platform.suspend_group", OPS, name=CO, reason="")
call("platform.suspend_group", OPS, name=CO, reason="Members complained about missing payouts; we're looking into it")
dash = get("coop.get_dashboard", T["m1"], cooperative=CO)
check("members still see everything", dash["cooperative"]["status"] == "Suspended" and dash["balances"]["savings"] == 21000
	and "missing payouts" in dash["cooperative"]["status_reason"])
check("and are told", any("paused by Cooperative360" in i["title"] for i in get("notifications.list_notifications", T["m1"])["items"]))
rejected("no payments while paused", "payments.initialize", T["m1"], cooperative=CO, purpose="Contribution", amount=1000,
	contains="paused")
rejected("no withdrawals while paused", "finance.cancel_withdrawal", T["m1"], name=w["name"], contains="paused")
rejected("no settings changes while paused", "coop.update_cooperative", FOUNDER, cooperative=CO, description="x", contains="paused")
fake("/_test/pay", reference=in_flight["reference"])
check("a checkout already under way still completes", call("payments.verify", T["m1"], reference=in_flight["reference"])["status"]
	== "Success")
check("the admin can still pay the subscription", call("subscription.pay", FOUNDER, cooperative=CO, plan="Growth", months=1)["amount"]
	== 15000)
rejected("only an open or paused group can be reinstated", "platform.review_group", OPS, name=CO, approve=1)
call("platform.reinstate_group", OPS, name=CO, note="Payouts checked: all were made")
check("open again", get("coop.get_dashboard", T["m1"], cooperative=CO)["cooperative"]["status"] == "Active")
call("finance.cancel_withdrawal", T["m1"], name=w["name"])
g = get("platform.get_group", OPS, name=CO)
check("every decision is logged", {"Submitted", "Approved", "Plan changed", "Subscription paid", "Subscription extended",
	"Suspended", "Reinstated"} <= {a["action"] for a in g["actions"]}, [a["action"] for a in g["actions"]])
check("the console shows last activity", bool(g["last_active"]))

# --- Declined ----------------------------------------------------------------------------------------------
dc = call("coop.create_cooperative", T["df"], cooperative_name=f"Quick Money {RUN}", location="Online")
call("platform.review_group", OPS, name=dc["name"], approve=0, note="Looks like an investment scheme, not a cooperative")
dd = get("coop.get_dashboard", T["df"], cooperative=dc["name"])
check("a declined group sees why", dd["cooperative"]["status"] == "Declined" and "investment scheme" in dd["cooperative"]["status_reason"])
rejected("and can't change anything", "coop.update_cooperative", T["df"], cooperative=dc["name"], description="x",
	contains="wasn't approved")
g = call("platform.reinstate_group", OPS, name=dc["name"], note="Founder sent their registration details")
check("a declined group can be approved later, with its trial", g["status"] == "Active" and g["subscription"]["state"] == "trial")
lst = get("platform.list_groups", OPS, q=f"ikeja traders {RUN}")
check("search groups", [x["name"] for x in lst] == [CO], [x["name"] for x in lst])
check("filter by status", all(x["status"] == "Active" for x in get("platform.list_groups", OPS, status="Active")))

# The test plan isn't offered to anyone after this run
call("platform.save_plan", OPS, name=TINY, plan_name=TINY, enabled=0)
check("a plan can be switched off", not next(p for p in get("platform.list_plans", OPS) if p["name"] == TINY)["enabled"])

done("platform console")
