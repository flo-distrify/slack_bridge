# Dynamic forms — the provider contract

A **Slack Dynamic Form** lets another Frappe app serve modal forms to Slack without
either app knowing about the other. The record names three dotted method paths — the
*provider* — and slack_bridge drives the whole Slack flow:

1. A slash command (Command Route, Does: *Open Dynamic Form*) opens a modal with a
   searchable picker. Every keystroke calls `options`.
2. Picking an entry and pressing *Next* calls `schema` and swaps the modal to the
   entry's input form.
3. Submitting calls `submit` with the collected values.

All three methods run **as the mapped Frappe user** (`frappe.set_user`), so your own
permission checks apply. The coupling is a dotted path in configuration — the provider
app never imports slack_bridge and works standalone.

## The three methods

```python
def options(query: str, context: dict) -> list[dict]:
    """Choices for the picker, filtered by what the user typed."""
    return [{"value": "PD-00007", "label": "Order Hardware"}]   # value ≤ 150 chars

def schema(key: str, context: dict) -> dict:
    """The input form for one choice (key = the picked value)."""
    return {
        "title": "Order Hardware",        # modal title, ≤ 24 chars after truncation
        "submit_label": "Start",          # optional, defaults to "Submit"
        "fields": [
            {"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": 1},
            {"fieldname": "priority", "label": "Priority", "fieldtype": "Select",
             "options": "Low\nMedium\nHigh", "default": "Medium"},
        ],
    }

def submit(key: str, values: dict, context: dict) -> str:
    """Perform the action. The return value is shown to the user in Slack."""
    ...
    return "Started <https://…|PI-00042>"    # or {"message": "…"}
```

`context` is the same dict in all three calls:

| key | meaning |
|---|---|
| `user` | the mapped Frappe user (same as `frappe.session.user` during the call) |
| `slack_user_id` | the Slack user id |
| `channel_id` | the channel the command was typed in (may be None) |
| `workspace` | the Slack Workspace record name |
| `arguments` | text typed after the subcommand, truncated to 200 chars |

## Field vocabulary

Fields use Frappe's fieldtype names and render as Block Kit inputs:

| fieldtype | Slack element | value in `values` |
|---|---|---|
| `Data` (and unknown types) | single-line text | string |
| `Small Text` / `Text` / `Long Text` / `Text Editor` | multiline text | string |
| `Int` | number input (integers) | string — provider coerces |
| `Float` / `Currency` | number input (decimals) | string — provider coerces |
| `Date` | date picker | `"YYYY-MM-DD"` |
| `Check` | single checkbox | `1` or `0` |
| `Select` | static dropdown (newline `options`) | chosen option string |
| `Radio` | radio buttons (newline `options`) | chosen option string |
| `Link` | searchable picker over the DocType in `options` | docname |

Per-field keys: `fieldname` (required), `label`, `fieldtype`, `reqd`, `default`,
`options`, `description` (rendered as the input hint). Empty inputs arrive as `""`
(Check as `0`) — the provider always sees every schema key.

`Link` fields are searched live: slack_bridge queries the target doctype by name and
title field with `frappe.get_list` **as the mapped user**, so results are permission-
filtered. The schema is re-fetched on each search, so keep `schema` cheap.

## Guarantees and expectations

- **Permissions**: enforce them in the provider (`frappe.has_permission`, throwing
  API methods). slack_bridge only guarantees *who* is calling.
- **Commits**: `submit` owns its own `frappe.db.commit()`.
- **Errors**: raise `frappe.ValidationError` for user-facing problems — the message is
  shown in the modal. Optionally attach a `field_errors` dict
  (`{fieldname: message}`) to the exception to pin messages to specific inputs.
- **Time budget**: Slack allows three seconds for the submission response. If your
  submit path is slower, tick **Submit in Background** on the Slack Dynamic Form —
  the modal closes immediately and the result (or error) arrives as a DM/ephemeral.
- `options` and `schema` are called per keystroke / per view build; they must be
  read-only and fast. Failures degrade to an empty picker, never a Slack error.

## Wiring it up

1. Create a **Slack Dynamic Form**: workspace, the three method paths, picker label.
2. Create a **Slack Command Route**: e.g. command `/erp`, subcommand `process`,
   Does *Open Dynamic Form*, pointing at the record.
3. There is nothing to change in the Slack app manifest — no new scopes, and the
   slash command is already declared by the Command Route.

### Example: Distrify Processes

The distrify_processes app ships a ready-made provider (`distrify_processes/slack_forms.py`):

| field | value |
|---|---|
| Options Method | `distrify_processes.slack_forms.options` |
| Schema Method | `distrify_processes.slack_forms.schema` |
| Submit Method | `distrify_processes.slack_forms.submit` |

It lists Active process definitions with a Form start-trigger, renders their start
form, and starts a Process Instance with the submitted values.
