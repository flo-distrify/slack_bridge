# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json

import frappe

from slack_bridge.slack.manifest import build_manifest
from slack_bridge.tests.fixtures import CHANNEL_ID, WORKSPACE, ensure_workspace, fake_slack
from slack_bridge.tests.test_comm_log import SLACK_USER_ID, CommLogTestCase, ensure_slack_user

FORM_TITLE = "SB Shortcut ToDo Form"
SHORTCUT_CALLBACK = "sb_form_todo_test"


def ensure_shortcut_form(**overrides):
	values = {
		"doctype": "Slack Form",
		"title": FORM_TITLE,
		"enabled": 1,
		"document_type": "ToDo",
		"mode": "Create",
		"modal_title": "New ToDo",
		"fields": [
			{"fieldname": "description", "label": "Text", "input_type": "Long Text", "reqd": 1},
			{"fieldname": "allocated_to", "label": "Assignee", "input_type": "User"},
		],
		"message_shortcut": 1,
		"shortcut_label": "Create ToDo",
		"shortcut_description": "Turn this message into a ToDo",
		"shortcut_callback_id": SHORTCUT_CALLBACK,
		"shortcut_prefill_field": "description",
	}
	values.update(overrides)

	existing = frappe.db.get_value("Slack Form", {"title": values["title"]})
	if existing:
		frappe.delete_doc("Slack Form", existing, force=True, ignore_permissions=True)

	form = frappe.get_doc(values)
	form.insert(ignore_permissions=True)
	return form


def shortcut_payload(text="Order <https://example.com|cables> &amp; screws", user_id=SLACK_USER_ID):
	return {
		"type": "message_action",
		"callback_id": SHORTCUT_CALLBACK,
		"action_ts": f"1700{frappe.generate_hash(length=9)}",
		"trigger_id": "123.456.abc",
		"response_url": "https://hooks.slack.com/actions/T0TEST/1/2",
		"user": {"id": user_id},
		"channel": {"id": CHANNEL_ID},
		"message": {"ts": "1700000002.000100", "text": text},
	}


class TestShortcutFormConfig(CommLogTestCase):
	def test_long_label_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut_form(shortcut_label="This label is far too long for a Slack menu")

	def test_callback_collision_with_comm_shortcut_is_rejected(self):
		from slack_bridge.tests.test_comm_log import CALLBACK_ID, ensure_shortcut

		ensure_shortcut()
		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut_form(shortcut_callback_id=CALLBACK_ID)

	def test_prefill_field_must_be_a_text_input(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut_form(shortcut_prefill_field="allocated_to")

		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut_form(shortcut_prefill_field="no_such_field")

	def test_manifest_lists_the_form_shortcut(self):
		ensure_shortcut_form()
		manifest = build_manifest(ensure_workspace())

		shortcuts = manifest["features"].get("shortcuts") or []
		entry = next((s for s in shortcuts if s["callback_id"] == SHORTCUT_CALLBACK), None)
		self.assertTrue(entry)
		self.assertEqual(entry["name"], "Create ToDo")
		self.assertEqual(entry["type"], "message")

	def test_disabled_form_shortcut_is_absent(self):
		ensure_shortcut_form(enabled=0)
		manifest = build_manifest(ensure_workspace())

		shortcuts = manifest["features"].get("shortcuts") or []
		self.assertFalse(any(s["callback_id"] == SHORTCUT_CALLBACK for s in shortcuts))


class TestShortcutFlow(CommLogTestCase):
	def test_shortcut_opens_prefilled_modal(self):
		ensure_slack_user()
		ensure_shortcut_form()

		with fake_slack() as mocked:
			self.post_payload(shortcut_payload())

		call = next(c for c in mocked.call_args_list if "views.open" in c.args[0])
		view = json.loads(call.kwargs["data"])["view"]

		self.assertEqual(view["callback_id"], "sb_form")
		self.assertEqual(json.loads(view["private_metadata"])["form"], FORM_TITLE)

		text_block = view["blocks"][0]
		self.assertEqual(text_block["element"]["type"], "plain_text_input")
		# Slack link markup and HTML entities are unwrapped before prefilling.
		self.assertEqual(
			text_block["element"]["initial_value"], "Order cables (https://example.com) & screws"
		)

		assignee_block = view["blocks"][1]
		self.assertEqual(assignee_block["element"]["type"], "users_select")

	def test_unmapped_user_is_pointed_to_linking(self):
		ensure_shortcut_form()

		with fake_slack() as mocked:
			self.post_payload(shortcut_payload(user_id="U0NOBODY99"))

		self.assertFalse(any("views.open" in c.args[0] for c in mocked.call_args_list))

	def test_same_invocation_is_idempotent(self):
		ensure_slack_user()
		ensure_shortcut_form()
		payload = shortcut_payload()

		with fake_slack() as mocked:
			self.post_payload(payload)
			self.post_payload(payload)

		opens = [c for c in mocked.call_args_list if "views.open" in c.args[0]]
		self.assertEqual(len(opens), 1)

	def test_submission_creates_the_document(self):
		# Note instead of ToDo: the suite runs on real sites, and a site can add its own
		# mandatory custom fields to ToDo (the dev site does) that bare CI lacks.
		ensure_slack_user()
		form = ensure_shortcut_form(
			title=f"{FORM_TITLE} Note",
			document_type="Note",
			shortcut_callback_id=f"{SHORTCUT_CALLBACK}_note",
			fields=[
				{"fieldname": "title", "label": "Title", "input_type": "Text", "reqd": 1},
				{"fieldname": "content", "label": "Text", "input_type": "Long Text"},
			],
			shortcut_prefill_field="content",
		)
		marker = f"SBFS {frappe.generate_hash(length=8)}"

		payload = {
			"type": "view_submission",
			"user": {"id": SLACK_USER_ID},
			"view": {
				"id": f"V{frappe.generate_hash(length=8)}",
				"hash": frappe.generate_hash(length=10),
				"callback_id": "sb_form",
				"private_metadata": json.dumps({"form": form.name, "channel": CHANNEL_ID}),
				"blocks": [{"block_id": "f_0_title"}, {"block_id": "f_1_content"}],
				"state": {
					"values": {
						"f_0_title": {"value": {"value": marker}},
						"f_1_content": {"value": {"value": "From the Slack message"}},
					}
				},
			},
		}

		with fake_slack():
			self.post_payload(payload)

		self.assertEqual(frappe.local.response.get("response_action"), "clear")
		self.assertTrue(frappe.db.exists("Note", {"title": marker}))
