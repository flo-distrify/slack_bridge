# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Durable outbound delivery.

Every outbound message becomes a Slack Message Log row first, then a delivery attempt.
That gives operators a delivery log, survives worker restarts, and lets us honour
Slack's per-channel pacing and Retry-After without losing messages.
"""

from __future__ import annotations

import hashlib
import json
import time

import frappe
from frappe.utils import add_to_date, now_datetime

from slack_bridge.slack.client import SlackClient, SlackError, SlackRateLimited

PACING_CACHE = "slack_bridge_channel_pace"
MIN_SECONDS_BETWEEN_SENDS = 1.0
DRAIN_BATCH_SIZE = 100


def settings():
	return frappe.get_cached_doc("Slack Bridge Settings")


def dedupe_key(rule, reference_doctype, reference_name, channel_id, event_method, text) -> str:
	# The minute bucket lets an identical message go out again later (a daily digest,
	# say) while still collapsing duplicates from a single write.
	bucket = now_datetime().strftime("%Y%m%d%H%M")
	raw = "|".join(
		[
			str(rule or ""),
			str(reference_doctype or ""),
			str(reference_name or ""),
			str(channel_id or ""),
			str(event_method or ""),
			bucket,
			hashlib.sha1((text or "").encode()).hexdigest(),
		]
	)
	return hashlib.sha1(raw.encode()).hexdigest()


def queue(
	workspace: str,
	channel_id: str,
	blocks: list,
	text: str,
	rule: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	thread_mode: str = "New Message",
	event_method: str | None = None,
	send_now: bool = True,
) -> str | None:
	"""Create the outbox row and attempt delivery. Returns the log name, or None if deduped."""
	key = dedupe_key(rule, reference_doctype, reference_name, channel_id, event_method, text)

	if frappe.db.exists("Slack Message Log", {"dedupe_key": key}):
		return None

	log = frappe.get_doc(
		{
			"doctype": "Slack Message Log",
			"status": "Queued",
			"workspace": workspace,
			"channel_id": channel_id,
			"rule": rule,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"dedupe_key": key,
			"payload": json.dumps(
				{"blocks": blocks, "text": text, "thread_mode": thread_mode}, separators=(",", ":")
			),
		}
	)

	try:
		log.insert(ignore_permissions=True)
	except frappe.exceptions.DuplicateEntryError:
		return None

	if send_now:
		frappe.enqueue(
			"slack_bridge.engine.outbox.deliver",
			queue="short",
			enqueue_after_commit=True,
			job_id=f"slack-bridge-deliver-{log.name}",
			deduplicate=True,
			log_name=log.name,
		)

	return log.name


# --------------------------------------------------------------------- delivery


def deliver(log_name: str) -> None:
	"""Attempt one delivery. Reschedules itself through the outbox on failure."""
	try:
		log = frappe.get_doc("Slack Message Log", log_name)
	except frappe.DoesNotExistError:
		return

	if log.status in ("Sent", "Dead", "Skipped"):
		return

	if not channel_ready(log.channel_id):
		# Waiting on the per-channel pace is not a failure, so it must not consume
		# the retry budget — otherwise a busy channel would kill its own messages.
		defer(log, seconds=2)
		return

	payload = json.loads(log.payload or "{}")
	blocks = payload.get("blocks") or []
	text = payload.get("text") or ""
	thread_mode = payload.get("thread_mode") or "New Message"

	try:
		client = SlackClient(log.workspace)
		previous = find_previous_message(log) if thread_mode != "New Message" else None

		if thread_mode == "Update Previous Message" and previous:
			response = client.update_message(
				channel=previous.channel_id, ts=previous.message_ts, blocks=blocks, text=text
			)
			log.message_ts = previous.message_ts
		else:
			thread_ts = (previous.thread_ts or previous.message_ts) if previous else None
			response = client.post_message(
				channel=log.channel_id, blocks=blocks, text=text, thread_ts=thread_ts
			)
			log.message_ts = response.get("ts")
			log.thread_ts = thread_ts or response.get("ts")
			# Slack resolves the conversation id for us (DMs, renamed channels).
			log.channel_id = response.get("channel") or log.channel_id

		log.status = "Sent"
		log.attempts = (log.attempts or 0) + 1
		log.last_error = None
		log.next_attempt_at = None
		log.save(ignore_permissions=True)
		mark_channel_sent(log.channel_id)

	except SlackRateLimited as e:
		reschedule(log, seconds=e.retry_after, error=str(e))
	except SlackError as e:
		if e.is_permanent:
			fail(log, f"{e.code}: {e}")
		else:
			reschedule(log, error=str(e))
	except Exception:
		reschedule(log, error=frappe.get_traceback(with_context=False))


def defer(log, seconds: int) -> None:
	"""Postpone without counting an attempt (used for pacing, not for errors)."""
	log.status = "Queued"
	log.next_attempt_at = add_to_date(now_datetime(), seconds=seconds)
	log.save(ignore_permissions=True)
	frappe.db.commit()


def reschedule(log, seconds: int | None = None, error: str | None = None) -> None:
	attempts = (log.attempts or 0) + 1
	config = settings()
	max_attempts = config.max_delivery_attempts or 5

	if attempts >= max_attempts:
		fail(log, error, status="Dead", attempts=attempts)
		return

	if seconds is None:
		base = config.retry_backoff_seconds or 60
		seconds = base * (2 ** (attempts - 1))

	log.status = "Failed"
	log.attempts = attempts
	log.last_error = (error or "")[:500]
	log.next_attempt_at = add_to_date(now_datetime(), seconds=seconds)
	log.save(ignore_permissions=True)
	frappe.db.commit()


def fail(log, error: str | None, status: str = "Dead", attempts: int | None = None) -> None:
	log.status = status
	log.attempts = attempts if attempts is not None else (log.attempts or 0) + 1
	log.last_error = (error or "")[:500]
	log.next_attempt_at = None
	log.save(ignore_permissions=True)
	frappe.db.commit()


def drain() -> None:
	"""Cron entry point: retry everything that is due."""
	rows = frappe.get_all(
		"Slack Message Log",
		filters=[
			["status", "in", ["Queued", "Failed"]],
			["next_attempt_at", "<=", now_datetime()],
		],
		or_filters=[["next_attempt_at", "is", "not set"]],
		fields=["name"],
		order_by="creation asc",
		limit=DRAIN_BATCH_SIZE,
	)

	for row in rows:
		try:
			deliver(row.name)
		except Exception:
			frappe.log_error(title="Slack Bridge: drain failed", message=frappe.get_traceback())


def find_previous_message(log):
	"""The most recent delivered message about the same document."""
	if not (log.reference_doctype and log.reference_name):
		return None

	rows = frappe.get_all(
		"Slack Message Log",
		filters={
			"status": "Sent",
			"reference_doctype": log.reference_doctype,
			"reference_name": log.reference_name,
			"channel_id": log.channel_id,
			"name": ["!=", log.name],
		},
		fields=["name", "channel_id", "message_ts", "thread_ts"],
		order_by="creation asc",
		limit=1,
	)

	return rows[0] if rows else None


# ---------------------------------------------------------------------- pacing


def channel_ready(channel_id: str) -> bool:
	"""Slack allows roughly one message per second per channel."""
	last = frappe.cache().hget(PACING_CACHE, channel_id)
	if not last:
		return True

	return (time.time() - float(last)) >= MIN_SECONDS_BETWEEN_SENDS


def mark_channel_sent(channel_id: str) -> None:
	frappe.cache().hset(PACING_CACHE, channel_id, time.time())


# ------------------------------------------------------------------- ad-hoc use


def send_now(workspace: str, channel_id: str, blocks: list, text: str, **kwargs) -> str | None:
	"""Queue a one-off message that is not tied to a rule (test sends, command replies)."""
	return queue(workspace=workspace, channel_id=channel_id, blocks=blocks, text=text, **kwargs)
