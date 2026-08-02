# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe

from slack_bridge.install import after_install, after_migrate
from slack_bridge.tests import SlackBridgeTestCase


class TestSettingsDefaults(SlackBridgeTestCase):
	def set_enabled(self, value):
		settings = frappe.get_single("Slack Bridge Settings")
		settings.enabled = value
		settings.flags.ignore_permissions = True
		settings.save()

	def tearDown(self):
		self.set_enabled(1)

	def test_reinstall_reasserts_defaults(self):
		# Uninstalling drops the doctypes but leaves the Single's row, so a reinstall
		# used to come back disabled and silently send nothing.
		self.set_enabled(0)

		after_install()

		self.assertEqual(frappe.db.get_single_value("Slack Bridge Settings", "enabled"), 1)

	def test_migrate_respects_a_deliberate_disable(self):
		self.set_enabled(0)

		after_migrate()

		self.assertEqual(frappe.db.get_single_value("Slack Bridge Settings", "enabled"), 0)

	def test_migrate_fills_in_never_set_values(self):
		# A field that was never written has no row in tabSingles at all.
		frappe.db.delete(
			"Singles", {"doctype": "Slack Bridge Settings", "field": "max_delivery_attempts"}
		)
		frappe.clear_document_cache("Slack Bridge Settings", "Slack Bridge Settings")

		after_migrate()

		self.assertEqual(
			frappe.db.get_single_value("Slack Bridge Settings", "max_delivery_attempts"), 5
		)

	def test_migrate_keeps_an_explicit_value(self):
		settings = frappe.get_single("Slack Bridge Settings")
		settings.max_delivery_attempts = 9
		settings.flags.ignore_permissions = True
		settings.save()

		after_migrate()

		self.assertEqual(
			frappe.db.get_single_value("Slack Bridge Settings", "max_delivery_attempts"), 9
		)
