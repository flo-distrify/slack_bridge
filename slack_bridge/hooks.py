app_name = "slack_bridge"
app_title = "Slack Bridge"
app_publisher = "Automates UG"
app_description = "Bidirectional Slack connector for Frappe: config-driven notifications, approvals, commands, modals and unfurls"
app_email = "operations@distrify.io"
app_license = "agpl-3.0"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
after_install = "slack_bridge.install.after_install"
after_migrate = "slack_bridge.install.after_migrate"

# ---------------------------------------------------------------------------
# Distrify Processes message channel (name-only coupling, like slack_bridge_doc_url
# in the other direction): distrify_processes reads this hook and resolves the
# dotted path; neither app imports the other. Inert when processes isn't installed.
# ---------------------------------------------------------------------------
process_message_channels = ["slack_bridge.integrations.process_channel.get_channels"]

# ---------------------------------------------------------------------------
# Document events
#
# The wildcard handler is on the hot path of every document write in the site, so
# it bails in well under a millisecond when no rule targets the doctype: one Redis
# hash lookup against a cache that is invalidated whenever a rule changes.
# ---------------------------------------------------------------------------
doc_events = {
	"*": {
		"after_insert": "slack_bridge.engine.rules.on_doc_event",
		"on_update": "slack_bridge.engine.rules.on_doc_event",
		"on_submit": "slack_bridge.engine.rules.on_doc_event",
		"on_cancel": "slack_bridge.engine.rules.on_doc_event",
		"on_trash": "slack_bridge.engine.rules.on_doc_event",
		"on_update_after_submit": "slack_bridge.engine.rules.on_doc_event",
		"on_change": "slack_bridge.engine.rules.on_doc_event",
	},
	"User": {
		"after_insert": "slack_bridge.engine.users.on_user_change",
		"on_update": "slack_bridge.engine.users.on_user_change",
	},
}

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------
scheduler_events = {
	"cron": {
		# Deliver queued messages with exponential backoff.
		"* * * * *": ["slack_bridge.engine.outbox.drain"],
		# Minute-offset rules (Slack mirrors core Notification's five-minute tick).
		"0/5 * * * *": ["slack_bridge.engine.scheduled.trigger_minute_rules"],
	},
	"daily": [
		"slack_bridge.engine.scheduled.trigger_daily_rules",
	],
	"hourly": [
		"slack_bridge.engine.users.refresh_stale_workspaces",
		# Daily Digest rules: due once per day, gated by each rule's send-after time.
		"slack_bridge.engine.digest.trigger_digest_rules",
	],
}

# Frappe's log-clearing job prunes these automatically.
default_log_clearing_doctypes = {
	"Slack Message Log": 90,
	"Slack Interaction Log": 30,
}

# Show Slack messages about a document on that document's timeline.
additional_timeline_content = {
	"*": ["slack_bridge.engine.timeline.get_timeline_content"],
}

# Slack messages should never block deletion of the document they reference.
ignore_links_on_delete = ["Slack Message Log", "Slack Interaction Log"]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True
