"""Describe service signals without asserting predictive accuracy."""
import signals
from store import transaction


def observe(service):
    with transaction() as q:
        return signals.snapshot(q)[service]
