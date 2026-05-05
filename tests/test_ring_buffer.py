import threading
import time
import uuid

from incidentary.ring_buffer import RingBuffer
from incidentary.types import SkeletonCe


def make_ce(wall_ts_ns: int | None = None) -> SkeletonCe:
    return SkeletonCe(
        id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        parent_id=None,
        service_id="svc",
        occurred_at=wall_ts_ns if wall_ts_ns is not None else int(time.time() * 1_000_000_000),
        kind="HTTP_SERVER",
        status_code=200,
        duration_ns=1_000,
    )


def test_write_and_size():
    buf = RingBuffer(capacity=3)
    buf.write(make_ce())
    buf.write(make_ce())
    assert buf.size == 2


def test_circular_overwrite_keeps_capacity():
    buf = RingBuffer(capacity=2)
    buf.write(make_ce())
    buf.write(make_ce())
    buf.write(make_ce())
    assert buf.size == 2


def test_flush_respects_cutoff_and_clears():
    now_ns = int(time.time() * 1_000_000_000)
    old_ns = now_ns - 70_000 * 1_000_000
    recent_ns = now_ns - 500 * 1_000_000

    buf = RingBuffer(capacity=3, window_ms=60_000)
    buf.write(make_ce(wall_ts_ns=old_ns))
    buf.write(make_ce(wall_ts_ns=recent_ns))

    flushed = buf.flush()
    assert len(flushed) == 1
    assert flushed[0].occurred_at == recent_ns
    assert buf.size == 0


def test_flush_returns_sorted_by_wall_ts():
    base = int(time.time() * 1_000_000_000)
    ce1 = make_ce(wall_ts_ns=base + 3)
    ce2 = make_ce(wall_ts_ns=base + 1)
    ce3 = make_ce(wall_ts_ns=base + 2)

    buf = RingBuffer(capacity=5)
    buf.write(ce1)
    buf.write(ce2)
    buf.write(ce3)

    flushed = buf.flush()
    assert [ce.occurred_at for ce in flushed] == sorted(
        [ce1.occurred_at, ce2.occurred_at, ce3.occurred_at]
    )


def test_concurrent_writes_no_corruption():
    """Concurrent writes must not corrupt the buffer or exceed capacity."""
    num_threads = 10
    writes_per_thread = 100
    capacity = 500
    buf = RingBuffer(capacity=capacity)
    barrier = threading.Barrier(num_threads)

    def writer():
        barrier.wait()
        for _ in range(writes_per_thread):
            buf.write(make_ce())

    threads = [threading.Thread(target=writer) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # size must never exceed capacity
    assert buf.size <= capacity
    # total written = 1000, capacity = 500, so buffer should be full
    assert buf.size == capacity

    flushed = buf.flush()
    # no None slots in flushed results
    assert all(ce is not None for ce in flushed)
    # count should match reported size before flush
    assert len(flushed) == capacity
    # buffer is empty after flush
    assert buf.size == 0
