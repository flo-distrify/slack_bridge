# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack request signature verification.

Every inbound request carries `x-slack-request-timestamp` and `x-slack-signature`.
We recompute HMAC-SHA256 over the *raw* body and compare in constant time, rejecting
anything older than five minutes to bound replay attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import frappe
from frappe import _

VERSION = "v0"
MAX_SKEW_SECONDS = 60 * 5


class SlackSignatureError(frappe.AuthenticationError):
	pass


def compute_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
	basestring = b"%s:%s:%s" % (VERSION.encode(), str(timestamp).encode(), raw_body)
	digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
	return f"{VERSION}={digest}"


def verify_request(signing_secret: str, timestamp, signature: str, raw_body: bytes) -> None:
	"""Raise SlackSignatureError unless the request is a genuine, fresh Slack request."""
	if not signature or not timestamp:
		raise SlackSignatureError(_("Missing Slack signature headers"))

	try:
		age = abs(time.time() - int(timestamp))
	except (TypeError, ValueError):
		raise SlackSignatureError(_("Invalid Slack timestamp header"))

	if age > MAX_SKEW_SECONDS:
		raise SlackSignatureError(_("Slack request timestamp is outside the allowed window"))

	if not signing_secret:
		raise SlackSignatureError(_("No signing secret configured for this workspace"))

	expected = compute_signature(signing_secret, timestamp, raw_body)
	if not hmac.compare_digest(expected, signature):
		raise SlackSignatureError(_("Slack signature verification failed"))
