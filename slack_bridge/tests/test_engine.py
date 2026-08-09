# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import json

import frappe

from slack_bridge.engine import outbox, rules
from slack_bridge.engine.context import evaluate_condition, render
from slack_bridge.engine.dispatch import run_rule
from slack_bridge.engine.render import render_message
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	CHANNEL_ID,
	WORKSPACE,
	ensure_channel,
	ensure_rule,
	ensure_workspace,
	fake_slack,
	make_todo,
)


class TestRuleMatching(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel()

	def test_cache_is_populated_and_cleared(self):
		rules.clear_cache("ToDo")
		rule = ensure_rule("SB Test Cache")

		self.assertIn(rule.name, [r["name"] for r in rules.get_rules("ToDo")])

		# Saving a rule must invalidate the cache, or edits would never take effect.
		rule.enabled = 0
		rule.save(ignore_permissions=True)
		self.assertNotIn(rule.name, [r["name"] for r in rules.get_rules("ToDo")])

	def test_unrelated_doctype_has_no_rules(self):
		ensure_rule("SB Test Unrelated")
		self.assertEqual(rules.get_rules("Blog Post"), [])

	def test_matches_new_document_on_after_insert_only(self):
		rule = ensure_rule("SB Test New Doc", event="New Document")
		doc = make_todo()
		candidates = [{"name": rule.name, "event": "New Document"}]

		self.assertEqual(rules.match_rules(candidates, "after_insert", doc), [rule.name])
		self.assertEqual(rules.match_rules(candidates, "on_update", doc), [])
		self.assertEqual(rules.match_rules(candidates, "on_submit", doc), [])

	def test_matches_submit_and_cancel(self):
		candidates = [{"name": "s", "event": "Submit"}, {"name": "c", "event": "Cancel"}]
		doc = make_todo()

		self.assertEqual(rules.match_rules(candidates, "on_submit", doc), ["s"])
		self.assertEqual(rules.match_rules(candidates, "on_cancel", doc), ["c"])

	def test_value_change_only_fires_when_field_changes(self):
		doc = make_todo()
		candidates = [{"name": "vc", "event": "Value Change", "value_changed": "status"}]

		# No previous version loaded yet: nothing to compare, so nothing fires.
		self.assertEqual(rules.match_rules(candidates, "on_change", doc), [])

		# Mirror what a real save does: the pre-save row is loaded, then the in-memory
		# document carries the new values.
		doc.load_doc_before_save()
		doc.status = "Closed"

		self.assertEqual(rules.match_rules(candidates, "on_change", doc), ["vc"])

	def test_value_change_ignores_other_fields(self):
		doc = make_todo()
		doc.load_doc_before_save()
		doc.description = "Something else entirely"

		candidates = [{"name": "vc", "event": "Value Change", "value_changed": "status"}]
		self.assertEqual(rules.match_rules(candidates, "on_change", doc), [])

	def test_method_event_matches_method_name(self):
		doc = make_todo()
		candidates = [{"name": "m", "event": "Method", "method": "on_update"}]

		self.assertEqual(rules.match_rules(candidates, "on_update", doc), ["m"])
		self.assertEqual(rules.match_rules(candidates, "after_insert", doc), [])

	def test_our_own_doctypes_never_trigger(self):
		self.assertIn("Slack Message Log", rules.IGNORED_DOCTYPES)
		self.assertIn("Version", rules.IGNORED_DOCTYPES)


class TestConditionsAndTemplates(SlackBridgeTestCase):
	def test_empty_condition_always_matches(self):
		self.assertTrue(evaluate_condition("", make_todo()))
		self.assertTrue(evaluate_condition(None, make_todo()))

	def test_condition_reads_document(self):
		doc = make_todo("urgent delivery")
		self.assertTrue(evaluate_condition("'urgent' in doc.description", doc))
		self.assertFalse(evaluate_condition("'nothing' in doc.description", doc))

	def test_broken_condition_does_not_fire(self):
		# A rule with a broken condition must stay silent rather than fire blindly.
		self.assertFalse(evaluate_condition("doc.no_such_field.attribute", make_todo()))

	def test_condition_cannot_reach_beyond_sandbox(self):
		with self.assertRaises(Exception):
			frappe.safe_eval("__import__('os').system('true')", None, {"doc": make_todo()})

	def test_render_uses_document_context(self):
		doc = make_todo("ship it")
		self.assertEqual(render("{{ doc.description }}", doc), "ship it")

	def test_render_exposes_doc_url(self):
		doc = make_todo()
		self.assertIn(doc.name, render("{{ doc_url }}", doc))


class TestRenderMessage(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel()

	def test_simple_mode_renders_headline_and_body(self):
		rule = ensure_rule("SB Test Simple", subject="Task {{ doc.name }}", message="{{ doc.description }}")
		doc = make_todo("pack the crate")

		blocks, text = render_message(rule, doc)

		rendered = json.dumps(blocks)
		self.assertIn("pack the crate", rendered)
		self.assertIn(doc.name, rendered)
		self.assertTrue(text)

	def test_simple_mode_appends_document_link(self):
		rule = ensure_rule("SB Test Link", attach_document_link=1)
		doc = make_todo()

		blocks, _text = render_message(rule, doc)
		self.assertIn(doc.name, json.dumps(blocks))
		self.assertEqual(blocks[-1]["type"], "context")

	def test_block_kit_mode_renders_template(self):
		rule = ensure_rule(
			"SB Test BlockKit",
			message_mode="Block Kit",
			blocks_template='[{"type":"section","text":{"type":"mrkdwn","text":"{{ doc.name }}"}}]',
		)
		doc = make_todo()

		blocks, _text = render_message(rule, doc)
		self.assertEqual(blocks[0]["text"]["text"], doc.name)

	def test_invalid_block_kit_is_rejected_at_save(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_rule(
				"SB Test BadJinja",
				message_mode="Block Kit",
				blocks_template="{% for x in %}",
			)

	def test_invalid_condition_is_rejected_at_save(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_rule("SB Test BadCondition", condition="doc.status ==")

	def test_unknown_watched_field_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_rule("SB Test BadField", event="Value Change", value_changed="not_a_field")


class TestOutbox(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel()

	def setUp(self):
		# Per-channel pacing is real behaviour, but it would make delivery assertions
		# depend on how fast the previous test ran.
		frappe.cache().delete_key(outbox.PACING_CACHE)

	def test_dedupe_key_is_stable_and_specific(self):
		args = ("rule", "ToDo", "T1", CHANNEL_ID, "after_insert", "hello")
		self.assertEqual(outbox.dedupe_key(*args), outbox.dedupe_key(*args))

		other = ("rule", "ToDo", "T2", CHANNEL_ID, "after_insert", "hello")
		self.assertNotEqual(outbox.dedupe_key(*args), outbox.dedupe_key(*other))

	def test_identical_message_is_queued_once(self):
		doc = make_todo()

		with fake_slack():
			first = outbox.queue(
				workspace=WORKSPACE,
				channel_id=CHANNEL_ID,
				blocks=[{"type": "divider"}],
				text="same text",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)
			second = outbox.queue(
				workspace=WORKSPACE,
				channel_id=CHANNEL_ID,
				blocks=[{"type": "divider"}],
				text="same text",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)

		self.assertTrue(first)
		self.assertIsNone(second)

	def test_successful_delivery_records_timestamp(self):
		doc = make_todo()

		with fake_slack():
			name = outbox.queue(
				workspace=WORKSPACE,
				channel_id=CHANNEL_ID,
				blocks=[{"type": "divider"}],
				text="delivery test",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)
			outbox.deliver(name)

		log = frappe.get_doc("Slack Message Log", name)
		self.assertEqual(log.status, "Sent")
		self.assertEqual(log.message_ts, "1700000000.000100")

	def test_permanent_error_is_not_retried(self):
		doc = make_todo()

		with fake_slack():
			name = outbox.queue(
				workspace=WORKSPACE,
				channel_id="C_MISSING",
				blocks=[{"type": "divider"}],
				text="permanent failure",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)

		with fake_slack({"ok": False, "error": "channel_not_found"}):
			outbox.deliver(name)

		log = frappe.get_doc("Slack Message Log", name)
		self.assertEqual(log.status, "Dead")
		self.assertIn("channel_not_found", log.last_error)

	def test_rate_limit_reschedules_instead_of_failing(self):
		doc = make_todo()

		with fake_slack():
			name = outbox.queue(
				workspace=WORKSPACE,
				channel_id=CHANNEL_ID,
				blocks=[{"type": "divider"}],
				text="rate limited",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)

		with fake_slack(
			{"ok": False, "error": "ratelimited"}, status_code=429, headers={"Retry-After": "30"}
		):
			outbox.deliver(name)

		log = frappe.get_doc("Slack Message Log", name)
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.attempts, 1)
		self.assertIsNotNone(log.next_attempt_at)

	def test_delivered_message_is_not_sent_again(self):
		doc = make_todo()

		with fake_slack():
			name = outbox.queue(
				workspace=WORKSPACE,
				channel_id=CHANNEL_ID,
				blocks=[{"type": "divider"}],
				text="only once",
				reference_doctype="ToDo",
				reference_name=doc.name,
				send_now=False,
			)
			outbox.deliver(name)

		with fake_slack({"ok": False, "error": "should_not_be_called"}) as mocked:
			outbox.deliver(name)
			mocked.assert_not_called()


class TestDispatch(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_channel()

	def setUp(self):
		frappe.cache().delete_key(outbox.PACING_CACHE)

	def test_rule_produces_a_queued_message(self):
		rule = ensure_rule("SB Test Dispatch", message="{{ doc.description }}")
		doc = make_todo("dispatch me")

		with fake_slack():
			run_rule(rule.name, "ToDo", doc.name, "after_insert")

		log = frappe.get_all(
			"Slack Message Log",
			filters={"rule": rule.name, "reference_name": doc.name},
			fields=["name", "channel_id", "payload"],
		)

		self.assertEqual(len(log), 1)
		self.assertEqual(log[0].channel_id, CHANNEL_ID)
		self.assertIn("dispatch me", log[0].payload)

	def test_failing_condition_sends_nothing(self):
		rule = ensure_rule("SB Test Condition", condition="doc.status == 'Cancelled'")
		doc = make_todo()

		with fake_slack():
			run_rule(rule.name, "ToDo", doc.name, "after_insert")

		self.assertFalse(
			frappe.db.exists("Slack Message Log", {"rule": rule.name, "reference_name": doc.name})
		)

	def test_disabled_rule_sends_nothing(self):
		rule = ensure_rule("SB Test Disabled")
		rule.db_set("enabled", 0)
		doc = make_todo()

		with fake_slack():
			run_rule(rule.name, "ToDo", doc.name, "after_insert")

		self.assertFalse(
			frappe.db.exists("Slack Message Log", {"rule": rule.name, "reference_name": doc.name})
		)

	def test_deleted_document_is_skipped(self):
		rule = ensure_rule("SB Test Deleted")

		with fake_slack():
			run_rule(rule.name, "ToDo", "does-not-exist", "after_insert")

		self.assertFalse(frappe.db.exists("Slack Message Log", {"rule": rule.name}))


class TestTemplateContext(SlackBridgeTestCase):
	def test_json_is_available_to_templates(self):
		# Notifying on log/event doctypes means reading a JSON payload field; without
		# a parser in the context those templates cannot be written at all.
		doc = make_todo()
		doc.description = '{"customer": "ACME", "rows": 42}'

		rendered = render('{{ json.loads(doc.description)["customer"] }}', doc)
		self.assertEqual(rendered, "ACME")

	def test_parse_json_helper_works(self):
		# The app provides parse_json itself: frappe v16 has it in the template
		# namespace, v15 does not, and templates must work identically on both.
		doc = make_todo()
		doc.description = '{"rows": 42}'

		self.assertEqual(render("{{ parse_json(doc.description).rows }}", doc), "42")

	def test_as_json_is_available(self):
		doc = make_todo()
		self.assertIn(doc.name, render("{{ as_json({'n': doc.name}) }}", doc))

	def test_context_still_sandboxed(self):
		doc = make_todo()
		with self.assertRaises(Exception):
			render("{{ json.__class__.__mro__ }}", doc)


class TestHtmlToMrkdwn(SlackBridgeTestCase):
	def test_common_structure_is_converted(self):
		from slack_bridge.engine.context import html_to_mrkdwn

		html = "<b>Form Test</b> — Manual<br>Priority: <strong>Medium</strong><div>Assigned by flo@distrify.io</div>"
		out = html_to_mrkdwn(html)

		self.assertIn("*Form Test* — Manual\nPriority: *Medium*", out)
		self.assertIn("\nAssigned by flo@distrify.io", out)
		self.assertNotIn("<b>", out)
		self.assertNotIn("<div>", out)

	def test_links_lists_and_entities(self):
		from slack_bridge.engine.context import html_to_mrkdwn

		html = '<a href="https://example.com/x">Open it</a> &amp; more<ul><li>one</li><li>two</li></ul>'
		out = html_to_mrkdwn(html)

		self.assertIn("<https://example.com/x|Open it>", out)
		self.assertIn("&amp; more", out)
		self.assertIn("• one\n• two", out)

	def test_plain_text_is_transport_escaped(self):
		from slack_bridge.engine.context import html_to_mrkdwn

		self.assertEqual(html_to_mrkdwn("a < b & c"), "a &lt; b &amp; c")
		self.assertEqual(html_to_mrkdwn(""), "")
		self.assertEqual(html_to_mrkdwn(None), "")

	def test_helper_is_available_in_templates(self):
		doc = make_todo()
		doc.description = "<b>Bold</b> task"

		rendered = render("{{ html_to_mrkdwn(doc.description) }}", doc)

		self.assertEqual(rendered, "*Bold* task")
