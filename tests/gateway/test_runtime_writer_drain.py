"""Real startup threads retain runtime exclusion after a drain deadline."""
import asyncio
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.platforms("linux")
@pytest.mark.parametrize('partial_start', [False, True])
def test_live_runtime_writers_prevent_final_lock_release(tmp_path, monkeypatch, partial_start):
    from gateway import run
    from gateway.run_bootstrap import _start_gateway_periodic_threads
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock

    home = tmp_path / 'runtime'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    gate = threading.Event()
    started = threading.Event()
    threads = []

    def fake_housekeeping(*a, **kw):
        started.set()
        gate.wait(20)

    monkeypatch.setattr(run, '_start_gateway_housekeeping', fake_housekeeping)
    original_start = threading.Thread.start

    def start_thread(thread):
        if thread.name == 'gateway-housekeeping' and partial_start:
            raise RuntimeError('owned thread-start failure')
        threads.append(thread)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, 'start', start_thread)
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False), adapters={})

    async def start():
        return _start_gateway_periodic_threads(runner)

    probe = '''
from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock
claimed = acquire_gateway_runtime_lock()
print('CLAIMED', claimed)
if claimed:
    release_gateway_runtime_lock()
'''

    def contender():
        result = subprocess.run([sys.executable, '-c', probe], env=os.environ.copy(),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    locked = acquire_gateway_runtime_lock()
    assert locked
    released = not locked
    try:
        if partial_start:
            with pytest.raises(RuntimeError, match='owned thread-start failure'):
                asyncio.run(start())
            # A failed owned-writer start never produced a live runtime writer.
        else:
            asyncio.run(start())
            assert started.wait(5)
            assert contender() == 'CLAIMED False'
            release_gateway_runtime_lock()
            released = True
            assert contender() == 'CLAIMED False', 'final cleanup unlocked while a runtime writer survived'
    finally:
        gate.set()
        for thread in threads:
            if thread.name.startswith('hermes-events-'):
                # Third-party processor threads owned by hermes_events; their
                # stop is an orderly-shutdown concern, not a runtime-writer
                # one. Lock-release coverage lives in the final probe below.
                continue
            thread.join(5)
            assert not thread.is_alive()
        if not released:
            release_gateway_runtime_lock()
    # The last writer unregisters asynchronously with its thread-death bookkeeping;
    # give it a bounded window instead of racing a single probe.
    import time as _time
    deadline = _time.time() + 10
    while _time.time() < deadline:
        if acquire_gateway_runtime_lock():
            release_gateway_runtime_lock()
            break
        _time.sleep(0.2)
    else:
        pytest.fail('runtime lock never became available after all writers stopped')
    assert contender() == 'CLAIMED True'
