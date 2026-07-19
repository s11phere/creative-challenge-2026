"""Shared Dramatiq Redis broker for the worker package.

Both ``tasks`` and ``ingestion_tasks`` import from here so all actors
are registered on—and consumed via—the same broker instance.
"""

from __future__ import annotations

import dramatiq
from infrastructure.config import settings
from infrastructure.queue import create_redis_broker

broker = create_redis_broker(settings.redis_url)
dramatiq.set_broker(broker)
