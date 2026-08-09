# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.utils import nowdate

from slack_bridge.engine import digest, outbox
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	FakeResponse,
	ensure_channel,
	ensure_rule,
	ensure_slack_user,
	ensure_user,
	ensure_workspace,
	fill_mandatory,
)

USER_A = "digest-a@example.com"
USER_B = "digest-b@example.com"  # deliberately never mapped to Slack


def fake_slack_dm(dm_channel: str):
	"""Fake transport that answers conversations.open with a DM id and everything else with ok."""

	def responder(url, **kwargs):
		if "conversations.open" in url:
			return FakeResponse({"ok": True, "channel": {"id": dm_channel}})
		return FakeResponse({"ok": True, "ts": "1700000000.000100", "channel": dm_channel})

	return patch("slack_bridge.slack.client.requests.post", side_effect=responder)


def make_todo(description: str, allocated_to: str | None = None, **values):
	todo = frappe.get_doc(
		{"doctype": "ToDo", "description": description, "status": "Open", "allocated_to": allocated_to}
	)
	todo.update(values)
	fill_mandatory(todo)
	todo.insert(ignore_permissions=True)
	return todo


def digest_rule(title: str, prefix: str, **overrides):
	"""A digest rule scoped to test documents via a description prefix filter."""
	values = {
		"event": "Daily Digest",
		"digest_group_by": "allocated_to",
		"digest_filters": json.dumps({"description": ["like", f"{prefix}%"], "status": "Open"}),
		"subject": "You have {{ count }} open ToDo(s)",
		"message": "{% for d in docs %}• {{ d.description }}\n{% endfor %}",
		"recipients": [],
	}
	values.update(overrides)
	return ensure_rule(title, **values)


