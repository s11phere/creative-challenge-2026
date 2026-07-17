"""Dramatiq broker construction."""

from dramatiq.brokers.redis import RedisBroker


def create_redis_broker(redis_url: str) -> RedisBroker:
    """Create a lazy Redis broker using Dramatiq's standard middleware."""
    return RedisBroker(url=redis_url)  # type: ignore[no-untyped-call]
