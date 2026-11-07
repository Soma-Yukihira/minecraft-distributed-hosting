import pytest

from mdh_server.coordination.lease import (
    LeaseExpired,
    LeaseHeld,
    LeasePolicy,
    LeaseState,
    Quarantine,
    StaleEpoch,
    acquire,
    check_fence,
    release,
    renew,
)

POLICY = LeasePolicy(ttl_s=30, grace_s=15)
BOOT = -1_000.0  # control plane started long ago


def grant(state: LeaseState, host: str, now: float, boot: float = BOOT) -> LeaseState:
    return acquire(state, host, now, POLICY, control_plane_started_at=boot)


def test_free_lease_is_granted_with_new_epoch() -> None:
    s = grant(LeaseState("srv"), "A", now=100)
    assert (s.epoch, s.holder, s.expires_at) == (1, "A", 130)


def test_other_host_cannot_take_an_active_lease() -> None:
    s = grant(LeaseState("srv"), "A", now=100)
    with pytest.raises(LeaseHeld):
        grant(s, "B", now=110)


def test_other_host_must_wait_for_expiry_plus_grace() -> None:
    s = grant(LeaseState("srv"), "A", now=100)        # expires at 130
    with pytest.raises(LeaseHeld):
        grant(s, "B", now=130)                         # expired but inside grace
    with pytest.raises(LeaseHeld):
        grant(s, "B", now=144.999)
    b = grant(s, "B", now=145)
    assert (b.epoch, b.holder) == (2, "B")


def test_renew_extends_and_keeps_epoch() -> None:
    s = grant(LeaseState("srv"), "A", now=100)
    s2 = renew(s, "A", 1, now=125, policy=POLICY)
    assert (s2.epoch, s2.expires_at) == (1, 155)


def test_renew_after_expiry_is_refused() -> None:
    s = grant(LeaseState("srv"), "A", now=100)
    with pytest.raises(LeaseExpired):
        renew(s, "A", 1, now=130, policy=POLICY)


def test_old_holder_is_fenced_after_takeover() -> None:
    a = grant(LeaseState("srv"), "A", now=100)
    b = grant(a, "B", now=200)
    for op in (
        lambda: renew(b, "A", 1, now=201, policy=POLICY),
        lambda: check_fence(b, "A", 1, now=201),
        lambda: release(b, "A", 1),
    ):
        with pytest.raises(StaleEpoch):
            op()
    check_fence(b, "B", 2, now=201)


def test_same_holder_can_reacquire_after_expiry_with_new_epoch() -> None:
    a = grant(LeaseState("srv"), "A", now=100)
    a2 = grant(a, "A", now=135)                       # no grace needed for the same host
    assert (a2.epoch, a2.holder) == (2, "A")
    with pytest.raises(StaleEpoch):
        check_fence(a2, "A", 1, now=136)              # the previous incarnation is fenced


def test_release_frees_without_bumping_epoch() -> None:
    a = grant(LeaseState("srv"), "A", now=100)
    free = release(a, "A", 1)
    assert (free.epoch, free.holder, free.expires_at) == (1, None, None)
    b = grant(free, "B", now=101)                     # clean handover: immediate
    assert b.epoch == 2


def test_startup_quarantine_blocks_takeover_but_not_free_leases() -> None:
    boot = 1_000.0
    held = LeaseState("srv", epoch=4, holder="A", expires_at=900.0)  # expired before reboot
    with pytest.raises(Quarantine):
        grant(held, "B", now=boot + 10, boot=boot)
    assert grant(held, "B", now=boot + POLICY.startup_quarantine_s, boot=boot).epoch == 5
    assert grant(LeaseState("srv", epoch=4), "B", now=boot + 1, boot=boot).epoch == 5
    assert grant(held, "A", now=boot + 1, boot=boot).holder == "A"


def test_epochs_are_strictly_increasing_over_many_handovers() -> None:
    s = LeaseState("srv")
    now = 0.0
    epochs = []
    for i in range(50):
        host = "ABC"[i % 3]
        now += 100
        s = grant(s, host, now)
        epochs.append(s.epoch)
    assert epochs == list(range(1, 51))


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        LeasePolicy(ttl_s=0)
