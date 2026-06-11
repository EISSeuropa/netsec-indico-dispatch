"""NetSec Dispatch plugin.

Subscribes to the EISS Indico instance's programme-change signals and, on a
change to a watched event, fires a GitHub ``repository_dispatch`` so the
NetSec static-site sync workflow runs within about a minute. Read-only
toward Indico: it never writes to the instance.

Phase 1 of docs/indico-integration.md (issue #823). The companion repo-side
change is adding ``repository_dispatch`` to ``.github/workflows/sync-indico.yml``
``on:`` triggers (see README).
"""

from __future__ import annotations

from wtforms.fields import BooleanField, IntegerField, StringField
from wtforms.validators import DataRequired, NumberRange, Optional

from indico.core import signals
from indico.core.cache import make_scoped_cache
from indico.core.plugins import IndicoPlugin
from indico.web.forms.base import IndicoForm
from indico.web.forms.fields import IndicoPasswordField
from indico.web.forms.widgets import SwitchWidget

from indico_netsec_dispatch.tasks import dispatch_change


# A scoped cache (Redis-backed in production) is the debounce store. It needs
# no DB model and no migration, unlike a plugin table (see #823 for why we
# avoid one). VALIDATE this import path against your installed Indico version.
cache = make_scoped_cache('netsec-dispatch')


class SettingsForm(IndicoForm):
    enabled = BooleanField(
        'Enabled', widget=SwitchWidget(),
        description='Fire a GitHub repository_dispatch when a watched event changes.')
    repo = StringField(
        'GitHub repository', [DataRequired()],
        description='owner/name, e.g. EISSeuropa/netsec.github.io')
    event_type = StringField(
        'Dispatch event_type', [DataRequired()],
        description="Must match `on: repository_dispatch: types: [...]` in the workflow.")
    github_token = IndicoPasswordField(
        'GitHub token', [Optional()], toggle=True,
        description='Fine-grained PAT scoped to the one repo, with permission to '
                    'create repository_dispatch events. Stored here in Indico, '
                    'never committed to the repo.')
    watched_category_id = IntegerField(
        'Watched category id', [DataRequired(), NumberRange(min=0)],
        description='Only events in (or under) this category trigger a dispatch. '
                    '1 = Annual Conferences on the EISS instance.')
    debounce_seconds = IntegerField(
        'Debounce (seconds)', [DataRequired(), NumberRange(min=0)],
        description='Coalesce a burst of edits into a single dispatch. 0 disables.')


class NetsecDispatchPlugin(IndicoPlugin):
    """NetSec Dispatch

    Fires a GitHub repository_dispatch when the watched Indico programme
    changes, so the NetSec static site refreshes within about a minute
    instead of waiting for the daily polling sync. Read-only toward Indico.
    """

    configurable = True
    settings_form = SettingsForm
    default_settings = {
        'enabled': False,
        'repo': 'EISSeuropa/netsec.github.io',
        'event_type': 'indico-changed',
        'github_token': '',
        'watched_category_id': 1,
        'debounce_seconds': 90,
    }

    def init(self):
        super().init()
        # Every receiver accepts **kwargs (the documented forward-compat rule).
        # The signal set covers the changes that move the public programme.
        for signal in (
            signals.event.times_changed,
            signals.event.timetable_entry_updated,
            signals.event.contribution_created,
            signals.event.contribution_updated,
            signals.event.contribution_deleted,
            signals.event.session_updated,
            signals.event.session_deleted,
            signals.event.updated,
        ):
            self.connect(signal, self._on_change)

    # ── signal handling ──

    def _on_change(self, sender, **kwargs):
        if not self.settings.get('enabled'):
            return
        event = self._resolve_event(sender, kwargs)
        if event is None or not self._is_watched(event):
            return
        self._schedule(event.id)

    @staticmethod
    def _resolve_event(sender, kwargs):
        """Find the Event a signal pertains to.

        The object a signal carries varies (a contribution, a session, a
        timetable entry, or the event itself), passed as the sender or in
        kwargs; each non-event object exposes ``.event``. VALIDATE against
        your Indico version with a test event (README test plan) and tighten
        per-signal if any differs.
        """
        candidates = (
            kwargs.get('obj'), kwargs.get('contribution'), kwargs.get('session'),
            kwargs.get('entry'), kwargs.get('event'), sender,
        )
        for cand in candidates:
            if cand is None:
                continue
            if cand.__class__.__name__ == 'Event':
                return cand
            event = getattr(cand, 'event', None)
            if event is not None:
                return event
        return None

    def _is_watched(self, event):
        watched = self.settings.get('watched_category_id')
        # Event.category_chain is the list of category ids from the root down
        # to and including the event's own category, so a watch on a parent
        # category catches events in its sub-categories too. (Verified against
        # Indico v3.3.12: the attribute is `category_chain`, NOT
        # `category_chain_ids` — that name exists only on the Category model as
        # `Category.chain_ids`.) Falls back to the immediate category only if
        # the chain is unavailable (e.g. an event not filed under a category).
        chain = getattr(event, 'category_chain', None)
        if not chain:
            cid = getattr(event, 'category_id', None)
            chain = [cid] if cid is not None else []
        return watched in chain

    def _schedule(self, event_id):
        debounce = int(self.settings.get('debounce_seconds') or 0)
        key = f'pending-{event_id}'
        if debounce and cache.get(key):
            return  # a dispatch is already queued for this event in this window
        cache.set(key, True, timeout=debounce + 5)
        # The network call happens out-of-request in Celery so a GitHub outage
        # can never slow or break an Indico web request.
        dispatch_change.apply_async(args=[event_id], countdown=debounce)
