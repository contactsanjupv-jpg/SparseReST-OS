"""Inbound reply checker. Safe to start many times: an advisory lock means only ONE instance polls the
mailbox at a time; the others stand by and take over automatically if the active one dies.
    python -m scripts.run_inbound
"""
from __future__ import annotations

import argparse
import logging
import time

from packages.core.config import get_settings
from packages.core.inbound import build_inbound_checker, process_inbound_for_company, resolve_company_id
from packages.core.log import bind_log_context, configure_logging, get_logger, log
from packages.core.workers import LOCK_INBOUND, AdvisoryLock, GracefulShutdown, HeartbeatService, make_worker_id

_log = get_logger("inbound")


def main(argv=None, *, stop: GracefulShutdown | None = None, checker=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iterations", type=int, default=None, help="testing only")
    args = parser.parse_args(argv)

    configure_logging("inbound")
    settings = get_settings()
    stop = stop or GracefulShutdown().install()
    checker = checker or build_inbound_checker()
    if checker is None:
        log(_log, logging.ERROR, "IMAP is not configured (IMAP_HOST + credentials); inbound checking is off. Exiting.")
        return 2

    worker_id = make_worker_id("inbound")
    company_id = resolve_company_id()
    HeartbeatService.register(worker_id, "inbound")
    lock = AdvisoryLock(LOCK_INBOUND)
    iterations, handled = 0, 0
    backoff = 0.0
    with bind_log_context(worker_id=worker_id):
        log(_log, logging.INFO, "inbound checker started")
        try:
            while not stop.requested and (args.max_iterations is None or iterations < args.max_iterations):
                iterations += 1
                if lock.acquire():
                    result = process_inbound_for_company(company_id, checker)
                    handled += len(result.task_ids)
                    # Back off while the mailbox is failing so a bad password isn't retried (and an Incident
                    # raised) every few seconds.
                    backoff = min(max(backoff * 2, settings.inbound_poll_interval_seconds), 900.0) if result.failed else 0.0
                HeartbeatService.beat(worker_id, handled)
                stop.wait(settings.inbound_poll_interval_seconds + backoff)
        finally:
            HeartbeatService.beat(worker_id, handled, status="stopped")
            lock.release()
            log(_log, logging.INFO, "inbound checker stopped", replies_queued=handled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
