"""Meetings: notice, replies, attendance, minutes and resolutions (plain text; no documents).

The secretary (or the chairman or an admin) calls a meeting and every member is told. Members say
whether they're coming or send apologies. On the day the secretary marks who was there, then
writes the minutes and each resolution with its votes. Publishing the minutes shows them to every
member, and published minutes can't be changed: a correction goes in the next meeting's minutes,
as it would in a minute book.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, format_datetime, get_datetime, now_datetime

from coopvault.api import can, get_membership
from coopvault.notify import members_of, notify

KINDS = ("General meeting", "Annual general meeting", "Executive meeting", "Special meeting")
OUTCOMES = ("Passed", "Not passed", "Deferred")
LIST_FIELDS = ["name", "title", "kind", "starts_at", "venue", "status", "agenda", "minutes_published", "published_on"]
REMIND_HOURS_BEFORE = 24
# A meeting stays "upcoming" for a few hours after it starts, so it's easy to find on the day
STILL_ON_HOURS = 6


def _when(value):
	return format_datetime(value, "EEE d MMM, h:mm a")


def _json(value):
	if isinstance(value, str):
		return json.loads(value or "[]")
	return value or []


def _counts(doc):
	rows = doc.attendance
	return {
		"coming": sum(1 for r in rows if r.response == "Coming"),
		"apologies": sum(1 for r in rows if r.response == "Apology"),
		"present": sum(1 for r in rows if cint(r.present)),
		"members": frappe.db.count("Coop Member", {"cooperative": doc.cooperative, "status": "Active"}),
	}


def _summary(doc, me):
	mine = next((r for r in doc.attendance if r.member == me.name), None) if me.name else None
	return {
		**{f: doc.get(f) for f in LIST_FIELDS},
		"my_response": mine.response if mine else None,
		"i_was_present": bool(mine and cint(mine.present)),
		"counts": _counts(doc),
	}


def _get(name, manage=False):
	doc = frappe.get_doc("Coop Meeting", name)
	me = get_membership(doc.cooperative, capability="meetings" if manage else None)
	return doc, me


def _add_member_row(doc, member):
	row = next((r for r in doc.attendance if r.member == member), None)
	if not row:
		row = doc.append("attendance", {"member": member,
			"member_name": frappe.db.get_value("Coop Member", member, "full_name")})
	return row


# Reading -----------------------------------------------------------------------------------


@frappe.whitelist()
def list_meetings(cooperative):
	me = get_membership(cooperative)
	soon = add_to_date(now_datetime(), hours=-STILL_ON_HOURS)
	upcoming, past = [], []
	for name in frappe.get_all("Coop Meeting", filters={"cooperative": cooperative}, pluck="name",
			order_by="starts_at desc", limit=60):
		doc = frappe.get_doc("Coop Meeting", name)
		row = _summary(doc, me)
		if doc.status == "Scheduled" and get_datetime(doc.starts_at) >= soon:
			upcoming.append(row)
		else:
			past.append(row)
	upcoming.reverse()  # soonest first
	return {"upcoming": upcoming, "past": past, "can_manage": can(me, "meetings")}


@frappe.whitelist()
def get_meeting(name):
	doc, me = _get(name)
	manage = can(me, "meetings")
	data = _summary(doc, me)
	data.update(
		cooperative=doc.cooperative,
		can_manage=manage,
		can_reply=bool(me.name) and doc.status == "Scheduled" and get_datetime(doc.starts_at) > now_datetime(),
		# Officers see the full register; members see who said they're coming and, once the
		# minutes are out, who was there
		attendance=[
			{"member": r.member, "member_name": r.member_name, "response": r.response, "present": cint(r.present),
				"note": r.note}
			for r in doc.attendance
			if manage or r.response == "Coming" or (doc.minutes_published and cint(r.present))
		],
	)
	if doc.minutes_published or manage:
		data.update(
			minutes=doc.minutes,
			resolutions=[{f: r.get(f) for f in ("resolution", "outcome", "votes_for", "votes_against", "abstained")}
				for r in doc.resolutions],
			minutes_by=frappe.db.get_value("User", doc.minutes_by, "full_name") if doc.minutes_by else None,
		)
	if manage:
		data["members"] = frappe.get_all("Coop Member", filters={"cooperative": doc.cooperative, "status": "Active"},
			fields=["name", "full_name", "member_number"], order_by="full_name asc")
	return data


def next_meeting(cooperative, me):
	"""The next meeting for the Home screen, with the member's reply."""
	name = frappe.db.get_value(
		"Coop Meeting",
		{"cooperative": cooperative, "status": "Scheduled",
			"starts_at": [">=", add_to_date(now_datetime(), hours=-STILL_ON_HOURS)]},
		"name",
		order_by="starts_at asc",
	)
	return _summary(frappe.get_doc("Coop Meeting", name), me) if name else None


