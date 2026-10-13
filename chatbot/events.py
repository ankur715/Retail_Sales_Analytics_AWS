"""Listen for load_completed events (milestone 3) and retire cached answers at once.

Without events, a replica notices new data when its once-a-minute data-version lookup runs
(chatbot/cache.py). With EVENTS_BOOTSTRAP set, a background thread consumes
EVENTS_TOPIC and calls cache.invalidate() on every load_completed, so the next question
re-reads the version and misses the old answers.

Every replica must hear every event (each keeps its own version in memory), so each uses
its own consumer group and starts from the latest offset: a replica that was down doesn't
need old events -- it looks the version up fresh when it starts.

The listener never takes the service down: a broken broker, a bad message or a crash in the
loop is logged and counted (chatbot_events_total{result}), and the 60 s fallback still works.
"""
import json
import logging
import socket
import threading
import uuid

from chatbot import cache, telemetry
from etl import config

log = logging.getLogger(__name__)
EVENT = "load_completed"
_thread: threading.Thread | None = None
_stop = threading.Event()


def handle(raw: bytes | None) -> str:
    """Act on one message; returns what happened (also the metric label)."""
    try:
        event = json.loads(raw or b"")
    except ValueError:
        return "malformed"
    if not isinstance(event, dict) or event.get("event") != EVENT:
        return "ignored"
    cache.invalidate()
    log.info("load_completed for %s (load %s): cached answers retired", event.get("pipeline_id"),
             event.get("load_id"))
    return "invalidated"


def consumer():
    from confluent_kafka import Consumer
    return Consumer({"bootstrap.servers": config.EVENTS_BOOTSTRAP,
                     "group.id": f"chatbot-{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
                     "auto.offset.reset": "latest", "enable.auto.commit": True,
                     "allow.auto.create.topics": True,                # the topic may not exist before the first load
                     "topic.metadata.refresh.interval.ms": 10000})   # ...so notice it soon once it does


def _run() -> None:
    c = None
    while not _stop.is_set():
        try:
            if c is None:
                c = consumer()
                c.subscribe([config.EVENTS_TOPIC])
            msg = c.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                telemetry.EVENTS.labels(EVENT, "broker_error").inc()
                log.warning("event consumer: %s", msg.error())
                continue
            telemetry.EVENTS.labels(EVENT, handle(msg.value())).inc()
        except Exception:
            telemetry.EVENTS.labels(EVENT, "consumer_crash").inc()
            log.exception("event consumer failed; reconnecting in 5 s (the 60 s version check still applies)")
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
                c = None
            _stop.wait(5)
    if c is not None:
        c.close()


def start() -> bool:
    """Start the listener thread if events are configured; True if started."""
    global _thread
    if not config.EVENTS_BOOTSTRAP or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_run, name="load-events", daemon=True)
    _thread.start()
    log.info("listening for %s on %s", EVENT, config.EVENTS_TOPIC)
    return True


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
