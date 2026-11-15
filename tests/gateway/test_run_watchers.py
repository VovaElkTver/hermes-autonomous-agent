"""L1 receive-pulse sensor (deafness contour R4): the heartbeat's third token.

The sensor is a DATA map (platform -> seconds since that platform's last receive
evidence) appended by the heartbeat watcher; the out-of-band watchdog (L2 muscle)
is the only reader, and absence of evidence must never be read as death —
``unknown`` / a missing token means "unmeasurable", not "dead".
"""
import json

from gateway.config import Platform
from gateway.run_watchers import GatewaySessionWatchersMixin


class _BareRunner(GatewaySessionWatchersMixin):
    """Bare mixin host: the sensor only touches ``adapters`` / ``_failed_platforms``."""

    def __init__(self, adapters=None, failed=None):
        self.adapters = adapters or {}
        self._failed_platforms = failed or {}
        self._running = False


class _PulseAdapter:
    def __init__(self, age):
        self._age = age

    def receive_evidence_age(self):
        return self._age


def test_pulse_map_normalizes_enum_keys_to_json_safe_names():
    # Platform is a plain Enum: an un-normalized enum key would drop the WHOLE
    # map at the json.dumps boundary (silently, behind the watcher's swallow).
    runner = _BareRunner(adapters={Platform.MATRIX: _PulseAdapter(12.0)})
    mapping = runner._receive_pulse_map()
    assert mapping == {"matrix": 12.0}
    assert json.loads(runner._receive_pulse_json()) == mapping


def test_adapter_without_evidence_publishes_unknown():
    class _Plain:
        pass

    runner = _BareRunner(adapters={Platform.TELEGRAM: _Plain()})
    assert runner._receive_pulse_map() == {"telegram": "unknown"}


def test_failed_platform_is_no_adapter_and_fresh_pulse_stays_zero():
    runner = _BareRunner(
        adapters={Platform.MATRIX: _PulseAdapter(5.0)},
        failed={Platform.SLACK: "boom"},
    )
    mapping = runner._receive_pulse_map()
    assert mapping == {"matrix": 5.0, "slack": "no_adapter"}
    # The watchdog's reader splits the heartbeat line with maxsplit=2 and
    # json.loads the third token; compact separators keep the token space-free.
    line = f"1 2 {runner._receive_pulse_json()}"
    assert json.loads(line.split(" ", 2)[2]) == mapping


def test_pulse_map_is_empty_when_no_adapters():
    runner = _BareRunner()
    assert runner._receive_pulse_map() == {}
    assert json.loads(runner._receive_pulse_json()) == {}
