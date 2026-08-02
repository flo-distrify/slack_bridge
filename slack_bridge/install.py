# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe

from slack_bridge.engine.rules import clear_cache

DEFAULTS = {
	"enabled": 1,
	"max_delivery_attempts": 5,
	"retry_backoff_seconds": 60,
	"messages_per_channel_per_minute": 50,
	"message_log_retention_days": 90,
	"interaction_log_retention_days": 30,
}


def after_install():
	# Uninstalling drops the doctypes but can leave the Single's row behind, so a
	# reinstall must reassert the defaults — otherwise the app comes back disabled
	# and silently sends nothing.
	apply_defaults(force=True)


def after_migrate():
	# Only fill in what was never set: an administrator who turned the app off
	# must stay in control of that.
	apply_defaults(force=False)
	# Rule definitions may have changed on disk.
	clear_cache()


def apply_defaults(force: bool = False):
	settings = frappe.get_single("Slack Bridge Settings")

	for fieldname, value in DEFAULTS.items():
		current = settings.get(fieldname)
		if force or current in (None, ""):
			settings.set(fieldname, value)

	settings.flags.ignore_permissions = True
	settings.save()
