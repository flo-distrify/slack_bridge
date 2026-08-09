# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json
import time
from urllib.parse import urlencode

import frappe
from frappe.utils import set_request

from slack_bridge.api import actions, base, commands, events, interactive
from slack_bridge.slack.manifest import build_manifest
from slack_bridge.slack.verify import compute_signature
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	CHANNEL_ID,
	SIGNING_SECRET,
	WORKSPACE,
	ensure_channel,
	ensure_workspace,
	fake_slack,
	make_todo,
)

SLACK_USER_ID = "U0TESTUSER"


def slack_request(body: bytes, workspace, sign: bool = True, timestamp: str | None = None):
	"""Build a signed Slack request exactly as Slack would send it."""
	timestamp = timestamp or str(int(time.time()))
	signature = compute_signature(
		SIGNING_SECRET if sign else "wrong-secret", timestamp, body
	)

	set_request(
		method="POST",
		path=f"/api/method/slack_bridge.api.events.handle?token={workspace.endpoint_token}",
		data=body,
		headers={
			"X-Slack-Request-Timestamp": timestamp,
			"X-Slack-Signature": signature,
		},
	)

	# The real request pipeline sets this; the rate limiter needs it.
	frappe.local.request_ip = "127.0.0.1"
	frappe.form_dict.token = workspace.endpoint_token
	return body


def ensure_slack_user(user: str, allow_actions: int = 1):
	name = f"{WORKSPACE}-{SLACK_USER_ID}"
	if frappe.db.exists("Slack User", name):
		doc = frappe.get_doc("Slack User", name)
		doc.user = user
		doc.allow_actions = allow_actions
		doc.enabled = 1
		doc.save(ignore_permissions=True)
		return doc

	return frappe.get_doc(
		{
			"doctype": "Slack User",
			"workspace": WORKSPACE,
			"user": user,
			"slack_user_id": SLACK_USER_ID,
			"mapping_source": "Manual",
			"allow_actions": allow_actions,
		}
	).insert(ignore_permissions=True)