# Calling a meeting ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def save_meeting(cooperative, title, starts_at, kind="General meeting", venue=None, agenda=None, name=None):
	me = get_membership(cooperative, capability="meetings")
	title = (title or "").strip()
	if len(title) < 3:
		frappe.throw(_("Give the meeting a title"))
	if kind not in KINDS:
		frappe.throw(_("Choose the kind of meeting"))
	starts_at = get_datetime(str(starts_at).replace("T", " "))
	if name:
		doc = frappe.get_doc("Coop Meeting", name)
		if doc.cooperative != cooperative or doc.status != "Scheduled":
			frappe.throw(_("Only meetings that haven't happened yet can be changed"))
		moved = get_datetime(doc.starts_at) != starts_at or (doc.venue or "") != (venue or "")
	else:
		if starts_at < now_datetime():
			frappe.throw(_("Choose a time in the future"))
		doc = frappe.new_doc("Coop Meeting")
		doc.cooperative = cooperative
		doc.status = "Scheduled"
		for m in frappe.get_all("Coop Member", filters={"cooperative": cooperative, "status": "Active"},
				fields=["name", "full_name"]):
			doc.append("attendance", {"member": m.name, "member_name": m.full_name})
		moved = False
	doc.update({"title": title, "kind": kind, "starts_at": starts_at, "venue": (venue or "").strip(),
		"agenda": (agenda or "").strip()})
	if moved:
		doc.reminded = 0
	doc.save(ignore_permissions=True)
	if not name or moved:
		heading = _("{0}: {1}").format(_("Meeting moved") if moved else _(kind), title)
		notify(
			members_of(cooperative, exclude_users=[me.user]),
			heading,
			_("{0}{1}. Let the secretary know if you're coming.").format(
				_when(doc.starts_at), _(" at {0}").format(doc.venue) if doc.venue else ""),
			link=f"/meetings/{doc.name}",
			cooperative=cooperative,
			kind="meeting",
		)
	return get_meeting(doc.name)


@frappe.whitelist(methods=["POST"])
def cancel_meeting(name, note=None):
	doc, me = _get(name, manage=True)
	if doc.status != "Scheduled":
		frappe.throw(_("Only a meeting that hasn't happened yet can be cancelled"))
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	notify(members_of(doc.cooperative, exclude_users=[me.user]), _("Meeting cancelled: {0}").format(doc.title),
		note or _("It was planned for {0}.").format(_when(doc.starts_at)), link=f"/meetings/{doc.name}",
		cooperative=doc.cooperative, kind="meeting")
	return get_meeting(name)


@frappe.whitelist(methods=["POST"])
def reply(name, response):
	"""A member says they're coming, or sends apologies."""
	doc, me = _get(name)
	if not me.name:
		frappe.throw(_("Only members can reply"))
	if response not in ("Coming", "Apology"):
		frappe.throw(_("Say whether you're coming"))
	if doc.status != "Scheduled" or get_datetime(doc.starts_at) <= now_datetime():
		frappe.throw(_("This meeting has already started"))
	_add_member_row(doc, me.name).response = response
	doc.save(ignore_permissions=True)
	return get_meeting(name)


