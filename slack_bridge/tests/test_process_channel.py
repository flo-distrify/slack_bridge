# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""The Distrify Processes message-channel provider. Runs without distrify_processes
installed — the contract is just a function call with plain data."""

import frappe

from slack_bridge.integrations import process_channel
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import CHANNEL_ID, WORKSPACE, ensure_channel, ensure_workspace, fake_slack

def _context() -> dict:
	"""The plain-data context distrify_processes hands a provider. The Message Log
	validates reference_name as a dynamic link, so when the processes app happens to
	be installed on the test site we reference a REAL minimal instance; on a pure
	slack_bridge site the log row simply carries no reference."""
	context = {
		"node_id": "msg-1",
		"definition": "PD-TEST",
		"instance_subject": "Acme onboarding",
		"variables": {},
		"references": [],
	}
	if not frappe.db.exists("DocType", "Process Instance"):
		context["instance"] = None
		return context
	existing = frappe.db.get_value("Process Instance", {"subject": "SB process-channel test"})
	if existing:
		context["instance"] = existing
		return context
	definition = frappe.get_doc(
		{"doctype": "Process Definition", "title": "SB Process Channel Test", "status": "Draft"}
	).insert(ignore_permissions=True)
	version = frappe.get_doc(
		{
			"doctype": "Process Definition Version",
			"process_definition": definition.name,
			"version": 1,
			"status": "Draft",
			"nodes": [{"node_id": "start", "node_type": "Start", "label": "Start"}],
			"edges": [],
		}
	).insert(ignore_permissions=True)
	instance = frappe.get_doc(
		{
			"doctype": "Process Instance",
			"subject": "SB process-channel test",
			"process_definition": definition.name,
			"definition_version": version.name,
			"status": "Running",
		}
	).insert(ignore_permissions=True)
	context["instance"] = instance.name
	return context


class TestProcessChannel(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel(channel_name="general")
		cls.context = _context()

	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Slack Bridge Settings", "default_workspace", WORKSPACE)
		frappe.db.delete("Slack Message Log")

	def test_get_channels_spec(self):
		specs = process_channel.get_channels()
		self.assertEqual(specs[0]["name"], "slack")
		self.assertFalse(specs[0]["supports_subject"])
		self.assertTrue(callable(frappe.get_attr(specs[0]["send"])))

	def test_send_to_channel_queues_message(self):
		with fake_slack():
			detail = process_channel.send("#general", None, "Hello **world**", self.context)
		self.assertIn("general", detail)
		logs = frappe.get_all(
			"Slack Message Log",
			filters={"reference_name": self.context["instance"]},
			fields=["channel_id", "payload", "reference_doctype"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].channel_id, CHANNEL_ID)
		self.assertEqual(logs[0].reference_doctype, "Process Instance")
		self.assertIn("*world*", logs[0].payload)  # markdown → mrkdwn

	def test_dedupe_within_minute_reports_already_queued(self):
		with fake_slack():
			process_channel.send("#general", None, "Same body", self.context)
			detail = process_channel.send("#general", None, "Same body", self.context)
		self.assertIn("already queued", detail)
		self.assertEqual(
			len(frappe.get_all("Slack Message Log", filters={"reference_name": self.context["instance"]})), 1
		)

	def test_unresolvable_recipient_raises(self):
		self.assertRaises(
			frappe.ValidationError, process_channel.send, "#no-such-channel", None, "hi", self.context
		)

	def test_ambiguous_workspace_raises(self):
		frappe.db.set_single_value("Slack Bridge Settings", "default_workspace", None)
		second = "Second Test Workspace"
		if not frappe.db.exists("Slack Workspace", second):
			ensure_workspace(second)
		try:
			self.assertRaises(frappe.ValidationError, process_channel.send, "#general", None, "hi", self.context)
		finally:
			frappe.db.set_value("Slack Workspace", second, "enabled", 0)

	def test_subject_becomes_bold_lead(self):
		with fake_slack():
			process_channel.send("#general", "Order shipped", "body text", self.context)
		log = frappe.get_all(
			"Slack Message Log", filters={"reference_name": self.context["instance"]}, fields=["payload"]
		)[0]
		self.assertIn("*Order shipped*", log.payload)
