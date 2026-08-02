# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe

from slack_bridge.slack import blocks as bk
from slack_bridge.tests import SlackBridgeTestCase


class TestBlocks(SlackBridgeTestCase):
	def test_truncate_respects_limit(self):
		self.assertEqual(bk.truncate("hello", 10), "hello")
		self.assertEqual(len(bk.truncate("x" * 100, 10)), 10)

	def test_validate_blocks_accepts_bare_list(self):
		parsed = bk.validate_blocks('[{"type": "section"}]')
		self.assertEqual(parsed, [{"type": "section"}])

	def test_validate_blocks_accepts_envelope(self):
		parsed = bk.validate_blocks('{"blocks": [{"type": "divider"}]}')
		self.assertEqual(parsed, [{"type": "divider"}])

	def test_validate_blocks_rejects_bad_json(self):
		with self.assertRaises(frappe.ValidationError):
			bk.validate_blocks("{not json")

	def test_validate_blocks_rejects_non_list(self):
		with self.assertRaises(frappe.ValidationError):
			bk.validate_blocks('{"type": "section"}')

	def test_validate_blocks_rejects_untyped_entries(self):
		with self.assertRaises(frappe.ValidationError):
			bk.validate_blocks('[{"text": "no type"}]')

	def test_validate_blocks_rejects_too_many(self):
		too_many = "[" + ",".join(['{"type":"divider"}'] * (bk.MAX_BLOCKS + 1)) + "]"
		with self.assertRaises(frappe.ValidationError):
			bk.validate_blocks(too_many)

	def test_clamp_blocks_keeps_within_limit(self):
		clamped = bk.clamp_blocks([{"type": "divider"}] * 60)
		self.assertEqual(len(clamped), bk.MAX_BLOCKS)
		self.assertEqual(clamped[-1]["type"], "context")

	def test_actions_chunks_buttons(self):
		elements = [bk.button(f"b{i}", f"a{i}") for i in range(12)]
		chunks = bk.actions(elements)

		self.assertEqual(len(chunks), 3)
		self.assertTrue(all(len(c["elements"]) <= bk.MAX_BUTTONS_PER_ACTIONS_BLOCK for c in chunks))

	def test_button_value_is_truncated(self):
		element = bk.button("Approve", "sb_action_0", value="x" * 5000)
		self.assertLessEqual(len(element["value"]), bk.MAX_ACTION_VALUE)

	def test_button_rejects_unknown_style(self):
		self.assertNotIn("style", bk.button("Go", "a", style="rainbow"))
		self.assertEqual(bk.button("Go", "a", style="danger")["style"], "danger")

	def test_fallback_text_prefers_section(self):
		blocks = [{"type": "divider"}, bk.section("Order SO-0001 needs approval")]
		self.assertIn("SO-0001", bk.fallback_text(blocks))

	def test_fallback_text_never_empty(self):
		self.assertTrue(bk.fallback_text([{"type": "divider"}]))
