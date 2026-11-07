"""Lease rules: who may run (and write) a world, and until when.

These are pure state transitions with no I/O, so they can be tested exhaustively. The database
layer (Phase 2) applies them inside a ``BEGIN IMMEDIATE`` transaction: read the row, call one
of these functions, write the result, commit, *then* answer the host.

Times are VPS wall-clock seconds. Hosts never compare their clock with these values; they only
use durations measured with their own monotonic clock (see ``mdh_client.coordination``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mdh_shared.constants import LEASE_GRACE_S, LEASE_TTL_S


class LeaseError(Exception):
    code = "lease_error"


class LeaseHeld(LeaseError):
    """Another host holds the lease, or held it too recently to be considered isolated."""

    code = "lease_held"


class StaleEpoch(LeaseError):
    """The caller is not (or no longer) the holder of this epoch: it must stop immediately."""

    code = "stale_epoch"


class LeaseExpired(LeaseError):
    """The caller's lease expired. It must stop and may try to re-acquire."""

    code = "expired"


class Quarantine(LeaseError):
    """The control plane just restarted and is not yet allowed to hand the lease to someone else."""

    code = "quarantine"


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    ttl_s: float = LEASE_TTL_S
    grace_s: float = LEASE_GRACE_S

    def __post_init__(self) -> None:
        if self.ttl_s <= 0 or self.grace_s < 0:
            raise ValueError("ttl must be > 0 and grace >= 0")

    @property
    def startup_quarantine_s(self) -> float:
        return self.ttl_s + self.grace_s


@dataclass(frozen=True, slots=True)
class LeaseState:
    """One row per logical server. ``epoch`` is the fencing token and never decreases."""

    server_id: str
    epoch: int = 0
    holder: str | None = None
    expires_at: float | None = None

    def is_active(self, now: float) -> bool:
        return self.holder is not None and self.expires_at is not None and now < self.expires_at

    def is_isolated_from(self, now: float, policy: LeasePolicy) -> bool:
        """True when nobody holds the lease, or the holder is past expiry + grace."""
        if self.holder is None:
            return True
        assert self.expires_at is not None
        return now >= self.expires_at + policy.grace_s


def acquire(
    state: LeaseState,
    host_id: str,
    now: float,
    policy: LeasePolicy,
    *,
    control_plane_started_at: float,
) -> LeaseState:
    """Grant the lease to ``host_id`` with a new epoch, or raise.

    * Free lease: granted.
    * Same holder (e.g. its agent restarted, or its lease just expired): granted with a new
      epoch; any older incarnation is fenced by the epoch change.
    * Other holder: only once it is isolated (expiry + grace) and the control plane is out of
      its startup quarantine.

    Whether a *dirty* takeover (no final commit) is allowed is a server-level policy decided by
    the caller before calling this function.
    """
    if state.holder is not None and state.holder != host_id:
        if now < control_plane_started_at + policy.startup_quarantine_s:
            raise Quarantine(f"control plane restarted less than {policy.startup_quarantine_s}s ago")
        if not state.is_isolated_from(now, policy):
            raise LeaseHeld(f"server {state.server_id} is held by {state.holder}")
    return LeaseState(
        server_id=state.server_id,
        epoch=state.epoch + 1,
        holder=host_id,
        expires_at=now + policy.ttl_s,
    )


def renew(state: LeaseState, host_id: str, epoch: int, now: float, policy: LeasePolicy) -> LeaseState:
    check_fence(state, host_id, epoch, now)
    return replace(state, expires_at=now + policy.ttl_s)


def release(state: LeaseState, host_id: str, epoch: int) -> LeaseState:
    """Voluntary release. Allowed even after expiry, as long as nobody else took over."""
    if state.holder != host_id or state.epoch != epoch:
        raise StaleEpoch(f"epoch {epoch} of {host_id} is not current")
    return replace(state, holder=None, expires_at=None)


def check_fence(state: LeaseState, host_id: str, epoch: int, now: float) -> None:
    """Gate for every write (renewal, world commit, blob upload)."""
    if state.holder != host_id or state.epoch != epoch:
        raise StaleEpoch(f"epoch {epoch} of {host_id} is not current (current: {state.epoch})")
    if not state.is_active(now):
        raise LeaseExpired(f"lease epoch {epoch} expired")
