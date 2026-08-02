# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Show Slack activity about a document on that document's Desk timeline."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html

STATUS_COLOURS = {
	"Sent": "green",
	"Queued": "orange",
	"Failed": "red",
	"Dead": "red",
	"Skipped": "gray",
}


def get_timeline_content(doctype: str, docname: str) -> list[dict]:
	if not frappe.db.table_exists("Slack Message Log"):
		return []

	if not frappe.has_permission("Slack Message Log", "read"):
		return []

	rows = frappe.get_all(
		"Slack Message Log",
		filters={"reference_doctype": doctype, "reference_name": docname},
		fields=["name", "creation", "owner", "status", "channel_id", "rule", "last_error"],
		order_by="creation desc",
		limit=20,
	)

	content = []
	for row in rows:
		channel = get_channel_label(row.channel_id)
		colour = STATUS_COLOURS.get(row.status, "gray")

		text = _("Slack message to {0}").format(f"<b>{escape_html(channel)}</b>")
		if row.rule:
			text += " " + _("via rule {0}").format(escape_html(row.rule))

		badge = f'<span class="indicator-pill {colour}">{escape_html(row.status)}</span>'
		if row.status in ("Failed", "Dead") and row.last_error:
			badge += f' <span class="text-muted small">{escape_html(row.last_error[:120])}</span>'

		content.append(
			{
				"icon": "share",
				"is_card": False,
				"creation": row.creation,
				"content": f"{text} {badge}",
			}
		)

	return content


def get_channel_label(channel_id: str) -> str:
	if not channel_id:
		return "Slack"

	name = frappe.db.get_value("Slack Channel", {"channel_id": channel_id}, "channel_name")
	return f"#{name}" if name else channel_id
