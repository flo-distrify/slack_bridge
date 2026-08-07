# Frappe Marketplace submission checklist

What the Frappe Cloud review looks for, and where this app stands. Sources are the
Frappe Cloud marketplace guidelines, app authoring guidelines and app versioning docs.

## Hard requirements

| Requirement | Status |
| --- | --- |
| Open source licence (MIT or GPL-compatible) | Done — AGPL-3.0 in `license.txt` |
| Hosted on GitHub, owned by the publisher account | Done — `github.com/flo-distrify/slack_bridge` |
| Unique app name on the marketplace | **To verify** before first submission |
| `pyproject.toml` at repo root with `[tool.bench.frappe-dependencies]` | Done — `frappe = ">=15.0.0,<17.0.0-dev"` |
| Branch per Frappe major (`version-15`, `version-16`) | Done — both branches live and release-synced via cherry-pick from `develop` |
| Passing GitHub Actions CI | Done — `.github/workflows/ci.yml` runs the suite on v15 and v16 |
| Semgrep clean against `frappe/semgrep-rules` | Workflow present (`linter.yml`); run before submitting |
| Does not override framework auth routes | Done — extends only via hooks |
| Settings doctype for global configuration | Done — `Slack Bridge Settings` |
| Secrets stored encrypted | Done — bot token and signing secret are `Password` fields |

## Listing assets

| Asset | Status |
| --- | --- |
| Logo, square, ≥ 200×200, no text | Placeholder at `slack_bridge/public/images/logo.svg` — **replace with a designed PNG** |
| Short description, 40–80 characters | **To do** — suggestion: "Two-way Slack connector for Frappe and ERPNext" (46) |
| Long description (features and usage, no install steps) | Draft available in `README.md` |
| Screenshots | **To do** — capture a rule, an approval message, a modal, an unfurl |
| Support URL | **To do** — GitHub issues URL is acceptable |
| Privacy policy URL | **To do** — required, must be reachable |
| Demo video for reviewers | **To do** — the approval loop is the strongest 60-second demo |

## Pricing decisions to make before listing

- Up to three plans, monthly recurring only, billed pro-rata, USD and INR.
- Commission: none until the first $500, then 80/20 in the publisher's favour.
- Frappe Cloud does **not** enforce plan-based feature gating. If plans differ by feature,
  the app must check its own subscription; otherwise keep one plan and avoid the problem.

## Slack app scopes to justify in the listing

Slack (and attentive reviewers) ask why each scope is requested. The manifest is
generated per customer, so these apply to every install:

| Scope | Why |
| --- | --- |
| `files:read` | Reads Slack's native transcript of voice clips for the comm-log shortcut |
| `channels:join` | Lets the bot join public channels to place the synced-✅ reaction |
| `reactions:write` | The synced-✅ marker on logged messages |
| remaining scopes | Core messaging/commands/unfurls, unchanged since the first release |

## Cleanup before submission

- [ ] Genericise or exclude the local bench-execute helpers `sbsetup.py`, `sbrule.py`,
      `sbinspect.py` — they hardcode the "Distrify" workspace name, `/erp` and an
      internal test address. They never run as part of the app, but they are Distrify
      residue in an otherwise tenant-neutral codebase (verified: shipped code touches
      only framework doctypes; the single business doctype written is `Communication`).

## Notes that shaped the build

- Guest endpoints and outbound HTTP are permitted on Frappe Cloud, so the Slack Request
  URLs work on a hosted site with no special handling. CSRF does not block cookie-less
  server-to-server POSTs.
- No sidecar processes are allowed; everything runs in the web, worker and scheduler
  processes, which is why delivery is an outbox drained by a cron job rather than a
  long-running consumer.
- The app deliberately has no third-party Python dependencies, which removes a common
  source of install and review friction.
