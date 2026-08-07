# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

import frappe
from frappe.utils import set_request

from slack_bridge.api import comm_log, interactive
from slack_bridge.slack.manifest import BOT_SCOPES, build_manifest
from slack_bridge.slack.verify import compute_signature
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	CHANNEL_ID,
	SIGNING_SECRET,
	WORKSPACE,
	FakeResponse,
	ensure_channel,
	ensure_workspace,
	fake_slack,
	make_todo,
)

SLACK_USER_ID = "U0COMMLOG"
CALLBACK_ID = "sb_log_comm_test"


def ensure_slack_user(user: str = "Administrator", allow_actions: int = 1):
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


def ensure_shortcut(**overrides):
	existing = frappe.db.get_value("Slack Communication Shortcut", {"callback_id": CALLBACK_ID})
	if existing:
		frappe.delete_doc("Slack Communication Shortcut", existing, force=True, ignore_permissions=True)

	values = {
		"doctype": "Slack Communication Shortcut",
		"shortcut_label": "Log as Phone Call",
		"shortcut_description": "Save these notes to the ERP",
		"callback_id": CALLBACK_ID,
		"enabled": 1,
		"workspace": WORKSPACE,
		"communication_medium": "Phone",
		"default_direction": "Outbound",
		"default_subject": "Phone call",
		"collect_phone_no": 1,
		"min_query_length": 2,
		"search_limit": 8,
		"party_doctypes": [{"ref_doctype": "ToDo", "label": "Tasks", "search_fields": "description"}],
	}
	values.update(overrides)

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	return doc


def message_action_payload(workspace, text="Call mit Alexander\nWinbond Preise kommen", files=None):
	payload = {
		"type": "message_action",
		"callback_id": CALLBACK_ID,
		"action_ts": f"1700{frappe.generate_hash(length=9)}",
		"trigger_id": "123.456.abc",
		"response_url": "https://hooks.slack.com/actions/T0TEST/1/2",
		"user": {"id": SLACK_USER_ID},
		"channel": {"id": CHANNEL_ID},
		"message": {"ts": "1700000001.000100", "text": text},
	}
	if files is not None:
		payload["message"]["files"] = files
	return payload


class CommLogTestCase(SlackBridgeTestCase):
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