class TestManifest(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workspace = ensure_workspace()

	def test_manifest_has_required_sections(self):
		manifest = build_manifest(self.workspace)

		self.assertIn("display_information", manifest)
		self.assertIn("oauth_config", manifest)
		self.assertFalse(manifest["settings"]["socket_mode_enabled"])

	def test_manifest_requests_the_scopes_we_use(self):
		scopes = build_manifest(self.workspace)["oauth_config"]["scopes"]["bot"]

		for required in ("chat:write", "commands", "users:read.email", "links:write"):
			self.assertIn(required, scopes)

	def test_request_urls_carry_the_endpoint_token(self):
		settings = build_manifest(self.workspace)["settings"]

		self.assertIn(self.workspace.endpoint_token, settings["event_subscriptions"]["request_url"])
		self.assertIn(self.workspace.endpoint_token, settings["interactivity"]["request_url"])

	def test_endpoint_token_is_generated_and_unguessable(self):
		self.assertTrue(self.workspace.endpoint_token)
		self.assertGreaterEqual(len(self.workspace.endpoint_token), 32)


class TestIdempotency(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()

	def test_second_claim_on_same_key_loses(self):
		key = base.idempotency_key("ts", "action", "user", frappe.generate_hash())

		self.assertIsNotNone(base.claim(key, "Interactive", WORKSPACE))
		self.assertIsNone(base.claim(key, "Interactive", WORKSPACE))

	def test_different_keys_both_win(self):
		suffix = frappe.generate_hash()
		self.assertIsNotNone(base.claim(base.idempotency_key("a", suffix), "Event", WORKSPACE))
		self.assertIsNotNone(base.claim(base.idempotency_key("b", suffix), "Event", WORKSPACE))

	def test_key_depends_on_every_part(self):
		self.assertNotEqual(
			base.idempotency_key("ts1", "approve", "U1"),
			base.idempotency_key("ts1", "approve", "U2"),
		)


class TestEventsEndpoint(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workspace = ensure_workspace()

	def test_url_verification_returns_the_challenge(self):
		body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
		slack_request(body, self.workspace)

		events.handle()

		self.assertEqual(frappe.local.response.get("challenge"), "abc123")

	def test_bad_signature_is_rejected(self):
		body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
		slack_request(body, self.workspace, sign=False)

		events.handle()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		self.assertIsNone(frappe.local.response.get("challenge"))

	def test_stale_timestamp_is_rejected(self):
		body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
		stale = str(int(time.time()) - 3600)
		slack_request(body, self.workspace, timestamp=stale)

		events.handle()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)

	def test_unknown_token_is_rejected(self):
		body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
		slack_request(body, self.workspace)
		frappe.form_dict.token = "not-a-real-token"

		events.handle()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)

	def test_duplicate_event_delivery_is_claimed_once(self):
		event_id = f"Ev{frappe.generate_hash(length=10)}"
		payload = {
			"type": "event_callback",
			"event_id": event_id,
			"event": {"type": "link_shared", "user": SLACK_USER_ID, "event_ts": "1700000000.1"},
		}
		body = json.dumps(payload).encode()

		slack_request(body, self.workspace)
		events.handle()

		count_before = frappe.db.count("Slack Interaction Log", {"kind": "Event"})

		# Slack redelivers on a slow ack; the second delivery must not be processed.
		slack_request(body, self.workspace)
		events.handle()

		self.assertEqual(frappe.db.count("Slack Interaction Log", {"kind": "Event"}), count_before)


class TestActionExecution(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel()

	def make_action(self, title: str, **overrides):
		if frappe.db.exists("Slack Action", title):
			frappe.delete_doc("Slack Action", title, force=True, ignore_permissions=True)

		values = {
			"doctype": "Slack Action",
			"title": title,
			"enabled": 1,
			"document_type": "ToDo",
			"action_type": "Set Field Value",
			"field_to_set": "status",
			"value_to_set": "Closed",
			"update_original_message": 0,
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def test_action_updates_the_document(self):
		action = self.make_action("SB Test Close")
		todo = make_todo()

		with fake_slack():
			actions.run(
				workspace=WORKSPACE,
				action_name=action.name,
				doctype="ToDo",
				docname=todo.name,
				user="Administrator",
				slack_user_id=SLACK_USER_ID,
			)

		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "status"), "Closed")

	def test_action_renders_jinja_values(self):
		action = self.make_action(
			"SB Test Jinja", field_to_set="description", value_to_set="Handled: {{ doc.name }}"
		)
		todo = make_todo()

		with fake_slack():
			actions.run(
				workspace=WORKSPACE,
				action_name=action.name,
				doctype="ToDo",
				docname=todo.name,
				user="Administrator",
				slack_user_id=SLACK_USER_ID,
			)

		self.assertIn(todo.name, frappe.db.get_value("ToDo", todo.name, "description"))

	def test_missing_document_is_reported_not_raised(self):
		action = self.make_action("SB Test Missing")
		log = base.claim(base.idempotency_key(frappe.generate_hash()), "Interactive", WORKSPACE)

		with fake_slack():
			# Must not raise into the worker.
			actions.run(
				workspace=WORKSPACE,
				action_name=action.name,
				doctype="ToDo",
				docname="no-such-todo",
				user="Administrator",
				slack_user_id=SLACK_USER_ID,
				log_name=log,
			)

		self.assertEqual(frappe.db.get_value("Slack Interaction Log", log, "status"), "Failed")

	def test_required_role_is_enforced(self):
		action = self.make_action("SB Test Role", required_role="System Manager")
		todo = make_todo()
		user = create_limited_user()
		log = base.claim(base.idempotency_key(frappe.generate_hash()), "Interactive", WORKSPACE)

		with fake_slack():
			actions.run(
				workspace=WORKSPACE,
				action_name=action.name,
				doctype="ToDo",
				docname=todo.name,
				user=user,
				slack_user_id=SLACK_USER_ID,
				log_name=log,
			)

		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "status"), "Open")
		self.assertEqual(frappe.db.get_value("Slack Interaction Log", log, "status"), "Failed")

	def test_session_user_is_restored_after_running(self):
		action = self.make_action("SB Test Session")
		todo = make_todo()
		before = frappe.session.user

		with fake_slack():
			actions.run(
				workspace=WORKSPACE,
				action_name=action.name,
				doctype="ToDo",
				docname=todo.name,
				user="Administrator",
				slack_user_id=SLACK_USER_ID,
			)

		self.assertEqual(frappe.session.user, before)


