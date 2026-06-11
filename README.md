# netsec-dispatch — Indico plugin

Fires a GitHub `repository_dispatch` when the watched EISS Indico programme
changes, so the NetSec static site (`netsec-cost.eu`) refreshes within about
a minute instead of waiting for the daily polling sync. **Read-only toward
Indico** — it never writes to the instance.

Phase 1 of [`docs/indico-integration.md`](https://github.com/EISSeuropa/netsec.github.io/blob/main/docs/indico-integration.md),
tracked in [#823](https://github.com/EISSeuropa/netsec.github.io/issues/823).

> This is the dedicated repository for the plugin (split out of
> `EISSeuropa/netsec.github.io` so the static-site Pages deploy never
> publishes plugin source). Deploy it to the Indico VPS by cloning this
> repo and `pip install -e .` into the Indico virtualenv.

## How it works

```
Indico signal (times_changed, contribution_*, session_*, event.updated)
  → plugin handler (only for events in/under the watched category)
  → debounced Celery task (out-of-request)
  → POST /repos/<repo>/dispatches to GitHub
  → sync-indico.yml runs on: repository_dispatch
  → the existing read-only sync → PR → auto-merge → Pages deploy
```

The daily cron stays as the safety-net, so a missed dispatch still resolves
within 24h, and the workflow's `concurrency: cancel-in-progress` collapses
any dispatches that slip past the debounce.

## Install on the VPS

```bash
source /opt/indico/.venv/bin/activate          # the Indico virtualenv
pip install -e /opt/indico/plugins/netsec-dispatch
```

Enable it in `indico.conf`:

```python
PLUGINS = {'netsec_dispatch', *PLUGINS}         # keep your existing entries
```

Reload and restart the worker (signals + tasks register at startup):

```bash
touch /opt/indico/web/indico.wsgi               # reload uWSGI
systemctl restart indico-celery
```

No database step is needed — the plugin defines no models.

## Configure

Administration → Plugins → **NetSec Dispatch**:

| Setting | Value |
| --- | --- |
| Enabled | on (after the token is set) |
| GitHub repository | `EISSeuropa/netsec.github.io` |
| Dispatch event_type | `indico-changed` |
| GitHub token | a fine-grained PAT (see below) |
| Watched category id | `1` (Annual Conferences) |
| Debounce (seconds) | `90` |

### The GitHub token

Create a **fine-grained PAT** scoped to **only** `EISSeuropa/netsec.github.io`,
with the minimum repository permission GitHub requires to create a
`repository_dispatch` event (Contents: read and write — confirm against
current GitHub docs). Nothing else. Paste it into the plugin setting; it is
stored in Indico, never in the repo.

### The companion workflow change (repo side)

Add `repository_dispatch` to `.github/workflows/sync-indico.yml`:

```yaml
on:
  schedule:
    - cron: "45 3 * * *"
  workflow_dispatch:
  repository_dispatch:
    types: [indico-changed]      # must equal the plugin's event_type
```

Harmless to land before the plugin is deployed (nothing fires it yet).
`repository_dispatch` only triggers workflows on the default branch, and
this workflow is on `main`, so it works as-is.

## Test plan (use a throwaway event, never live ESSC)

1. Create a **test event** in category 1 (or point `watched_category_id` at a
   dedicated test category first).
2. Retime a session → check the Indico/Celery logs show the signal and one
   queued `dispatch_change` task.
3. Confirm a `repository_dispatch` run of `sync-indico.yml` appears in the
   Actions tab and produces the usual sync PR.
4. Burst-edit (move several contributions) → confirm **one** dispatch, not
   many (debounce).
5. Edit an event in an **unwatched** category → confirm **no** dispatch.
6. Revoke the PAT briefly → confirm the task fails and retries cleanly and
   Indico itself is unaffected (the daily cron still refreshes the site).

## Two things to validate against your Indico version

The scaffold is faithful to the 3.3 plugin idioms, but two details depend on
the exact installed build and should be confirmed during the test plan:

1. **The signal-to-event resolver** in `plugin.py` (`_resolve_event`): the
   object each signal carries varies. Watch the logs on the test event and
   tighten the candidate list per signal if any does not resolve.
2. **`from indico.core.cache import make_scoped_cache`**: confirm this import
   path. If it has moved, adjust the two imports (`plugin.py`, `tasks.py`).

## Rollback

Remove `netsec_dispatch` from `PLUGINS` in `indico.conf`, reload uWSGI. The
daily cron keeps the site fresh. Zero static-site risk throughout.
