# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe

from slack_bridge.engine.rules import clear_cache


def after_install():
	create_settings()


def after_migrate():
	create_settings()
	# Rule definitions may have changed on disk (fixtures, updates).
	clear_cache()


def create_settings():
	"""Materialise the Single so its defaults are readable before anyone opens it."""
	if not frappe.db.exists("Slack Bridge Settings", "Slack Bridge Settings"):
		settings = frappe.new_doc("Slack Bridge Settings")
		settings.enabled = 1
		settings.max_delivery_attempts = 5
		settings.retry_backoff_seconds = 60
		settings.message_log_retention_days = 90
		settings.interaction_log_retention_days = 30
		settings.flags.ignore_permissions = True
		settings.save()
