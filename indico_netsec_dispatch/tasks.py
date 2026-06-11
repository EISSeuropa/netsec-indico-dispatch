"""The out-of-request dispatch task.

Runs in Celery (never inline in a signal handler) so a slow or failing
GitHub call can never affect an Indico web request. On failure the NetSec
daily cron is the safety-net, so we retry a few times then give up rather
than holding anything.
"""

from __future__ import annotations

import requests
from flask_pluginengine import current_plugin

from indico.core.cache import make_scoped_cache
from indico.core.celery import celery


cache = make_scoped_cache('netsec-dispatch')
GITHUB_API = 'https://api.github.com'


# `plugin='netsec_dispatch'` activates the plugin context, so `current_plugin`
# resolves to NetsecDispatchPlugin and `.settings` / `.logger` are available.
@celery.task(plugin='netsec_dispatch', max_retries=3, default_retry_delay=30)
def dispatch_change(event_id):
    settings = current_plugin.settings
    # Clear the debounce key first, so an edit made right after this flush
    # re-arms a fresh dispatch window rather than being swallowed.
    cache.delete(f'pending-{event_id}')

    repo = settings.get('repo')
    token = settings.get('github_token')
    if not (repo and token):
        current_plugin.logger.warning(
            'netsec-dispatch: repo or github_token not configured; skipping event %s', event_id)
        return

    resp = requests.post(
        f'{GITHUB_API}/repos/{repo}/dispatches',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        json={
            'event_type': settings.get('event_type'),
            'client_payload': {'event_id': event_id, 'source': 'netsec-dispatch'},
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        current_plugin.logger.error(
            'netsec-dispatch: GitHub dispatch failed %s: %s', resp.status_code, resp.text[:300])
        resp.raise_for_status()  # raises → Celery retry (the daily cron is the backstop)
    current_plugin.logger.info(
        'netsec-dispatch: dispatched %r for event %s', settings.get('event_type'), event_id)