class TestDigestValidation(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()

	def test_rejects_group_field_that_is_not_a_user_link(self):
		with self.assertRaises(frappe.ValidationError):
			digest_rule("SB Digest Bad Field", "X-", digest_group_by="description")

	def test_accepts_owner_as_group_field(self):
		rule = digest_rule("SB Digest Owner", "X-", digest_group_by="owner")
		self.assertEqual(rule.digest_group_by, "owner")

	def test_rejects_invalid_filters_json(self):
		with self.assertRaises(frappe.ValidationError):
			digest_rule("SB Digest Bad JSON", "X-", digest_filters="{not json")

	def test_rejects_recipient_rows(self):
		with self.assertRaises(frappe.ValidationError):
			digest_rule(
				"SB Digest With Recipients",
				"X-",
				recipients=[{"recipient_type": "Channel", "source": "Static", "channel": ensure_channel()}],
			)

	def test_other_events_still_require_recipients(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_rule("SB Digest No Recipients", recipients=[])


class TestDigestDelivery(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_user(USER_A)
		ensure_user(USER_B)
		ensure_slack_user(USER_A, "U0DIGESTA")

	def get_logs(self, rule_name: str):
		return frappe.get_all(
			"Slack Message Log",
			filters={"rule": rule_name},
			fields=["name", "payload", "status", "channel_id"],
		)

	def test_groups_and_sends_one_dm_per_mapped_user(self):
		prefix = "SBDIG-G1"
		rule = digest_rule("SB Digest Grouping", prefix)
		make_todo(f"{prefix} first", USER_A)
		make_todo(f"{prefix} second", USER_A)
		make_todo(f"{prefix} unmapped", USER_B)
		make_todo(f"{prefix} closed", USER_A, status="Closed")
		make_todo(f"{prefix} unassigned")

		with fake_slack_dm("D0DIGEST01"):
			queued = digest.run_digest_rule(rule.name, force=True)

			# USER_B has no Slack mapping and the unassigned ToDo has no group user.
			self.assertEqual(queued, 1)
			logs = self.get_logs(rule.name)
			self.assertEqual(len(logs), 1)

			# Delivery normally runs after commit; drive it explicitly here.
			outbox.deliver(logs[0].name)

		self.assertEqual(frappe.db.get_value("Slack Message Log", logs[0].name, "status"), "Sent")

		rendered = json.dumps(json.loads(logs[0].payload)["blocks"])
		self.assertIn(f"{prefix} first", rendered)
		self.assertIn(f"{prefix} second", rendered)
		self.assertIn("You have 2 open ToDo(s)", rendered)
		self.assertNotIn(f"{prefix} closed", rendered)
		self.assertNotIn(f"{prefix} unmapped", rendered)

	def test_condition_is_evaluated_per_document(self):
		prefix = "SBDIG-C1"
		rule = digest_rule("SB Digest Condition", prefix, condition="doc.priority == 'High'")
		make_todo(f"{prefix} high", USER_A, priority="High")
		make_todo(f"{prefix} medium", USER_A, priority="Medium")

		with fake_slack_dm("D0DIGEST02"):
			self.assertEqual(digest.run_digest_rule(rule.name, force=True), 1)

		rendered = json.dumps(json.loads(self.get_logs(rule.name)[0].payload)["blocks"])
		self.assertIn(f"{prefix} high", rendered)
		self.assertNotIn(f"{prefix} medium", rendered)

	def test_once_per_day_after_send_time(self):
		prefix = "SBDIG-T1"
		rule = digest_rule("SB Digest Timing", prefix, digest_send_after="08:00:00")
		make_todo(f"{prefix} task", USER_A)

		before = datetime(2026, 1, 1, 6, 0, 0)
		after = datetime(2026, 1, 1, 9, 0, 0)

		with fake_slack_dm("D0DIGEST03"):
			with patch("slack_bridge.engine.digest.now_datetime", return_value=before):
				self.assertEqual(digest.run_digest_rule(rule.name), 0)
			self.assertFalse(frappe.db.get_value("Slack Notification Rule", rule.name, "last_digest_date"))

			with patch("slack_bridge.engine.digest.now_datetime", return_value=after):
				self.assertEqual(digest.run_digest_rule(rule.name), 1)
				last = frappe.db.get_value("Slack Notification Rule", rule.name, "last_digest_date")
				self.assertEqual(str(last), nowdate())

				# Second tick the same day must not send again.
				self.assertEqual(digest.run_digest_rule(rule.name), 0)

	def test_disabled_rule_only_sends_when_forced(self):
		prefix = "SBDIG-D1"
		rule = digest_rule("SB Digest Disabled", prefix, enabled=0)
		make_todo(f"{prefix} task", USER_A)

		with fake_slack_dm("D0DIGEST04"):
			self.assertEqual(digest.run_digest_rule(rule.name), 0)
			# Send Test Message must still work on a draft/disabled rule.
			self.assertEqual(digest.run_digest_rule(rule.name, force=True), 1)

	def test_block_kit_digest_renders_group_context(self):
		prefix = "SBDIG-BK"
		template = (
			'[{"type": "section", "text": {"type": "mrkdwn", "text": "{{ count }} item(s) for {{ user }}"}}]'
		)
		rule = digest_rule("SB Digest Block Kit", prefix, message_mode="Block Kit", blocks_template=template)
		make_todo(f"{prefix} task", USER_A)

		with fake_slack_dm("D0DIGEST05"):
			self.assertEqual(digest.run_digest_rule(rule.name, force=True), 1)

		rendered = json.dumps(json.loads(self.get_logs(rule.name)[0].payload)["blocks"])
		self.assertIn(f"1 item(s) for {USER_A}", rendered)

	def test_preview_renders_first_group_without_sending(self):
		prefix = "SBDIG-P1"
		rule = digest_rule("SB Digest Preview", prefix)
		make_todo(f"{prefix} task", USER_A)

		result = rule.preview()

		self.assertIn(USER_A, result["docname"])
		self.assertIn(f"{prefix} task", json.dumps(result["blocks"]))
		self.assertFalse(self.get_logs(rule.name))

	def test_build_context_caps_docs_but_reports_full_count(self):
		rule = digest_rule("SB Digest Cap", "SBDIG-CAP")
		docs = [frappe._dict(name=f"doc-{i}") for i in range(digest.MAX_PER_USER + 10)]

		context = digest.build_context(rule, USER_A, docs)

		self.assertEqual(len(context["docs"]), digest.MAX_PER_USER)
		self.assertEqual(context["count"], digest.MAX_PER_USER + 10)
		self.assertEqual(context["user"], USER_A)
		self.assertTrue(context["doc_url"])
