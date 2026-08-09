# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

import frappe
from frappe.utils import set_request

from slack_bridge.api import commands, interactive
from slack_bridge.slack.verify import compute_signature
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	SIGNING_SECRET,
	WORKSPACE,
	ensure_channel,
	ensure_user,
	ensure_workspace,
	fake_slack,
)

SLACK_USER_ID = "U0DYNFORM"
MAPPED_USER = "dynform-user@example.com"
FORM_TITLE = "SB Dynamic Test Form"

# ------------------------------------------------------------- fake provider
# Referenced by dotted path from Slack Dynamic Form records — this is exactly how
# a real app plugs in, so the tests double as a contract reference implementation.

calls: dict[str, list] = {"options": [], "schema": [], "submit": []}

FIELDS = [
	{"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": 1, "default": "Hello"},
	{"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
	{
		"fieldname": "priority",
		"label": "Priority",
		"fieldtype": "Select",
		"options": "Low\nMedium\nHigh",
		"default": "Medium",
	},
	{"fieldname": "mode", "label": "Mode", "fieldtype": "Radio", "options": "Fast\nThorough"},
	{"fieldname": "urgent", "label": "Urgent", "fieldtype": "Check", "default": 1},
	# Link target is a slack_bridge-owned doctype: real sites customize stock doctypes'
	# permissions (the dev site strips ToDo from standard roles), our own stay predictable.
	{"fieldname": "ref", "label": "Reference", "fieldtype": "Link", "options": "Slack Channel"},
	{"fieldname": "due", "label": "Due", "fieldtype": "Date", "description": "When it is needed"},
]


def fake_options(query, context):
	calls["options"].append({"query": query, "context": context, "session_user": frappe.session.user})
	choices = [{"value": "proc-a", "label": "Process Alpha"}, {"value": "proc-b", "label": "Process Beta"}]
	return [c for c in choices if query.lower() in c["label"].lower()]


def fake_schema(key, context):
	calls["schema"].append({"key": key, "session_user": frappe.session.user})
	return {"title": "Start Alpha", "submit_label": "Start", "fields": FIELDS}


def fake_submit(key, values, context):
	calls["submit"].append({"key": key, "values": values, "session_user": frappe.session.user})
	return f"Started {key}"


def failing_submit(key, values, context):
	calls["submit"].append({"key": key, "values": values})
	frappe.throw("Required form field(s) missing: Subject", frappe.ValidationError)


# ----------------------------------------------------------------- fixtures


def ensure_mapped_user():
	ensure_user(MAPPED_USER)
	user = frappe.get_doc("User", MAPPED_USER)
	if "System Manager" not in frappe.get_roles(MAPPED_USER):
		user.add_roles("System Manager")

	name = f"{WORKSPACE}-{SLACK_USER_ID}"
	if not frappe.db.exists("Slack User", name):
		frappe.get_doc(
			{
				"doctype": "Slack User",
				"workspace": WORKSPACE,
				"user": MAPPED_USER,
				"slack_user_id": SLACK_USER_ID,
				"mapping_source": "Manual",
				"allow_actions": 1,
			}
		).insert(ignore_permissions=True)


def ensure_dynamic_form(**overrides):
	if frappe.db.exists("Slack Dynamic Form", FORM_TITLE):
		frappe.delete_doc("Slack Dynamic Form", FORM_TITLE, force=True, ignore_permissions=True)

	values = {
		"doctype": "Slack Dynamic Form",
		"title": FORM_TITLE,
		"enabled": 1,
		"workspace": WORKSPACE,
		"modal_title": "Start a process",
		"picker_label": "Process",
		"min_query_length": 0,
		"options_method": "slack_bridge.tests.test_dynamic_forms.fake_options",
		"schema_method": "slack_bridge.tests.test_dynamic_forms.fake_schema",
		"submit_method": "slack_bridge.tests.test_dynamic_forms.fake_submit",
	}
	values.update(overrides)

	form = frappe.get_doc(values)
	form.insert(ignore_permissions=True)
	return form


def ensure_route(form_name: str):
	existing = frappe.get_all(
		"Slack Command Route",
		filters={"workspace": WORKSPACE, "command": "/erp", "subcommand": "dynform"},
		pluck="name",
	)
	for name in existing:
		frappe.delete_doc("Slack Command Route", name, force=True, ignore_permissions=True)

	return frappe.get_doc(
		{
			"doctype": "Slack Command Route",
			"command": "/erp",
			"subcommand": "dynform",
			"enabled": 1,
			"workspace": WORKSPACE,
			"route_type": "Open Dynamic Form",
			"dynamic_form": form_name,
		}
	).insert(ignore_permissions=True)


class DynamicFormTestCase(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workspace = ensure_workspace()
		ensure_mapped_user()

	def setUp(self):
		for bucket in calls.values():
			bucket.clear()

	def sign_request(self, path: str, body: bytes) -> None:
		timestamp = str(int(time.time()))
		set_request(
			method="POST",
			path=f"/api/method/{path}?token={self.workspace.endpoint_token}",
			data=body,
			headers={
				"X-Slack-Request-Timestamp": timestamp,
				"X-Slack-Signature": compute_signature(SIGNING_SECRET, timestamp, body),
			},
		)
		frappe.local.request_ip = "127.0.0.1"
		frappe.form_dict.token = self.workspace.endpoint_token

	def post_command(self, text: str, user_id: str = SLACK_USER_ID):
		fields = {
			"command": "/erp",
			"text": text,
			"user_id": user_id,
			"channel_id": "C0DYNFORM",
			"channel_name": "general",
			"trigger_id": f"123.{frappe.generate_hash(length=8)}.abc",
			"response_url": "https://hooks.slack.com/commands/T0TEST/1/2",
		}
		body = urlencode(fields).encode()
		self.sign_request("slack_bridge.api.commands.handle", body)
		frappe.form_dict.update(fields)
		commands.handle()

	def post_payload(self, payload: dict):
		body = urlencode({"payload": json.dumps(payload)}).encode()
		self.sign_request("slack_bridge.api.interactive.handle", body)
		frappe.form_dict.payload = json.dumps(payload)
		interactive.handle()

	def pick_payload(self, form_name: str = FORM_TITLE, key: str = "proc-a", user_id: str = SLACK_USER_ID):
		return {
			"type": "view_submission",
			"user": {"id": user_id},
			"view": {
				"id": f"V{frappe.generate_hash(length=8)}",
				"hash": frappe.generate_hash(length=10),
				"callback_id": "sb_dyn_pick",
				"private_metadata": json.dumps(
					{"kind": "sbdf", "form": form_name, "channel": "C0DYNFORM", "args": ""}
				),
				"blocks": [{"block_id": "df_picker"}],
				"state": {
					"values": {
						"df_picker": {"sbdf_options": {"selected_option": {"value": key} if key else None}}
					}
				},
			},
		}

	def form_payload(
		self,
		state_values: dict,
		form_name: str = FORM_TITLE,
		view_id: str | None = None,
		view_hash: str | None = None,
	):
		return {
			"type": "view_submission",
			"user": {"id": SLACK_USER_ID},
			"view": {
				"id": view_id or f"V{frappe.generate_hash(length=8)}",
				"hash": view_hash or frappe.generate_hash(length=10),
				"callback_id": "sb_dyn_form",
				"private_metadata": json.dumps(
					{"kind": "sbdf", "form": form_name, "channel": "C0DYNFORM", "args": "", "key": "proc-a"}
				),
				"blocks": [{"block_id": f"df_{i}_{f['fieldname']}"} for i, f in enumerate(FIELDS)],
				"state": {"values": state_values},
			},
		}

	def filled_state(self):
		return {
			"df_0_subject": {"value": {"value": "Order parts"}},
			"df_1_notes": {"value": {"value": "ASAP please"}},
			"df_2_priority": {"value": {"selected_option": {"value": "High"}}},
			"df_3_mode": {"value": {"selected_option": {"value": "Fast"}}},
			"df_4_urgent": {"value": {"selected_options": [{"value": "1"}]}},
			"df_5_ref": {"sbdf_link": {"selected_option": {"value": "TODO-0001"}}},
			"df_6_due": {"value": {"selected_date": "2026-09-01"}},
		}


class TestDynamicFormConfig(DynamicFormTestCase):
	def test_unresolvable_method_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_dynamic_form(submit_method="no.such.module.fn")

	def test_long_modal_title_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_dynamic_form(modal_title="This title is much longer than Slack allows")


class TestPickerFlow(DynamicFormTestCase):
	def test_command_opens_picker_modal(self):
		form = ensure_dynamic_form()
		ensure_route(form.name)

		with fake_slack() as mocked:
			self.post_command("dynform")

		call = mocked.call_args
		self.assertIn("views.open", call.args[0])
		view = json.loads(call.kwargs["data"])["view"]
		self.assertEqual(view["callback_id"], "sb_dyn_pick")
		element = view["blocks"][0]["element"]
		self.assertEqual(element["type"], "external_select")
		self.assertEqual(element["action_id"], "sbdf_options")
		metadata = json.loads(view["private_metadata"])
		self.assertEqual(metadata["form"], form.name)
		self.assertEqual(metadata["channel"], "C0DYNFORM")

	def test_unmapped_user_gets_link_prompt(self):
		form = ensure_dynamic_form()
		ensure_route(form.name)

		with fake_slack() as mocked:
			self.post_command("dynform", user_id="U0NOBODY")

		self.assertFalse(mocked.called)
		self.assertIn("not linked", frappe.local.response.get("text") or "")

	def test_required_role_gates_the_picker(self):
		role = "SBDF Gate Role"
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)

		form = ensure_dynamic_form(required_role=role)
		ensure_route(form.name)

		with fake_slack() as mocked:
			self.post_command("dynform")

		self.assertFalse(mocked.called)
		self.assertIn("role", frappe.local.response.get("text") or "")

	def test_picker_suggestions_come_from_provider_as_mapped_user(self):
		form = ensure_dynamic_form()

		self.post_payload(
			{
				"type": "block_suggestion",
				"action_id": "sbdf_options",
				"block_id": "df_picker",
				"value": "beta",
				"user": {"id": SLACK_USER_ID},
				"view": {
					"private_metadata": json.dumps(
						{"kind": "sbdf", "form": form.name, "channel": "C0DYNFORM"}
					)
				},
			}
		)

		options = frappe.local.response.get("options") or []
		self.assertEqual([o["value"] for o in options], ["proc-b"])
		self.assertEqual(calls["options"][0]["query"], "beta")
		self.assertEqual(calls["options"][0]["session_user"], MAPPED_USER)
		self.assertEqual(calls["options"][0]["context"]["user"], MAPPED_USER)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_unknown_form_returns_empty_options(self):
		self.post_payload(
			{
				"type": "block_suggestion",
				"action_id": "sbdf_options",
				"value": "x",
				"user": {"id": SLACK_USER_ID},
				"view": {"private_metadata": json.dumps({"kind": "sbdf", "form": "Nope"})},
			}
		)

		self.assertEqual(frappe.local.response.get("options"), [])


class TestStageTransition(DynamicFormTestCase):
	def test_pick_submission_updates_to_form_view(self):
		form = ensure_dynamic_form()
		logs_before = frappe.db.count("Slack Interaction Log")

		self.post_payload(self.pick_payload(form.name))

		self.assertEqual(frappe.local.response.get("response_action"), "update")
		view = frappe.local.response.get("view")
		self.assertEqual(view["callback_id"], "sb_dyn_form")
		self.assertEqual(view["title"]["text"], "Start Alpha")
		self.assertEqual(view["submit"]["text"], "Start")
		self.assertEqual(json.loads(view["private_metadata"])["key"], "proc-a")

		types = [b["element"]["type"] for b in view["blocks"]]
		self.assertEqual(
			types,
			[
				"plain_text_input",
				"plain_text_input",
				"static_select",
				"radio_buttons",
				"checkboxes",
				"external_select",
				"datepicker",
			],
		)
		self.assertTrue(view["blocks"][1]["element"].get("multiline"))
		self.assertEqual(view["blocks"][0]["element"]["initial_value"], "Hello")
		self.assertEqual(view["blocks"][2]["element"]["initial_option"]["value"], "Medium")
		self.assertTrue(view["blocks"][4]["element"].get("initial_options"))
		self.assertEqual(view["blocks"][5]["element"]["action_id"], "sbdf_link")
		self.assertEqual(view["blocks"][6]["hint"]["text"], "When it is needed")
		self.assertFalse(view["blocks"][0]["optional"])
		self.assertTrue(view["blocks"][1]["optional"])

		# Rendering the next view is read-only, so it must not burn an idempotency claim.
		self.assertEqual(frappe.db.count("Slack Interaction Log"), logs_before)

	def test_pick_without_selection_errors_on_picker_block(self):
		ensure_dynamic_form()

		self.post_payload(self.pick_payload(key=None))

		self.assertEqual(frappe.local.response.get("response_action"), "errors")
		self.assertIn("df_picker", frappe.local.response.get("errors") or {})

	def test_link_field_suggestions_search_target_doctype(self):
		form = ensure_dynamic_form()
		channel = ensure_channel()

		# Search by docname — title fields vary per site, names never do.
		self.post_payload(
			{
				"type": "block_suggestion",
				"action_id": "sbdf_link",
				"block_id": "df_5_ref",
				"value": channel,
				"user": {"id": SLACK_USER_ID},
				"view": {
					"private_metadata": json.dumps(
						{"kind": "sbdf", "form": form.name, "channel": "C0DYNFORM", "key": "proc-a"}
					)
				},
			}
		)

		options = frappe.local.response.get("options") or []
		self.assertIn(channel, [o["value"] for o in options])
		self.assertEqual(calls["schema"][0]["key"], "proc-a")


class TestFormSubmission(DynamicFormTestCase):
	def test_submit_calls_provider_with_extracted_values(self):
		ensure_dynamic_form()

		with fake_slack() as mocked:
			self.post_payload(self.form_payload(self.filled_state()))

		self.assertEqual(frappe.local.response.get("response_action"), "clear")
		submitted = calls["submit"][0]
		self.assertEqual(submitted["key"], "proc-a")
		self.assertEqual(submitted["session_user"], MAPPED_USER)
		self.assertEqual(
			submitted["values"],
			{
				"subject": "Order parts",
				"notes": "ASAP please",
				"priority": "High",
				"mode": "Fast",
				"urgent": 1,
				"ref": "TODO-0001",
				"due": "2026-09-01",
			},
		)
		self.assertEqual(frappe.session.user, "Administrator")

		# Confirmation lands as an ephemeral in the origin channel.
		self.assertTrue(any("chat.postEphemeral" in c.args[0] for c in mocked.call_args_list))

	def test_empty_optional_fields_reach_provider_as_empty_strings(self):
		ensure_dynamic_form()
		state = {"df_0_subject": {"value": {"value": "Only subject"}}}

		with fake_slack():
			self.post_payload(self.form_payload(state))

		values = calls["submit"][0]["values"]
		self.assertEqual(values["subject"], "Only subject")
		self.assertEqual(values["notes"], "")
		self.assertEqual(values["ref"], "")
		self.assertEqual(values["urgent"], 0)

	def test_validation_error_maps_to_first_block(self):
		ensure_dynamic_form(submit_method="slack_bridge.tests.test_dynamic_forms.failing_submit")

		with fake_slack():
			self.post_payload(self.form_payload(self.filled_state()))

		self.assertEqual(frappe.local.response.get("response_action"), "errors")
		errors = frappe.local.response.get("errors") or {}
		self.assertIn("df_0_subject", errors)
		self.assertIn("Subject", errors["df_0_subject"])

	def test_duplicate_submission_runs_provider_once(self):
		ensure_dynamic_form()
		# A fresh id per run: claims persist in the Interaction Log across test runs.
		payload = self.form_payload(
			self.filled_state(),
			view_id=f"V{frappe.generate_hash(length=8)}",
			view_hash=frappe.generate_hash(length=10),
		)

		with fake_slack():
			self.post_payload(payload)
			self.post_payload(payload)

		self.assertEqual(len(calls["submit"]), 1)
		self.assertEqual(frappe.local.response.get("response_action"), "clear")

	def test_background_submit_enqueues_instead_of_running_inline(self):
		ensure_dynamic_form(submit_in_background=1)

		with fake_slack(), patch("frappe.enqueue") as enqueue:
			self.post_payload(self.form_payload(self.filled_state()))

		self.assertEqual(frappe.local.response.get("response_action"), "clear")
		self.assertFalse(calls["submit"])
		job = next(c for c in enqueue.call_args_list if "execute_submit" in str(c.args))
		self.assertEqual(job.kwargs["key"], "proc-a")
		self.assertEqual(job.kwargs["user"], MAPPED_USER)

	def test_disabled_form_is_rejected(self):
		ensure_dynamic_form(enabled=0)

		with fake_slack():
			self.post_payload(self.form_payload(self.filled_state()))

		self.assertEqual(frappe.local.response.get("response_action"), "errors")
		self.assertFalse(calls["submit"])
