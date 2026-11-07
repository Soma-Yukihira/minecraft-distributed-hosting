from mdh_client.coordination.lease_guard import LeaseGuard


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_no_lease_means_stop() -> None:
    assert LeaseGuard(clock=FakeClock()).must_stop()


def test_deadline_counts_from_send_time_not_receive_time() -> None:
    clock = FakeClock()
    guard = LeaseGuard(safety_margin_s=5, clock=clock)
    clock.now = 100
    sent = guard.request_sent()
    clock.now = 103                     # slow answer
    guard.granted(sent, epoch=1, ttl_s=30)
    clock.now = 124.9
    assert not guard.must_stop()
    clock.now = 125                     # 100 + 30 - 5
    assert guard.must_stop()


def test_out_of_order_answers_keep_latest_deadline() -> None:
    clock = FakeClock()
    guard = LeaseGuard(safety_margin_s=5, clock=clock)
    first = guard.request_sent()
    clock.now = 10
    second = guard.request_sent()
    guard.granted(second, epoch=1, ttl_s=30)
    guard.granted(first, epoch=1, ttl_s=30)   # late answer to the older request
    assert guard.remaining_s() == 25


def test_new_epoch_resets_and_old_epoch_is_ignored() -> None:
    clock = FakeClock()
    guard = LeaseGuard(safety_margin_s=5, clock=clock)
    guard.granted(guard.request_sent(), epoch=2, ttl_s=30)
    clock.now = 20
    guard.granted(0.0, epoch=1, ttl_s=1000)   # stale epoch must not extend anything
    assert guard.epoch == 2
    assert guard.remaining_s() == 5


def test_revoked_stops_immediately() -> None:
    clock = FakeClock()
    guard = LeaseGuard(clock=clock)
    guard.granted(guard.request_sent(), epoch=1, ttl_s=30)
    guard.revoked()
    assert guard.must_stop()
