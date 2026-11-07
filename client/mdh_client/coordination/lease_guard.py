"""Host-side view of a lease: decides, without asking anyone, when the host must stop.

Only the local monotonic clock is used. A successful grant/renewal for a request *sent* at local
time ``s`` proves the control plane set an expiry of at least ``s + ttl`` (it received the
request after we sent it). We therefore stop at ``s + ttl - margin`` at the latest unless a newer
renewal succeeds. The control plane waits until ``expiry + grace`` before handing the world to
anyone else, so the two can never overlap (docs/ARCHITECTURE.md 2.3).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from mdh_shared.constants import LEASE_SAFETY_MARGIN_S


class LeaseGuard:
    def __init__(
        self,
        safety_margin_s: float = LEASE_SAFETY_MARGIN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._margin = safety_margin_s
        self._clock = clock
        self._epoch: int | None = None
        self._deadline = -math.inf

    @property
    def epoch(self) -> int | None:
        return self._epoch

    def request_sent(self) -> float:
        """Call right before sending an acquire/renew request; pass the result to ``granted``."""
        return self._clock()

    def granted(self, sent_at: float, epoch: int, ttl_s: float) -> None:
        if self._epoch is not None and epoch < self._epoch:
            return  # late answer for an epoch we already replaced
        if epoch != self._epoch:
            self._epoch = epoch
            self._deadline = -math.inf
        # Responses may arrive out of order: each one is a valid lower bound, keep the latest.
        self._deadline = max(self._deadline, sent_at + ttl_s - self._margin)

    def revoked(self) -> None:
        """The control plane answered stale_epoch / expired: stop now."""
        self._deadline = -math.inf

    def must_stop(self) -> bool:
        return self._clock() >= self._deadline

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())