class TestInteractiveEndpoint(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workspace = ensure_workspace()
		ensure_channel()

	def post_payload(self, payload: dict):
		body = urlencode({"payload": json.dumps(payload)}).encode()
		timestamp = str(int(time.time()))

		set_request(
			method="POST",
			path=f"/api/method/slack_bridge.api.interactive.handle?token={self.workspace.endpoint_token}",
			data=body,
			headers={
				"X-Slack-Request-Timestamp": timestamp,
				"X-Slack-Signature": compute_signature(SIGNING_SECRET, timestamp, body),
			},
		)
		frappe.local.request_ip = "127.0.0.1"
		frappe.form_dict.token = self.workspace.endpoint_token
		frappe.form_dict.payload = json.dumps(payload)

		interactive.handle()

	def test_unlinked_user_is_told_to_link(self):
		frappe.db.delete("Slack User", {"slack_user_id": SLACK_USER_ID})
		todo = make_todo()

		self.post_payload(
			{
				"type": "block_actions",
				"user": {"id": SLACK_USER_ID},
				"message": {"ts": f"170000{frappe.generate_hash(length=6)}"},
				"channel": {"id": CHANNEL_ID},
				"actions": [
					{
						"action_id": "sb_action_0",
						"value": json.dumps(
							{"action": "SB Test Close", "doctype": "ToDo", "docname": todo.name}
						),
					}
				],
			}
		)

		self.assertEqual(frappe.local.response.get("response_type"), "ephemeral")
		self.assertIn("not linked", frappe.local.response.get("text", "").lower())

	def test_bad_signature_is_rejected(self):
		body = urlencode({"payload": "{}"}).encode()
		timestamp = str(int(time.time()))

		set_request(
			method="POST",
			path=f"/api/method/slack_bridge.api.interactive.handle?token={self.workspace.endpoint_token}",
			data=body,
			headers={
				"X-Slack-Request-Timestamp": timestamp,
				"X-Slack-Signature": compute_signature("wrong", timestamp, body),
			},
		)
		frappe.local.request_ip = "127.0.0.1"
		frappe.form_dict.token = self.workspace.endpoint_token

		interactive.handle()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)


class TestFormValueExtraction(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()

	def make_form(self):
		title = "SB Test Form"
		if frappe.db.exists("Slack Form", title):
			frappe.delete_doc("Slack Form", title, force=True, ignore_permissions=True)

		return frappe.get_doc(
			{
				"doctype": "Slack Form",
				"title": title,
				"document_type": "ToDo",
				"mode": "Create",
				"fields": [
					{"fieldname": "description", "input_type": "Long Text", "reqd": 1},
					{"fieldname": "priority", "input_type": "Select", "options": "Low\nMedium\nHigh"},
					{"fieldname": "date", "input_type": "Date"},
				],
			}
		).insert(ignore_permissions=True)

	def test_builds_one_input_block_per_field(self):
		from slack_bridge.api.forms import build_view

		form = self.make_form()
		view = build_view(form, private_metadata={"channel": CHANNEL_ID})

		self.assertEqual(view["type"], "modal")
		self.assertEqual(len(view["blocks"]), 3)
		self.assertTrue(all(b["type"] == "input" for b in view["blocks"]))

	def test_required_fields_are_not_optional(self):
		from slack_bridge.api.forms import build_view

		view = build_view(self.make_form())
		self.assertFalse(view["blocks"][0]["optional"])
		self.assertTrue(view["blocks"][1]["optional"])

	def test_extracts_each_input_type(self):
		from slack_bridge.api.forms import extract_values

		form = self.make_form()
		view = {
			"state": {
				"values": {
					"f_0_description": {"value": {"value": "Pack the crate"}},
					"f_1_priority": {"value": {"selected_option": {"value": "High"}}},
					"f_2_date": {"value": {"selected_date": "2026-08-05"}},
				}
			}
		}

		values = extract_values(form, view)

		self.assertEqual(values["description"], "Pack the crate")
		self.assertEqual(values["priority"], "High")
		self.assertEqual(values["date"], "2026-08-05")

	def test_modal_title_respects_slack_limit(self):
		form = self.make_form()
		form.modal_title = None
		self.assertLessEqual(len(form.get_modal_title()), 24)


def create_limited_user() -> str:
	email = "slack-bridge-limited@example.com"

	# A dedicated no-permission role: "Blogger" (used previously) was removed
	# from frappe core in v16, so the fixture provides its own.
	if not frappe.db.exists("Role", "Slack Bridge Limited"):
		frappe.get_doc({"doctype": "Role", "role_name": "Slack Bridge Limited"}).insert(
			ignore_permissions=True
		)

	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Limited",
				"send_welcome_email": 0,
				"roles": [{"role": "Slack Bridge Limited"}],
			}
		)
		user.insert(ignore_permissions=True)

	return email


class TestProtocolResponses(SlackBridgeTestCase):
	def test_respond_drops_queued_server_messages(self):
		# A failing Notification or a msgprint in a document hook queues messages that
		# Frappe appends to the response body — Slack rejects the unexpected key.
		frappe.msgprint("Failed to send Notification", raise_exception=False)
		self.assertTrue(frappe.local.message_log)

		base.respond({"response_action": "clear"})

		self.assertFalse(frappe.local.message_log)
		self.assertEqual(frappe.local.response.get("response_action"), "clear")
