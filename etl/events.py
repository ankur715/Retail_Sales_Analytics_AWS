"""load_completed events (milestone 3): after a load is marked SUCCESS, tell anyone who
caches its data. The chatbot listens and retires its cached answers at once, instead of
noticing the new data version within a minute (chatbot/events.py).

Topic EVENTS_TOPIC (default retail.load_completed) on Redpanda, which speaks the Kafka API.
Keyed by pipeline_id, so one feed's events stay in order. The value is JSON:

    {"event": "load_completed", "version": 1, "load_id": ..., "pipeline_id": ..., "client_id": ...,
     "retailer_id": ..., "load_type": ..., "window_start": ..., "window_end": ..., "finished_at": ...}

Send one by hand (the chaos/demo scripts run this inside the cluster):
    python -m etl.events --pipeline C101_R201 --load-id demo-1

Like the failure email, publish_load_completed() never raises: the load already succeeded,
and a missed event only means caches catch up on their own schedule. Off unless
EVENTS_BOOTSTRAP is set, so CI, tests and the laptop never connect.
"""
import json
import logging
from datetime import datetime, timezone

from etl import config

log = logging.getLogger(__name__)
EVENT = "load_completed"
SCHEMA_VERSION = 1
FLUSH_TIMEOUT_S = 10


def build(ctx, finished_at: datetime | None = None) -> dict:
    return {"event": EVENT, "version": SCHEMA_VERSION, "load_id": ctx.load_id, "pipeline_id": ctx.pipeline_id,
            "client_id": ctx.client_id, "retailer_id": ctx.retailer_id, "load_type": ctx.load_type,
            "window_start": ctx.window_start.isoformat(), "window_end": ctx.window_end.isoformat(),
            "finished_at": (finished_at or datetime.now(timezone.utc)).isoformat()}


def producer():
    from confluent_kafka import Producer
    return Producer({"bootstrap.servers": config.EVENTS_BOOTSTRAP, "acks": "all", "enable.idempotence": True,
                     "message.timeout.ms": FLUSH_TIMEOUT_S * 1000, "client.id": "retail-etl"})


def publish(event: dict, key: str) -> bool:
    """Send one event and wait for the broker to acknowledge it; True if it did. Never raises."""
    if not config.EVENTS_BOOTSTRAP:
        log.info("%s event skipped: EVENTS_BOOTSTRAP not set", event.get("event"))
        return False
    delivered = []
    try:
        p = producer()
        p.produce(config.EVENTS_TOPIC, key=key, value=json.dumps(event),
                  on_delivery=lambda err, msg: delivered.append(err is None))
        p.flush(FLUSH_TIMEOUT_S)
    except Exception:
        log.exception("%s event could not be published; the load itself succeeded", event.get("event"))
        return False
    if delivered != [True]:
        log.error("%s event for %s was not acknowledged within %s s", event.get("event"), key, FLUSH_TIMEOUT_S)
        return False
    log.info("published %s for %s to %s", event["event"], key, config.EVENTS_TOPIC)
    return True


def publish_load_completed(ctx) -> bool:
    return publish(build(ctx), key=ctx.pipeline_id)


def main(argv=None) -> int:
    import argparse
    from datetime import date, timedelta
    from types import SimpleNamespace

    parser = argparse.ArgumentParser(description="Publish one load_completed event")
    parser.add_argument("--pipeline", default="C101_R201")
    parser.add_argument("--load-id", default="manual")
    args = parser.parse_args(argv)
    client_id, retailer_id = args.pipeline.split("_")
    end = date.today()
    ctx = SimpleNamespace(load_id=args.load_id, pipeline_id=args.pipeline, client_id=client_id,
                          retailer_id=retailer_id, load_type="manual", window_start=end - timedelta(weeks=5),
                          window_end=end)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return 0 if publish_load_completed(ctx) else 1


if __name__ == "__main__":
    raise SystemExit(main())
