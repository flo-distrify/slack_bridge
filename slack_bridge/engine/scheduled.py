# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Date and time offset triggers.

Mirrors core Notification: a daily job for day offsets and a five-minute tick for
minute offsets. Each tick covers a non-overlapping window, so a document fires once.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, nowdate

from slack_bridge.engine.rules import is_enabled, queue_rule

DAY_EVENTS = ("Days Before", "Days After")
MINUTE_EVENTS = ("Minutes Before", "Minutes After")
TICK_MINUTES = 5


def trigger_daily_rules() -> None:
	if not is_enabled():
		return

	for rule in get_rules(DAY_EVENTS):
		try:
			run_day_rule(rule)
		except Exception:
			frappe.log_error(
				title="Slack Bridge: scheduled rule failed",
				message=f"Rule: {rule.name}\n\n{frappe.get_traceback()}",
			)


def trigger_minute_rules() -> None:
	if not is_enabled():
		return

	for rule in get_rules(MINUTE_EVENTS):
		try:
			run_minute_rule(rule)
		except Exception:
			frappe.log_error(
				title="Slack Bridge: scheduled rule failed",
				message=f"Rule: {rule.name}\n\n{frappe.get_traceback()}",
			)


def get_rules(events: tuple[str, ...]):
	return frappe.get_all(
		"Slack Notification Rule",
		filters=[["enabled", "=", 1], ["event", "in", list(events)]],
		fields=["name", "document_type", "event", "date_field", "days_in_advance"],
	)


def run_day_rule(rule) -> None:
	if not rule.date_field:
		return

	offset = rule.days_in_advance or 0
	days = offset if rule.event == "Days Before" else -offset
	target_date = add_to_date(nowdate(), days=days, as_string=True, as_datetime=False)

	for name in get_matching_documents(rule, target_date, target_date, date_only=True):
		enqueue(rule, name)


def run_minute_rule(rule) -> None:
	if not rule.date_field:
		return

	offset = rule.days_in_advance or 0
	minutes = offset if rule.event == "Minutes Before" else -offset

	window_end = add_to_date(now_datetime(), minutes=minutes)
	window_start = add_to_date(window_end, minutes=-TICK_MINUTES)

	for name in get_matching_documents(rule, window_start, window_end):
		enqueue(rule, name)


def get_matching_documents(rule, start, end, date_only: bool = False) -> list[str]:
	meta = frappe.get_meta(rule.document_type)
	if not meta.get_field(rule.date_field) and rule.date_field not in ("creation", "modified"):
		return []

	if date_only:
		filters = [[rule.document_type, rule.date_field, "=", start]]
	else:
		filters = [
			[rule.document_type, rule.date_field, ">", get_datetime(start)],
			[rule.document_type, rule.date_field, "<=", get_datetime(end)],
		]

	# Submittable doctypes: never notify about cancelled paperwork.
	if meta.is_submittable:
		filters.append([rule.document_type, "docstatus", "<", 2])

	rows = frappe.get_all(rule.document_type, filters=filters, fields=["name"], limit=500)
	return [row.name for row in rows]


def enqueue(rule, docname: str) -> None:
	doc = frappe.get_doc(rule.document_type, docname)
	queue_rule(rule.name, doc, rule.event)
