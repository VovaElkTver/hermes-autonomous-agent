"""Д7: the service-form marker (ТЗ v2, incident 28.11 form-storm).

Machinery acknowledgements (queue/pause/restart status, silence markers, redirects) must
reach the chat as ``m.notice`` — a message class peers and clients treat as background
status — while everything a human asked for stays ``m.text``. The marker is a ``str``
subclass so every existing return path keeps working unchanged; only send-capable adapters
recognize it and pick the msgtype. Nothing else may import it for branching logic:
classification belongs to the emitter, transport to the adapter.
"""
from __future__ import annotations

from typing import Any


class ServiceForm(str):
    """A machinery acknowledgement. Delivered as m.notice where the platform has the class."""

    __slots__ = ()


def service_form(text: str) -> ServiceForm:
    """Wrap ``text`` as a service form. Cheap: a str subclass instance, no allocation copy."""
    return ServiceForm(text)


def is_service_form(value: Any) -> bool:
    return isinstance(value, ServiceForm)
