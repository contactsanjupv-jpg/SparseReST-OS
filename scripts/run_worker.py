"""Long-lived task worker. Run any number in parallel (FOR UPDATE SKIP LOCKED makes that safe).
Exactly one of them (whoever wins an advisory lock) also runs periodic maintenance.
    python -m scripts.run_worker [--product-slug xflow]
"""
from __future__ import annotations

import argparse
import logging
import time

from packages.core.config import get_settings
from packages.core.log import bind_log_context, configure_logging, get_logger, log
from packages.core.maintenance import run_maintenance
from packages.core.orchestrator import run_worker_loop
from packages.core.workers import LOCK_MAINTENANCE, AdvisoryLock, GracefulShutdown, HeartbeatService, make_worker_id

_log = get_logger("worker")


def main(argv=None, *, stop: GracefulShutdown | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-slug", default=None, help="shard this worker to one product's queue")
    parser.add_argument("--max-iterations", type=int, default=None, help="testing only")
    args = parser.parse_args(argv)

    configure_logging("worker")
    settings = get_settings()
    stop = stop or GracefulShutdown().install()
    worker_id = make_worker_id("worker")

    product_id = None
    if args.product_slug:
        from packages.db.models import Product
        from packages.db.session import session_scope
        with session_scope() as session:
            product = session.query(Product).filter_by(slug=args.product_slug).one()
            product_id = product.id

    HeartbeatService.register(worker_id, "worker")
    lock = AdvisoryLock(LOCK_MAINTENANCE)
    state = {"last_beat": 0.0, "last_maint": 0.0}

    def on_cycle(processed: int) -> None:
        now = time.monotonic()
        if now - state["last_beat"] >= settings.worker_heartbeat_interval_seconds:
            HeartbeatService.beat(worker_id, processed)
            state["last_beat"] = now
        if now - state["last_maint"] >= settings.maintenance_interval_seconds:
            state["last_maint"] = now
            if lock.acquire():                      # only one worker holds this; others skip silently
                run_maintenance()

    with bind_log_context(worker_id=worker_id):
        log(_log, logging.INFO, "worker started", product=args.product_slug)
        processed = 0
        try:
            processed = run_worker_loop(
                worker_id, poll_interval_seconds=settings.worker_poll_interval_seconds, product_id=product_id,
                max_iterations=args.max_iterations, should_stop=lambda: stop.requested,
                sleep_fn=stop.wait, on_cycle=on_cycle,
            )
        finally:
            HeartbeatService.beat(worker_id, processed, status="stopped")
            lock.release()
            log(_log, logging.INFO, "worker stopped", tasks_processed=processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
