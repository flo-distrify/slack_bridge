# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Block Kit construction helpers and Slack's structural limits."""

from __future__ import annotations

import json

import frappe
from frappe import _

MAX_BLOCKS = 50
MAX_TEXT_LENGTH = 3000
MAX_ACTION_VALUE = 2000
MAX_PRIVATE_METADATA = 3000
MAX_BUTTONS_PER_ACTIONS_BLOCK = 5

STYLE_MAP = {"Primary": "primary", "Danger": "danger"}


def truncate(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
	text = text or ""
	if len(text) <= limit:
		return text
	return text[: limit - 1] + "…"


def section(text: str) -> dict:
	return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(text)}}


def context(text: str) -> dict:
	return {"type": "context", "elements": [{"type": "mrkdwn", "text": truncate(text, 2000)}]}


def divider() -> dict:
	return {"type": "divider"}


def button(
	label: str,
	action_id: str,
	value: str | None = None,
	style: str | None = None,
	url: str | None = None,
	confirm: dict | None = None,
) -> dict:
	element = {
		"type": "button",
		"text": {"type": "plain_text", "text": truncate(label, 75), "emoji": True},
		"action_id": action_id,
	}

	if url:
		element["url"] = url
	if value is not None:
		element["value"] = truncate(value, MAX_ACTION_VALUE)
	if style in ("primary", "danger"):
		element["style"] = style
	if confirm:
		element["confirm"] = confirm

	return element


def confirm_dialog(text: str, title: str | None = None, confirm_label: str | None = None) -> dict:
	return {
		"title": {"type": "plain_text", "text": truncate(title or _("Are you sure?"), 100)},
		"text": {"type": "mrkdwn", "text": truncate(text, 300)},
		"confirm": {"type": "plain_text", "text": truncate(confirm_label or _("Confirm"), 30)},
		"deny": {"type": "plain_text", "text": _("Cancel")},
	}


def actions(elements: list[dict]) -> list[dict]:
	"""Chunk buttons into actions blocks (Slack allows at most 5 elements each)."""
	blocks = []
	for i in range(0, len(elements), MAX_BUTTONS_PER_ACTIONS_BLOCK):
		blocks.append({"type": "actions", "elements": elements[i : i + MAX_BUTTONS_PER_ACTIONS_BLOCK]})
	return blocks


def clamp_blocks(blocks: list[dict]) -> list[dict]:
	if len(blocks) <= MAX_BLOCKS:
		return blocks

	kept = blocks[: MAX_BLOCKS - 1]
	kept.append(context(_("… truncated, {0} more blocks").format(len(blocks) - len(kept))))
	return kept


def validate_blocks(value: str, fieldname: str = "blocks") -> list[dict]:
	"""Parse and sanity-check a Block Kit JSON string; throws with a useful message."""
	if not value:
		return []

	try:
		parsed = json.loads(value)
	except ValueError as e:
		frappe.throw(_("{0} is not valid JSON: {1}").format(fieldname, e))

	# Accept both a bare list of blocks and a {"blocks": [...]} envelope.
	if isinstance(parsed, dict) and "blocks" in parsed:
		parsed = parsed["blocks"]

	if not isinstance(parsed, list):
		frappe.throw(_("{0} must be a JSON array of Block Kit blocks.").format(fieldname))

	for block in parsed:
		if not isinstance(block, dict) or not block.get("type"):
			frappe.throw(_("Every entry in {0} must be an object with a 'type'.").format(fieldname))

	if len(parsed) > MAX_BLOCKS:
		frappe.throw(_("{0} has {1} blocks; Slack allows at most {2}.").format(fieldname, len(parsed), MAX_BLOCKS))

	return parsed


def fallback_text(blocks: list[dict]) -> str:
	"""Plain-text summary for notifications and accessibility."""
	for block in blocks:
		if block.get("type") == "section":
			text = (block.get("text") or {}).get("text")
			if text:
				return truncate(text, 300)

	for block in blocks:
		if block.get("type") == "header":
			text = (block.get("text") or {}).get("text")
			if text:
				return truncate(text, 300)

	return _("Notification from your ERP")