# On the day and after ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def record_attendance(name, present):
	"""Mark who was there (everyone else is absent). Can be corrected until the minutes are out."""
	doc, _me = _get(name, manage=True)
	if doc.status == "Cancelled" or doc.minutes_published:
		frappe.throw(_("Attendance can't be changed now"))
	if get_datetime(doc.starts_at) > add_to_date(now_datetime(), hours=1):
		frappe.throw(_("Attendance is taken on the day of the meeting"))
	present = set(_json(present))
	valid = set(frappe.get_all("Coop Member", filters={"cooperative": doc.cooperative, "name": ["in", list(present) or [""]]},
		pluck="name"))
	if present - valid:
		frappe.throw(_("Choose members of this group"))
	for member in present:
		_add_member_row(doc, member)
	for row in doc.attendance:
		row.present = 1 if row.member in present else 0
	doc.status = "Held"
	doc.save(ignore_permissions=True)
	return get_meeting(name)


@frappe.whitelist(methods=["POST"])
def save_minutes(name, minutes, resolutions=None, publish=0):
	doc, me = _get(name, manage=True)
	if doc.status != "Held":
		frappe.throw(_("Take attendance first: minutes are for a meeting that took place"))
	if doc.minutes_published:
		frappe.throw(_("These minutes are published and can't be changed. Note any correction in the next meeting's minutes."))
	doc.minutes = (minutes or "").strip()
	doc.set("resolutions", [])
	present = sum(1 for r in doc.attendance if cint(r.present))
	for r in _json(resolutions):
		text = (r.get("resolution") or "").strip()
		if not text:
			continue
		votes = [max(0, cint(r.get(k))) for k in ("votes_for", "votes_against", "abstained")]
		if sum(votes) > present:
			frappe.throw(_("More votes than the {0} members present on: {1}").format(present, text[:60]))
		outcome = r.get("outcome") if r.get("outcome") in OUTCOMES else "Passed"
		doc.append("resolutions", {"resolution": text, "outcome": outcome, "votes_for": votes[0],
			"votes_against": votes[1], "abstained": votes[2]})
	if cint(publish):
		if len(doc.minutes) < 20:
			frappe.throw(_("Write the minutes before publishing them"))
		doc.update({"minutes_published": 1, "minutes_by": me.user, "published_on": now_datetime()})
	doc.save(ignore_permissions=True)
	if cint(publish):
		notify(members_of(doc.cooperative, exclude_users=[me.user]), _("Minutes of {0}").format(doc.title),
			_("{0} resolution(s) recorded. Read them in the app.").format(len(doc.resolutions)),
			link=f"/meetings/{doc.name}", cooperative=doc.cooperative, kind="meeting")
	return get_meeting(name)


# Reminders -------------------------------------------------------------------------------------


def remind_meetings():
	"""Hourly: remind members the day before, except those who sent apologies."""
	now = now_datetime()
	for name in frappe.get_all("Coop Meeting", filters={"status": "Scheduled", "reminded": 0,
			"starts_at": ["between", [now, add_to_date(now, hours=REMIND_HOURS_BEFORE)]]}, pluck="name"):
		doc = frappe.get_doc("Coop Meeting", name)
		apologies = {r.member for r in doc.attendance if r.response == "Apology"}
		users = [m.user for m in frappe.get_all("Coop Member", filters={"cooperative": doc.cooperative, "status": "Active"},
			fields=["name", "user"]) if m.name not in apologies]
		notify(users, _("Reminder: {0}").format(doc.title),
			_("{0}{1}.").format(_when(doc.starts_at), _(" at {0}").format(doc.venue) if doc.venue else ""),
			link=f"/meetings/{doc.name}", cooperative=doc.cooperative, kind="meeting")
		doc.db_set("reminded", 1, update_modified=False)
