# Installing on this dev server

The `distrify` bench's virtualenv (`/root/frappe/distrify/env`) is owned by `root`, so
the usual editable install fails for non-root users. Either of the following works.

## Option A — proper editable install (needs root once)

```bash
sudo uv pip install -e /root/frappe/distrify/apps/slack_bridge \
  --python /root/frappe/distrify/env/bin/python

echo slack_bridge >> /root/frappe/distrify/sites/apps.txt
cd /root/frappe/distrify
bench --site distrify.localhost install-app slack_bridge
```

Afterwards `/root/frappe/distrify/.env` is redundant and can be deleted.

## Option B — no root

`/root/frappe/distrify/.env` already sets `PYTHONPATH` to the app directory, and honcho
reads that file when `bench start` launches the bench, so every web, worker and scheduler
process can import the app.

```bash
echo slack_bridge >> /root/frappe/distrify/sites/apps.txt
cd /root/frappe/distrify
PYTHONPATH=/root/frappe/distrify/apps/slack_bridge \
  bench --site distrify.localhost install-app slack_bridge
```

Then restart the bench so the running processes pick up the `.env`:

```bash
sudo frappe-manage start distrify   # note: this stops other running instances
```

**Do not install the app on a site without doing one of these first.** Frappe imports
every installed app on each request, so an app that is listed in the site's installed
apps but is not importable makes the whole site return HTTP 500.

## Running the tests

Tests do not need the app installed on a site, only importable:

```bash
cd /root/frappe/distrify
PYTHONPATH=/root/frappe/distrify/apps/slack_bridge \
  bench --site distrify.localhost run-tests --app slack_bridge
```