class TestShortcutConfig(CommLogTestCase):
	def test_label_length_is_enforced(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut(shortcut_label="This label is far too long for Slack")

	def test_unknown_search_field_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_shortcut(party_doctypes=[{"ref_doctype": "ToDo", "search_fields": "no_such_field"}])

	def test_search_limit_is_clamped(self):
		shortcut = ensure_shortcut(search_limit=99)
		self.assertEqual(shortcut.search_limit, 15)


class TestManifestShortcuts(CommLogTestCase):
	def test_manifest_lists_shortcut_and_scope(self):
		ensure_shortcut()
		manifest = build_manifest(self.workspace)

		self.assertIn("files:read", BOT_SCOPES)
		# Without the Options Load URL, external_select pickers silently stay empty.
		self.assertTrue(manifest["settings"]["interactivity"].get("message_menu_options_url"))
		shortcuts = manifest["features"].get("shortcuts") or []
		self.assertTrue(any(s["callback_id"] == CALLBACK_ID for s in shortcuts))
		self.assertTrue(all(s["type"] == "message" for s in shortcuts))

	def test_disabled_shortcut_is_absent(self):
		ensure_shortcut(enabled=0)
		manifest = build_manifest(self.workspace)
		shortcuts = manifest["features"].get("shortcuts") or []
		self.assertFalse(any(s["callback_id"] == CALLBACK_ID for s in shortcuts))


class TestMessageAction(CommLogTestCase):
	def test_modal_opens_with_prefilled_notes(self):
		ensure_shortcut()
		ensure_slack_user()

		with fake_slack() as post:
			self.post_payload(message_action_payload(self.workspace))

		views_open = [c for c in post.call_args_list if "views.open" in c.args[0]]
		self.assertEqual(len(views_open), 1)

		view = json.loads(views_open[0].kwargs["data"])["view"]
		self.assertEqual(view["callback_id"], comm_log.CALLBACK_ID)

		blocks = {b.get("block_id"): b for b in view["blocks"] if b.get("block_id")}
		self.assertEqual(blocks[comm_log.BLOCK_PARTY]["element"]["type"], "external_select")
		self.assertEqual(blocks[comm_log.BLOCK_PARTY]["element"]["min_query_length"], 2)
		self.assertIn("Call mit Alexander", blocks[comm_log.BLOCK_NOTES]["element"]["initial_value"])
		self.assertEqual(blocks[comm_log.BLOCK_SUBJECT]["element"]["initial_value"], "Call mit Alexander")

		metadata = json.loads(view["private_metadata"])
		self.assertTrue(metadata.get("cs"))
		self.assertLessEqual(len(view["private_metadata"]), 3000)

	def test_unmapped_user_gets_link_prompt_via_response_url(self):
		ensure_shortcut()
		frappe.db.delete("Slack User", {"slack_user_id": SLACK_USER_ID})

		with fake_slack() as post:
			self.post_payload(message_action_payload(self.workspace))

		urls = [c.args[0] for c in post.call_args_list]
		self.assertTrue(any("hooks.slack.com" in u for u in urls))
		self.assertFalse(any("views.open" in u for u in urls))

	def test_same_invocation_is_idempotent(self):
		ensure_shortcut()
		ensure_slack_user()
		payload = message_action_payload(self.workspace)

		with fake_slack() as post:
			self.post_payload(payload)
			self.post_payload(payload)

		views_open = [c for c in post.call_args_list if "views.open" in c.args[0]]
		self.assertEqual(len(views_open), 1)

	def test_voice_clip_transcript_prefills_notes(self):
		ensure_shortcut()
		ensure_slack_user()

		def slack_api(url, **kwargs):
			if "files.info" in url:
				return FakeResponse(
					{
						"ok": True,
						"file": {
							"id": "F0AUDIO",
							"transcription": {
								"status": "complete",
								"preview": {"content": "Alex ruft zurück wegen Winbond", "has_more": False},
							},
						},
					}
				)
			return FakeResponse({"ok": True})

		with patch("slack_bridge.slack.client.requests.post", side_effect=slack_api) as post:
			self.post_payload(
				message_action_payload(
					self.workspace, text="", files=[{"id": "F0AUDIO", "subtype": "slack_audio"}]
				)
			)

		views_open = [c for c in post.call_args_list if "views.open" in c.args[0]]
		view = json.loads(views_open[0].kwargs["data"])["view"]
		blocks = {b.get("block_id"): b for b in view["blocks"] if b.get("block_id")}
		self.assertIn("Winbond", blocks[comm_log.BLOCK_NOTES]["element"]["initial_value"])
		self.assertEqual(json.loads(view["private_metadata"]).get("file"), "F0AUDIO")

	def test_pending_transcript_warns_and_opens_empty(self):
		ensure_shortcut()
		ensure_slack_user()

		def slack_api(url, **kwargs):
			if "files.info" in url:
				return FakeResponse(
					{"ok": True, "file": {"id": "F0AUDIO", "transcription": {"status": "processing"}}}
				)
			return FakeResponse({"ok": True})

		with patch("slack_bridge.slack.client.requests.post", side_effect=slack_api) as post:
			self.post_payload(
				message_action_payload(
					self.workspace, text="", files=[{"id": "F0AUDIO", "subtype": "slack_audio"}]
				)
			)

		views_open = [c for c in post.call_args_list if "views.open" in c.args[0]]
		view = json.loads(views_open[0].kwargs["data"])["view"]
		blocks = {b.get("block_id"): b for b in view["blocks"] if b.get("block_id")}
		self.assertNotIn("initial_value", blocks[comm_log.BLOCK_NOTES]["element"])
		contexts = [b for b in view["blocks"] if b.get("type") == "context"]
		self.assertTrue(contexts)


class TestPartyOptions(CommLogTestCase):
	def test_options_are_grouped_and_index_encoded(self):
		shortcut = ensure_shortcut()
		ensure_slack_user()
		todo = make_todo("Findable by comm log picker")

		payload = {
			"type": "block_suggestion",
			"action_id": comm_log.PARTY_ACTION_ID,
			"value": "comm log picker",
			"user": {"id": SLACK_USER_ID},
			"view": {"private_metadata": json.dumps({"cs": shortcut.name})},
		}
		self.post_payload(payload)

		groups = frappe.local.response.get("option_groups") or []
		self.assertTrue(groups)
		self.assertEqual(groups[0]["label"]["text"], "Tasks")
		values = [o["value"] for g in groups for o in g["options"]]
		self.assertIn(f"0::{todo.name}", values)

	def test_wrong_action_id_returns_no_options(self):
		ensure_shortcut()
		ensure_slack_user()
		self.post_payload(
			{
				"type": "block_suggestion",
				"action_id": "something_else",
				"value": "x",
				"user": {"id": SLACK_USER_ID},
				"view": {"private_metadata": "{}"},
			}
		)
		self.assertEqual(frappe.local.response.get("options"), [])


class TestSubmission(CommLogTestCase):
	def submission_payload(self, shortcut, todo, direction="Inbound", notes="Er ruft Montag zurück."):
		return {
			"type": "view_submission",
			"user": {"id": SLACK_USER_ID},
			"view": {
				"id": f"V{frappe.generate_hash(length=8)}",
				"hash": frappe.generate_hash(length=10),
				"callback_id": comm_log.CALLBACK_ID,
				"private_metadata": json.dumps({"cs": shortcut.name, "channel": CHANNEL_ID, "message_ts": "1700000001.000100"}),
				"blocks": [{"block_id": comm_log.BLOCK_PARTY}],
				"state": {
					"values": {
						comm_log.BLOCK_PARTY: {
							comm_log.PARTY_ACTION_ID: {"selected_option": {"value": f"0::{todo.name}"}}
						},
						comm_log.BLOCK_SUBJECT: {"value": {"value": "Call mit Alexander"}},
						comm_log.BLOCK_DIRECTION: {"value": {"selected_option": {"value": direction}}},
						comm_log.BLOCK_NOTES: {"value": {"value": notes}},
						comm_log.BLOCK_PHONE: {"value": {"value": "+49 89 123"}},
					}
				},
			},
		}

	def test_submission_creates_communication(self):
		shortcut = ensure_shortcut()
		ensure_slack_user()
		todo = make_todo("Submission target")
		before = frappe.session.user

		with fake_slack() as post:
			self.post_payload(self.submission_payload(shortcut, todo))

		reactions = [c for c in post.call_args_list if "reactions.add" in c.args[0]]
		self.assertEqual(len(reactions), 1)

		self.assertEqual(frappe.local.response.get("response_action"), "clear")
		self.assertEqual(frappe.session.user, before)

		comm = frappe.get_last_doc(
			"Communication", filters={"reference_doctype": "ToDo", "reference_name": todo.name}
		)
		self.assertEqual(comm.communication_medium, "Phone")
		self.assertEqual(comm.subject, "Call mit Alexander")
		self.assertEqual(comm.sent_or_received, "Received")
		self.assertEqual(comm.phone_no, "+49 89 123")
		self.assertIn("Montag", comm.content)
		self.assertEqual(comm.status, "Linked")

	def test_duplicate_submission_creates_one_communication(self):
		shortcut = ensure_shortcut()
		ensure_slack_user()
		todo = make_todo("Duplicate submission target")
		payload = self.submission_payload(shortcut, todo)

		with fake_slack():
			self.post_payload(payload)
			self.post_payload(payload)

		count = frappe.db.count("Communication", {"reference_doctype": "ToDo", "reference_name": todo.name})
		self.assertEqual(count, 1)

	def test_user_without_write_permission_is_rejected(self):
		shortcut = ensure_shortcut()
		email = "comm-log-nobody@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Nobody",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		ensure_slack_user(user=email)
		todo = make_todo("Forbidden target")

		with fake_slack():
			self.post_payload(self.submission_payload(shortcut, todo))

		self.assertEqual(frappe.local.response.get("response_action"), "errors")
		self.assertIn(comm_log.BLOCK_PARTY, frappe.local.response.get("errors") or {})
		self.assertFalse(
			frappe.db.exists("Communication", {"reference_doctype": "ToDo", "reference_name": todo.name})
		)

	def test_disabled_config_is_rejected(self):
		shortcut = ensure_shortcut()
		ensure_slack_user()
		todo = make_todo("Disabled config target")
		payload = self.submission_payload(shortcut, todo)
		frappe.db.set_value("Slack Communication Shortcut", shortcut.name, "enabled", 0)
		frappe.get_cached_doc("Slack Communication Shortcut", shortcut.name)
		frappe.clear_document_cache("Slack Communication Shortcut", shortcut.name)

		with fake_slack():
			self.post_payload(payload)

		self.assertEqual(frappe.local.response.get("response_action"), "errors")


class TestVttToText(SlackBridgeTestCase):
	def test_vtt_is_flattened(self):
		vtt = "\n".join(
			[
				"WEBVTT",
				"",
				"1",
				"00:00:00.000 --> 00:00:02.000",
				"<v Nils>Alex ruft zurück",
				"",
				"2",
				"00:00:02.000 --> 00:00:04.000",
				"Alex ruft zurück",
				"",
				"3",
				"00:00:04.000 --> 00:00:06.000",
				"wegen der Winbond Preise",
			]
		)
		self.assertEqual(comm_log.vtt_to_text(vtt), "Alex ruft zurück wegen der Winbond Preise")

	def test_empty_vtt(self):
		self.assertEqual(comm_log.vtt_to_text(""), "")


class TestNotesFormatting(SlackBridgeTestCase):
	def test_demarkdown_unescapes_slack_entities(self):
		self.assertEqual(comm_log.demarkdown("JTL -&gt; Distrify &amp; Co"), "JTL -> Distrify & Co")

	def test_demarkdown_unwraps_links_and_mentions(self):
		self.assertEqual(
			comm_log.demarkdown("siehe <https://example.com|Angebot> von <@U123>"),
			"siehe Angebot (https://example.com) von @U123",
		)

	def test_nested_bullets_become_nested_lists(self):
		html = comm_log.notes_to_html(
			"Call mit Stephan\n• Generisches Plugin\n• Core Funktionen\n    ◦ Datenmodell\n    ◦ Konfiguration\n• One-Time Sync"
		)
		self.assertEqual(
			html,
			"<div>Call mit Stephan</div>"
			"<ul><li>Generisches Plugin</li><li>Core Funktionen</li>"
			"<ul><li>Datenmodell</li><li>Konfiguration</li></ul>"
			"<li>One-Time Sync</li></ul>",
		)

	def test_bullets_become_a_list(self):
		html = comm_log.notes_to_html("Call mit Alex\n• Winbond Preise\n• 4 Key Anfragen\nFazit: gut")
		self.assertEqual(
			html,
			"<div>Call mit Alex</div>"
			"<ul><li>Winbond Preise</li><li>4 Key Anfragen</li></ul>"
			"<div>Fazit: gut</div>",
		)

	def test_content_is_escaped(self):
		self.assertEqual(comm_log.notes_to_html("• a <b> & c"), "<ul><li>a &lt;b&gt; &amp; c</li></ul>")
