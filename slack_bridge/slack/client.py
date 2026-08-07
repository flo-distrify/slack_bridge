# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Minimal Slack Web API client.

Deliberately built on ``requests`` (a Frappe dependency) instead of ``slack_sdk`` so the
app installs with zero extra packages. The Web API is a flat JSON-over-HTTPS RPC, so the
surface we need is small; what matters is correct error and rate-limit handling.
"""

from __future__ import annotations

import json

import frappe
import requests
from frappe import _

API_BASE = "https://slack.com/api/"
DEFAULT_TIMEOUT = 15

# Slack error codes that will never succeed on retry — fail the message immediately
# instead of burning the retry budget.
PERMANENT_ERRORS = {
	"channel_not_found",
	"not_in_channel",
	"is_archived",
	"invalid_auth",
	"account_inactive",
	"token_revoked",
	"no_permission",
	"missing_scope",
	"invalid_blocks",
	"invalid_blocks_format",
	"msg_too_long",
	"user_not_found",
	"users_not_found",
	"restricted_action",
	"cannot_dm_bot",
}


class SlackError(frappe.ValidationError):
	"""A Slack API call returned ok=false."""

	def __init__(self, message, code=None, response=None):
		super().__init__(message)
		self.code = code
		self.response = response

	@property
	def is_permanent(self) -> bool:
		return self.code in PERMANENT_ERRORS


class SlackRateLimited(SlackError):
	"""HTTP 429 — carries the server-provided Retry-After in seconds."""

	def __init__(self, message, retry_after=60, response=None):
		super().__init__(message, code="ratelimited", response=response)
		self.retry_after = retry_after


class SlackClient:
	"""Bound to one Slack Workspace document."""

	def __init__(self, workspace, token: str | None = None):
		if isinstance(workspace, str):
			workspace = frappe.get_cached_doc("Slack Workspace", workspace)

		self.workspace = workspace
		self._token = token

	@property
	def token(self) -> str:
		if self._token is None:
			self._token = self.workspace.get_password("bot_token", raise_exception=False)

		if not self._token:
			frappe.throw(
				_("Slack Workspace {0} has no bot token configured.").format(self.workspace.name),
				title=_("Slack not configured"),
			)

		return self._token

	# ------------------------------------------------------------------ core

	def call(self, method: str, use_json: bool = True, **params) -> dict:
		"""Call a Web API method and return its payload.

		Raises SlackRateLimited on 429 (with retry_after) and SlackError on ok=false.
		"""
		url = API_BASE + method
		headers = {"Authorization": f"Bearer {self.token}"}

		# Slack accepts JSON bodies for most methods; a few legacy ones want form encoding.
		if use_json:
			headers["Content-Type"] = "application/json; charset=utf-8"
			payload = {k: v for k, v in params.items() if v is not None}
			response = requests.post(
				url, headers=headers, data=json.dumps(payload), timeout=DEFAULT_TIMEOUT
			)
		else:
			payload = {
				k: (json.dumps(v) if isinstance(v, list | dict) else v)
				for k, v in params.items()
				if v is not None
			}
			response = requests.post(url, headers=headers, data=payload, timeout=DEFAULT_TIMEOUT)

		if response.status_code == 429:
			retry_after = int(response.headers.get("Retry-After") or 60)
			raise SlackRateLimited(
				_("Slack rate limited {0}; retry after {1}s").format(method, retry_after),
				retry_after=retry_after,
				response=response.text,
			)

		try:
			data = response.json()
		except ValueError:
			raise SlackError(
				_("Slack returned a non-JSON response for {0} (HTTP {1})").format(
					method, response.status_code
				),
				code="invalid_response",
				response=response.text[:500],
			)

		if not data.get("ok"):
			code = data.get("error", "unknown_error")
			detail = ""

			# missing_scope names the scopes at fault; without them the admin has no
			# idea which permission to add or that a reinstall is required.
			if code == "missing_scope" and data.get("needed"):
				missing = sorted(
					set((data.get("needed") or "").split(","))
					- set((data.get("provided") or "").split(","))
				)
				detail = _(
					" — this Slack app is missing the scope(s) {0}. Add them to the app "
					"(or re-apply the generated manifest) and reinstall it to your workspace."
				).format(", ".join(missing))

			# invalid_blocks etc. put the useful part in response_metadata.messages
			messages = (data.get("response_metadata") or {}).get("messages")
			if messages:
				detail += " — " + "; ".join(messages)

			raise SlackError(
				_("Slack API {0} failed: {1}{2}").format(method, code, detail),
				code=code,
				response=json.dumps(data)[:2000],
			)

		return data

	# ------------------------------------------------------- messaging

	def post_message(self, channel: str, blocks=None, text=None, thread_ts=None, **kwargs) -> dict:
		return self.call(
			"chat.postMessage",
			channel=channel,
			blocks=blocks,
			text=text,
			thread_ts=thread_ts,
			**kwargs,
		)

	def update_message(self, channel: str, ts: str, blocks=None, text=None, **kwargs) -> dict:
		return self.call("chat.update", channel=channel, ts=ts, blocks=blocks, text=text, **kwargs)

	def post_ephemeral(self, channel: str, user: str, text=None, blocks=None, **kwargs) -> dict:
		return self.call(
			"chat.postEphemeral", channel=channel, user=user, text=text, blocks=blocks, **kwargs
		)

	def unfurl(self, channel: str, ts: str, unfurls: dict, unfurl_id=None, source=None) -> dict:
		# When Slack gives us unfurl_id/source we must use them (the channel/ts pair is
		# not always resolvable, e.g. for links shared in private conversations).
		if unfurl_id and source:
			return self.call("chat.unfurl", unfurl_id=unfurl_id, source=source, unfurls=unfurls)

		return self.call("chat.unfurl", channel=channel, ts=ts, unfurls=unfurls)

	# ----------------------------------------------------------- views

	def open_view(self, trigger_id: str, view: dict) -> dict:
		return self.call("views.open", trigger_id=trigger_id, view=view)

	def push_view(self, trigger_id: str, view: dict) -> dict:
		return self.call("views.push", trigger_id=trigger_id, view=view)

	def publish_home(self, user_id: str, view: dict) -> dict:
		return self.call("views.publish", user_id=user_id, view=view)

	def add_reaction(self, channel: str, timestamp: str, name: str) -> dict:
		return self.call("reactions.add", channel=channel, timestamp=timestamp, name=name)

	def join_channel(self, channel: str) -> dict:
		"""Join a public channel (channels:join). Private channels need an /invite."""
		return self.call("conversations.join", channel=channel)

	# ----------------------------------------------------------- files

	def file_info(self, file_id: str) -> dict:
		return self.call("files.info", file=file_id, use_json=False)

	def fetch_file(self, url: str, timeout: int = 10, max_bytes: int = 2_000_000) -> str:
		"""Download a Slack-hosted file (e.g. a voice clip's VTT transcript) as text.

		url_private requires the bot token as a Bearer header. Slack answers failed
		auth with an HTML login page instead of a 4xx, so an HTML body is an error.
		"""
		response = requests.get(
			url,
			headers={"Authorization": f"Bearer {self.token}"},
			timeout=timeout,
			allow_redirects=True,
		)

		if response.status_code != 200:
			raise SlackError(_("Slack file download failed with HTTP {0}").format(response.status_code))
		if len(response.content) > max_bytes:
			raise SlackError(_("Slack file is larger than {0} bytes").format(max_bytes))
		if response.text.lstrip().startswith("<"):
			raise SlackError(_("Slack served a login page instead of the file — check the files:read scope."))

		return response.text

	# --------------------------------------------------- users/channels

	def auth_test(self) -> dict:
		return self.call("auth.test")

	def lookup_user_by_email(self, email: str) -> dict | None:
		try:
			return self.call("users.lookupByEmail", email=email, use_json=False).get("user")
		except SlackError as e:
			if e.code in ("users_not_found", "user_not_found"):
				return None
			raise

	def open_dm(self, slack_user_id: str) -> str:
		"""Return the DM channel id for a Slack user."""
		return self.call("conversations.open", users=slack_user_id)["channel"]["id"]

	def list_conversations(self, types="public_channel,private_channel", limit=1000):
		"""Yield every conversation, following cursor pagination."""
		cursor = None
		while True:
			data = self.call(
				"conversations.list",
				types=types,
				limit=limit,
				exclude_archived=False,
				cursor=cursor,
				use_json=False,
			)
			yield from data.get("channels", [])

			cursor = (data.get("response_metadata") or {}).get("next_cursor")
			if not cursor:
				break

	def list_users(self, limit=200):
		cursor = None
		while True:
			data = self.call("users.list", limit=limit, cursor=cursor, use_json=False)
			yield from data.get("members", [])

			cursor = (data.get("response_metadata") or {}).get("next_cursor")
			if not cursor:
				break


def respond(response_url: str, payload: dict) -> None:
	"""POST to a Slack response_url (usable 5 times within 30 minutes)."""
	try:
		requests.post(response_url, json=payload, timeout=DEFAULT_TIMEOUT)
	except Exception:
		frappe.log_error(title="Slack response_url delivery failed", message=frappe.get_traceback())
