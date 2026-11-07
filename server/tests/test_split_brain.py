"""Split-brain prevention: the old host always stops well before a new host may start.

Simulates real time with independent, drifting clocks for the host and the control plane,
random network delays, and a network partition at a random moment. Uses the real
:class:`LeaseGuard` (client) and lease rules (VPS).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from mdh_client.coordination.lease_guard import LeaseGuard
from mdh_server.coordination.lease import LeaseHeld, LeasePolicy, LeaseState, acquire, renew
from mdh_shared.constants import LEASE_RENEW_INTERVAL_S, LEASE_SAFETY_MARGIN_S

POLICY = LeasePolicy()
STEP = 0.05
#: Time the old host is guaranteed to have, between deciding to stop and the earliest possible
#: start of another host (docs/ARCHITECTURE.md 2.3 announces ~19.9 s with the defaults).
REQUIRED_STOP_BUDGET_S = 19.0


class Clock:
    def __init__(self, offset: float, drift: float) -> None:
        self.offset, self.drift, self.real = offset, drift, 0.0

    def __call__(self) -> float:
        return self.offset + self.real * (1 + self.drift)


@settings(max_examples=150, deadline=None)
@given(
    host_drift=st.floats(-1e-3, 1e-3),
    vps_drift=st.floats(-1e-3, 1e-3),
    host_offset=st.floats(-1e6, 1e6),
    vps_offset=st.floats(1e6, 2e6),
    renewals=st.integers(0, 8),
    delays=st.lists(st.tuples(st.floats(0, 4), st.floats(0, 4)), min_size=9, max_size=9),
)
def test_old_host_stops_before_any_takeover(
    host_drift: float,
    vps_drift: float,
    host_offset: float,
    vps_offset: float,
    renewals: int,
    delays: list[tuple[float, float]],
) -> None:
    host_clock = Clock(host_offset, host_drift)
    vps_clock = Clock(vps_offset, vps_drift)
    guard = LeaseGuard(LEASE_SAFETY_MARGIN_S, clock=host_clock)

    def set_real(t: float) -> None:
        host_clock.real = vps_clock.real = t

    # Initial acquire, then `renewals` successful renewals, then the network is cut.
    real = 0.0
    state = LeaseState("srv")
    for i in range(renewals + 1):
        up, down = delays[i]
        set_real(real)
        sent = guard.request_sent()
        set_real(real + up)
        if i == 0:
            state = acquire(state, "A", vps_clock(), POLICY, control_plane_started_at=-1e9)
        else:
            state = renew(state, "A", state.epoch, vps_clock(), POLICY)
        set_real(real + up + down)
        guard.granted(sent, state.epoch, POLICY.ttl_s)
        real += LEASE_RENEW_INTERVAL_S

    # Find when the host decides to stop, and when B can first be granted the lease.
    t = 0.0
    host_stops_at = None
    takeover_at = None
    while takeover_at is None:
        set_real(t)
        if host_stops_at is None and guard.must_stop():
            host_stops_at = t
        try:
            acquire(state, "B", vps_clock(), POLICY, control_plane_started_at=-1e9)
            takeover_at = t
        except LeaseHeld:
            pass
        t += STEP

    assert host_stops_at is not None
    assert takeover_at - host_stops_at > REQUIRED_STOP_BUDGET_S


def test_stale_epoch_answer_stops_host_immediately() -> None:
    clock = Clock(0, 0)
    guard = LeaseGuard(clock=clock)
    guard.granted(guard.request_sent(), epoch=1, ttl_s=30)
    assert not guard.must_stop()
    guard.revoked()
    assert guard.must_stop()
