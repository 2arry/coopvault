"""Fixes from the October review: late/unmatched account-number transfers, push after sign-out,
phone number changes."""
from harness import (
	N, PW, RUN, call, check, console, done, err, fake, fake_get, get, guard, new_cooperative, raw, rejected, signup,
	webhook,
)

guard()
a, b, c, d = signup("ada", 1, "Ada Admin"), signup("bayo", 2, "Bayo Bello"), signup("chi", 3, "Chi Obi"), signup("dan", 4, "Dan Eze")
A, B, C, D = a["token"], b["token"], c["token"], d["token"]
coop, ids = new_cooperative(A, "Review", [(B, "Member"), (C, "Member"), (D, "Treasurer")])
CO = coop["name"]
console(f"frappe.db.set_single_value('CoopVault Settings', 'enable_dedicated_accounts', 1)")


def give_account_number(token, member, number, customer):
	call("payments.request_dedicated_account", token, cooperative=CO)
	email = get("auth.me", token)["user"]["email"]
	webhook("dedicatedaccount.assign.success", {"customer": {"email": email, "customer_code": customer},
		"dedicated_account": {"id": int(number[-4:]), "account_number": number, "account_name": "PAYSTACK/Test",
		"bank": {"name": "Test Bank"}}})
	return email


def transfer(email, number, amount, ref):
	fake("/_test/transfer", reference=ref, amount=amount * 100, email=email, account_number=number)
	return webhook("charge.success", {"reference": ref})


# --- 1. Transfers to an account number --------------------------------------------------
num_b = f"77{int(RUN, 16) % 10**8:08d}"
email_b = give_account_number(B, ids[B], num_b, f"CUS_b{RUN}")
r = call("coop.leave_cooperative", B, cooperative=CO)
check("member with no money can leave", r["ok"])
check("leaving switches the account number off at Paystack", str(int(num_b[-4:])) in fake_get("/_test/closed"))

res = transfer(email_b, num_b, 5000, f"LATE-{RUN}")
check("late transfer to a former member's number is accepted (no retry loop)", res.status_code == 200, res.text[:200])
mine = get("auth.me", B)["cooperatives"]
left = next((m for m in mine if m["cooperative"] == CO), None)
NET = 5000 - 5000 * 0.015  # the fake Paystack keeps 1.5%; members bear the transfer fee here
check("the group still shows for them while it holds their money", left and left["status"] == "Left" and left["savings"] == NET, left)
staff_notes = get("notifications.list_notifications", A)["items"]
check("staff are told about it", any("sent ₦5,000 to their account number" in n["title"] for n in staff_notes))
call("auth.set_payout_account", B, bank_code="001", account_number="0012345678", password=PW)
elig = get("finance.withdrawal_eligibility", B, cooperative=CO)
check("former member can withdraw it", elig["withdrawable"] == NET, elig["withdrawable"])
wd = call("finance.request_withdrawal", B, cooperative=CO, amount=NET, reason="Sent by mistake after leaving")
check("withdrawal requested", wd["status"] == "Pending")

# Suspended member
num_c = f"78{int(RUN, 16) % 10**8:08d}"
email_c = give_account_number(C, ids[C], num_c, f"CUS_c{RUN}")
call("coop.update_member", A, member=ids[C], status="Suspended")
res = transfer(email_c, num_c, 3000, f"SUSP-{RUN}")
check("transfer from a suspended member is recorded", res.status_code == 200 and
	get("coop.get_statement", A, cooperative=CO, member=ids[C])["balances"]["savings"] == 3000 * 0.985)

# Nobody's number
res = transfer("ghost@example.com", "7999999999", 2000, f"GHOST-{RUN}")
check("unmatched transfer accepted, not retried forever", res.status_code == 200, res.text[:200])
status = console(f"print(frappe.db.get_value('Coop Payment Transaction', 'GHOST-{RUN}', 'status'))")
check("unmatched transfer kept for follow-up", "Unmatched" in status, status.strip()[-40:])

# The person who left can get an account number in a new group
other, oids = new_cooperative(D, "Second", [(B, "Member")])
r = call("payments.request_dedicated_account", B, cooperative=other["name"])
check("someone who left can get an account number in another group", r["status"] == "Requested", r)

# --- 3. Push notifications end with the login ------------------------------------------
endpoint = f"https://push.example.com/{RUN}"
keys = {"p256dh": "BOrfakeKey" + RUN, "auth": "authsecret" + RUN}
s1 = call("auth.login", email=f"dan.{RUN}@example.com", password=PW)["token"]
s2 = call("auth.login", email=f"dan.{RUN}@example.com", password=PW)["token"]
call("notifications.subscribe", s1, subscription={"endpoint": endpoint, "keys": keys})
check("device subscribed", get("notifications.device_status", s2, endpoint=endpoint)["subscribed"])
call("notifications.set_privacy", s1, endpoint=endpoint, discreet=1)
check("lock-screen privacy saved", get("notifications.device_status", s2, endpoint=endpoint)["discreet"])
call("auth.logout", s1)
check("signing out removes the device from push", not get("notifications.device_status", s2, endpoint=endpoint)["subscribed"])
s3 = call("auth.login", email=f"dan.{RUN}@example.com", password=PW)["token"]
call("notifications.subscribe", s3, subscription={"endpoint": endpoint + "x", "keys": keys})
call("auth.logout_all_devices", s2)
s4 = call("auth.login", email=f"dan.{RUN}@example.com", password=PW)["token"]
check("log out everywhere removes every device", not get("notifications.device_status", s4, endpoint=endpoint + "x")["subscribed"])

# --- 4. Phone number changes -----------------------------------------------------------
NEW_PHONE = f"0809{N:06d}7"
rejected("phone change needs the password", "auth.update_profile", C, phone=NEW_PHONE, contains="password")
rejected("phone change refuses a wrong password", "auth.update_profile", C, phone=NEW_PHONE, password="wrong-one")
p = call("auth.update_profile", C, phone=NEW_PHONE, password=PW)
check("phone changed with the password", p["mobile_no"] == "+234809" + f"{N:06d}7", p["mobile_no"])
check("name change alone needs no password", call("auth.update_profile", C, full_name="Chi Obi-Okafor")["full_name"] == "Chi Obi-Okafor")
mine = get("notifications.list_notifications", C)["items"]
check("member is told their phone changed", any("phone number was changed" in n["title"] for n in mine))
call("coop.update_member", A, member=ids[C], status="Active")
status, msg = err("auth.set_payout_account", C, bank_code="001", account_number="0012345679", password=PW)
check("payout changes pause after a phone change", status != 200 and "paused" in msg, msg)
code = call("coop.create_reset_code", A, cooperative=CO, member=ids[C])
check("admin sees the phone changed recently when making a reset code", bool(code.get("phone_changed_on")))
admin_notes = get("notifications.list_notifications", A)["items"]
check("admins are told about the phone change", any("changed their phone number" in n["title"] for n in admin_notes))

done("review fixes")
