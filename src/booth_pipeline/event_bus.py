"""Event triggers' side of the event bus (ADR 0123): one shared durable JetStream consumer.

Every replica pulls from the same durable consumer, ``booth-pipeline-triggers``, on the shared
``BOOTH_EVENTS`` stream (ADR 0026), filtered to ``booth.*.storage.object.created`` and
``booth.*.api.write.committed``; JetStream shares the messages out. Each message is handled in one
database transaction (``TriggerIngest`` → ``store.ingest_trigger_event``) and acked only after it
commits. Nothing about a burst is kept in memory: the scheduler fires it from the database.

* **Duplicates.** The stream sequence is recorded with the event; a redelivery (an ack lost after
  the commit, a replica that died mid-batch) is acked and does nothing.
* **First start.** The consumer is created delivering only new events: turning this on must not
  replay the stream's week of history into new triggers.
* **Lost consumer.** If the consumer has gone but this module had one (the ``trigger_bus`` row), it
  is recreated from the last time any replica was reading, so the gap is replayed, not skipped.
* **Lost events.** If no replica has read for longer than the stream keeps events, some may be gone:
  every enabled event trigger fires once (ADR 0123 revision), and its status line says why.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .store.base import Store
from .triggers import PENDING, SUBJECTS, BadEvent, parse_event

log = logging.getLogger(__name__)

STREAM = "BOOTH_EVENTS"
DURABLE = "booth-pipeline-triggers"
FETCH_BATCH = 100
# While connected, how often a replica records that it is reading (and how stale that record may get
# before a restart counts the silence against the stream's retention).
CONTACT_SECONDS = 30
# A recreated consumer replays from a little before the last recorded contact: anything seen twice is
# a recorded sequence and does nothing.
REPLAY_MARGIN = timedelta(seconds=2 * CONTACT_SECONDS)
PRUNE_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(UTC)


class TriggerIngest:
    """One message off the bus into the store. No NATS here, so tests feed it directly."""

    def __init__(self, store: Store, wake: Callable[[], None] | None = None) -> None:
        self._store = store
        self._wake = wake
        self.bad = 0  # malformed events dropped (logged), for the status line and tests

    def handle(self, subject: str, body: bytes, seq: int | None, now: datetime | None = None) -> dict[str, str] | None:
        """Returns what each matching trigger did with it, None for a redelivery, {} for no match.
        Raises only when the store fails, so the caller naks and the message comes back."""
        try:
            ev = parse_event(subject, body)
        except BadEvent as e:
            self.bad += 1
            log.warning("event trigger: dropped an event on %s: %s", subject, e)
            return {}
        out = self._store.ingest_trigger_event(ev, seq, now or _now())
        if out and PENDING in out.values() and self._wake is not None:
            self._wake()
        return out


class EventBus:
    """The consumer loop, on its own thread with its own asyncio loop. Reconnects forever."""

    def __init__(self, cfg: Config, store: Store, ingest: TriggerIngest, *, stream: str = STREAM, durable: str = DURABLE) -> None:
        self._cfg = cfg
        self._store = store
        self._ingest = ingest
        self._stream = stream
        self._durable = durable
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self.connected = False
        self.last_error = ""
        self.ready = threading.Event()  # set once the consumer exists and is being read (tests wait on it)

    @property
    def configured(self) -> bool:
        return bool(self._cfg.event_bus_url)

    def start(self) -> None:
        if not self.configured:
            log.info("event triggers: no event-bus credential, so no consumer (event triggers can't be turned on)")
            return
        self._thread = threading.Thread(target=self._run, name="event-bus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._stopping is not None:
            self._loop.call_soon_threadsafe(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        self._stopping = asyncio.Event()
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._session()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - the bus or the database is away: retry, never give up
                self.connected = False
                self.last_error = f"{e.__class__.__name__}: {e}"
                log.warning("event triggers: bus session ended (%s); retrying in %.0fs", self.last_error, backoff)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        import nats
        from nats.errors import TimeoutError as NatsTimeout

        assert self._stopping is not None
        opts: dict[str, Any] = {"servers": [self._cfg.event_bus_url], "max_reconnect_attempts": -1, "name": "booth-pipeline"}
        if self._cfg.event_bus_creds_file:
            opts["user_credentials"] = self._cfg.event_bus_creds_file
        nc = await nats.connect(**opts)
        try:
            js = nc.jetstream()
            await self._ensure_consumer(js)
            sub = await js.pull_subscribe_bind(durable=self._durable, stream=self._stream)
            self.connected, self.last_error = True, ""
            self.ready.set()
            log.info("event triggers: reading %s as durable consumer %r", ", ".join(SUBJECTS), self._durable)
            loop = asyncio.get_running_loop()
            last_contact = last_prune = loop.time()
            while not self._stopping.is_set():
                try:
                    msgs = await sub.fetch(FETCH_BATCH, timeout=2)
                except NatsTimeout:
                    msgs = []
                for msg in msgs:
                    seq = msg.metadata.sequence.stream
                    try:
                        await asyncio.to_thread(self._ingest.handle, msg.subject, msg.data, seq)
                    except Exception:  # noqa: BLE001 - the database is away: let JetStream redeliver it
                        log.exception("event triggers: could not record event %s; it will be redelivered", seq)
                        await msg.nak(delay=5)
                        continue
                    await msg.ack()
                if loop.time() - last_contact >= CONTACT_SECONDS:
                    await asyncio.to_thread(self._store.record_trigger_bus_contact, _now())
                    last_contact = loop.time()
                if loop.time() - last_prune >= PRUNE_SECONDS:
                    await asyncio.to_thread(self._prune)
                    last_prune = loop.time()
        finally:
            self.connected = False
            await nc.close()

    def _prune(self) -> None:
        self._store.prune_seen_trigger_events(_now() - timedelta(days=8))

    async def _ensure_consumer(self, js) -> None:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        from nats.js.errors import NotFoundError

        info = await js.stream_info(self._stream)
        max_age = float(info.config.max_age or 0)  # seconds; 0 = kept until limits
        now = _now()
        bus = await asyncio.to_thread(self._store.trigger_bus)
        if bus is not None and max_age > 0 and (now - bus[1]).total_seconds() > max_age:
            # Nobody read for longer than the stream keeps events: some may be gone for good.
            n = await asyncio.to_thread(self._store.event_triggers_missed_events, now)
            log.warning("event triggers: no replica read the bus for %s (the stream keeps %ss); %d trigger(s) fire once", now - bus[1], max_age, n)
        try:
            await js.consumer_info(self._stream, self._durable)
        except NotFoundError:
            cfg = ConsumerConfig(
                durable_name=self._durable,
                filter_subjects=list(SUBJECTS),
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=float(self._cfg.events_ack_wait_seconds),
                max_ack_pending=self._cfg.events_max_ack_pending,
                deliver_policy=DeliverPolicy.NEW,
            )
            if bus is not None:
                # It existed before (this module recorded reading it): replay what it would have had.
                cfg.deliver_policy = DeliverPolicy.BY_START_TIME
                cfg.opt_start_time = (bus[1] - REPLAY_MARGIN).isoformat().replace("+00:00", "Z")
                log.warning("event triggers: consumer %r was gone; recreating it from %s", self._durable, cfg.opt_start_time)
            await js.add_consumer(self._stream, config=cfg)
        await asyncio.to_thread(self._store.record_trigger_bus_contact, now)
