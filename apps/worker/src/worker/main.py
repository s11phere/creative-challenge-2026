"""Dramatiq Worker entry point."""

import logging

from dramatiq.cli import main as dramatiq_main
from dramatiq.cli import make_argument_parser
from infrastructure.config import settings

logger = logging.getLogger(__name__)


def main() -> None:
    """Start a bounded Worker process with signal-aware graceful shutdown."""
    settings.validate_secrets()
    logger.info("worker_starting")
    args = make_argument_parser().parse_args(  # type: ignore[no-untyped-call]
        [
            "--processes",
            str(settings.worker_processes),
            "--threads",
            str(settings.worker_threads),
            "--worker-shutdown-timeout",
            str(settings.worker_shutdown_timeout_ms),
            "worker.tasks:broker",
        ]
    )
    exit_code = dramatiq_main(args)  # type: ignore[no-untyped-call]
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
