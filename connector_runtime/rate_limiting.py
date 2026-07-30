"""
Adaptive rate limiting (DL-CONN-11) bound per connection (DL-SCOPE-07).

Three strategies behind one `RateLimitPolicy` port: fixed window, token bucket, and
`Retry-After`-driven backoff. On sustained 429s the policy asks the caller to checkpoint
and exit cleanly rather than burn the Lambda budget — reusing the existing
`LambdaTimeoutWarning` checkpoint path rather than inventing a second exit route.

Twelve HubSpot connections hit twelve independent tenant quotas at the provider, so a
policy instance binds to a connection, not to a source type. A provider with a shared
quota declares that in its capability set and the registry hands out one shared instance.
"""

from __future__ import annotations

import abc
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Consecutive throttles after which the connector should checkpoint rather than keep waiting.
DEFAULT_SUSTAINED_THROTTLE_LIMIT: Final[int] = 5

# Upper bound on any single backoff so a hostile Retry-After cannot pin a Lambda open.
MAX_BACKOFF_SECONDS: Final[float] = 60.0

# Beyond this, the policy stops sleeping and asks the caller to checkpoint and resume (L14).
#
# Sleeping inside a Lambda is billed wall-clock inside a 900s budget: a provider issuing repeated
# `Retry-After: 60` consumes the invocation doing nothing and then dies at the timeout mid-entity.
# Ten throttling SaaS APIs across 80-100 entities per tenant makes that a systematic ceiling, not
# an edge case. The Step Functions `Wait` state costs nothing while it waits.
MAX_IN_LAMBDA_SLEEP_SECONDS: Final[float] = 5.0


class RateLimitStrategy(StrEnum):
    """Registered policy kinds."""

    FIXED_WINDOW = "fixed_window"
    TOKEN_BUCKET = "token_bucket"  # noqa: S105 — strategy name, not a credential  # nosec B105
    RETRY_AFTER = "retry_after"


class ResumeAfterBackoffRequired(Exception):  # noqa: N818 — a control-flow signal, not a failure
    """
    Raised instead of sleeping when the required backoff exceeds what a Lambda should absorb.

    Carries `retry_after_seconds` so the caller can commit a partial watermark and hand the wait to
    a Step Functions `Wait` state, which is free. Not an error: the extraction is progressing, it
    simply cannot continue in this invocation.
    """

    def __init__(self, message: str, *, retry_after_seconds: float, connection_id: str) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.connection_id = connection_id


class SustainedThrottleError(Exception):
    """Raised when a source throttles persistently; the caller must checkpoint and exit."""


@dataclass
class RateLimitObservation:
    """What the last response told us about the remaining budget."""

    throttled: bool = False
    retry_after_seconds: float | None = None
    remaining_requests: int | None = None
    limit_requests: int | None = None


class RateLimitPolicy(abc.ABC):
    """Port every adapter acquires against before an outbound source call."""

    def __init__(
        self,
        *,
        connection_id: str,
        sustained_throttle_limit: int = DEFAULT_SUSTAINED_THROTTLE_LIMIT,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._sustained_throttle_limit = sustained_throttle_limit
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self.consecutive_throttles = 0
        self.total_throttles = 0
        self.total_backoff_ms = 0.0

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @abc.abstractmethod
    def acquire(self) -> None:
        """Block until a request may be issued, or raise `SustainedThrottleError`."""
        raise NotImplementedError

    def observe(self, response_headers: Mapping[str, str]) -> RateLimitObservation:
        """Feed the provider's rate-limit headers back into the policy."""
        observation = parse_rate_limit_headers(response_headers)
        with self._lock:
            if observation.throttled:
                self.consecutive_throttles += 1
                self.total_throttles += 1
            else:
                self.consecutive_throttles = 0
        self._apply_observation(observation)
        return observation

    def _apply_observation(self, observation: RateLimitObservation) -> None:  # noqa: B027
        """Optional subclass hook — a fixed schedule legitimately ignores observations."""

    def _guard_sustained_throttling(self) -> None:
        if self.consecutive_throttles >= self._sustained_throttle_limit:
            raise SustainedThrottleError(
                f"Connection {self._connection_id!r} was throttled "
                f"{self.consecutive_throttles} times consecutively. Checkpoint the extraction "
                "and exit cleanly rather than consuming the remaining Lambda budget."
            )

    def _back_off(self, seconds: float) -> None:
        bounded = max(0.0, min(seconds, MAX_BACKOFF_SECONDS))
        if bounded > MAX_IN_LAMBDA_SLEEP_SECONDS:
            # Hand the wait to the state machine rather than billing for it here (L14).
            record_platform_metric(
                PlatformMetric.RATE_LIMIT_BACKOFF_MS,
                bounded * 1000,
                ConnectionId=self._connection_id,
            )
            raise ResumeAfterBackoffRequired(
                f"Connection {self._connection_id!r} must wait {bounded:.1f}s, above the "
                f"{MAX_IN_LAMBDA_SLEEP_SECONDS:.0f}s a Lambda should absorb. Checkpoint and "
                "resume rather than paying for idle wall-clock.",
                retry_after_seconds=bounded,
                connection_id=self._connection_id,
            )
        # Full jitter: without it, N connections throttled by the same provider window
        # would retry in lockstep and re-trigger the same throttle.
        jittered = bounded * (0.5 + random.random() / 2)  # noqa: S311  # nosec B311 — jitter
        self.total_backoff_ms += jittered * 1000
        if jittered > 0:
            self._sleep(jittered)


class FixedWindowRateLimitPolicy(RateLimitPolicy):
    """At most `max_requests` per rolling `window_seconds`."""

    def __init__(
        self,
        *,
        connection_id: str,
        max_requests: int,
        window_seconds: float = 1.0,
        sustained_throttle_limit: int = DEFAULT_SUSTAINED_THROTTLE_LIMIT,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            connection_id=connection_id,
            sustained_throttle_limit=sustained_throttle_limit,
            sleep=sleep,
        )
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1.")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._monotonic = monotonic or time.monotonic
        self._window_started = self._monotonic()
        self._issued_in_window = 0

    def acquire(self) -> None:
        self._guard_sustained_throttling()
        with self._lock:
            now = self._monotonic()
            if now - self._window_started >= self._window_seconds:
                self._window_started = now
                self._issued_in_window = 0
            if self._issued_in_window < self._max_requests:
                self._issued_in_window += 1
                return
            wait = self._window_seconds - (now - self._window_started)
        self._back_off(wait)
        with self._lock:
            self._window_started = self._monotonic()
            self._issued_in_window = 1


class TokenBucketRateLimitPolicy(RateLimitPolicy):
    """Sustained `refill_per_second` with `capacity` burst headroom."""

    def __init__(
        self,
        *,
        connection_id: str,
        capacity: int,
        refill_per_second: float,
        sustained_throttle_limit: int = DEFAULT_SUSTAINED_THROTTLE_LIMIT,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            connection_id=connection_id,
            sustained_throttle_limit=sustained_throttle_limit,
            sleep=sleep,
        )
        if capacity < 1:
            raise ValueError("capacity must be at least 1.")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive.")
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_second
        self._monotonic = monotonic or time.monotonic
        self._tokens = float(capacity)
        self._last_refill = self._monotonic()

    def acquire(self) -> None:
        self._guard_sustained_throttling()
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self._refill_per_second
        self._back_off(wait)
        with self._lock:
            self._refill()
            self._tokens = max(0.0, self._tokens - 1.0)

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)


class RetryAfterRateLimitPolicy(RateLimitPolicy):
    """Issues freely until throttled, then honours `Retry-After` with exponential fallback."""

    def __init__(
        self,
        *,
        connection_id: str,
        base_backoff_seconds: float = 1.0,
        sustained_throttle_limit: int = DEFAULT_SUSTAINED_THROTTLE_LIMIT,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(
            connection_id=connection_id,
            sustained_throttle_limit=sustained_throttle_limit,
            sleep=sleep,
        )
        self._base_backoff_seconds = base_backoff_seconds
        self._pending_backoff_seconds = 0.0

    def acquire(self) -> None:
        self._guard_sustained_throttling()
        with self._lock:
            wait = self._pending_backoff_seconds
            self._pending_backoff_seconds = 0.0
        if wait > 0:
            self._back_off(wait)

    def _apply_observation(self, observation: RateLimitObservation) -> None:
        if not observation.throttled:
            return
        if observation.retry_after_seconds is not None:
            self._pending_backoff_seconds = observation.retry_after_seconds
            return
        self._pending_backoff_seconds = self._base_backoff_seconds * (
            2 ** min(self.consecutive_throttles - 1, 6)
        )


# Header names providers actually use; parsed case-insensitively.
_RETRY_AFTER_HEADERS: Final[tuple[str, ...]] = ("retry-after", "x-ratelimit-retry-after")
_REMAINING_HEADERS: Final[tuple[str, ...]] = (
    "x-ratelimit-remaining",
    "x-rate-limit-remaining",
    "ratelimit-remaining",
)
_LIMIT_HEADERS: Final[tuple[str, ...]] = (
    "x-ratelimit-limit",
    "x-rate-limit-limit",
    "ratelimit-limit",
)
_STATUS_HEADERS: Final[tuple[str, ...]] = ("x-edl-response-status", "status")


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitObservation:
    """
    Normalise provider rate-limit headers into one observation.

    One parser parameterised by header name rather than one per provider — the same reuse
    rule the credential client and raw-layer writer already follow.
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    retry_after = _first_float(lowered, _RETRY_AFTER_HEADERS)
    remaining = _first_int(lowered, _REMAINING_HEADERS)
    limit = _first_int(lowered, _LIMIT_HEADERS)
    status = _first_int(lowered, _STATUS_HEADERS)
    throttled = status == 429 or retry_after is not None or remaining == 0
    return RateLimitObservation(
        throttled=throttled,
        retry_after_seconds=retry_after,
        remaining_requests=remaining,
        limit_requests=limit,
    )


def _first_float(headers: Mapping[str, str], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in headers:
            try:
                return float(headers[name])
            except ValueError:
                continue
    return None


def _first_int(headers: Mapping[str, str], names: tuple[str, ...]) -> int | None:
    value = _first_float(headers, names)
    return None if value is None else int(value)


@dataclass
class RateLimitPolicySpec:
    """Declarative policy definition, resolved to an instance per connection."""

    strategy: RateLimitStrategy
    max_requests: int = 10
    window_seconds: float = 1.0
    capacity: int = 10
    refill_per_second: float = 10.0
    base_backoff_seconds: float = 1.0
    shared_across_connections: bool = False


@dataclass(frozen=True)
class DocumentedRateLimit:
    """One limit a vendor publishes, in the vendor's own units."""

    max_requests: int
    window_seconds: float

    def worst_case_issued(self, capacity: int, refill_per_second: float) -> float:
        """Most requests a full bucket can issue inside this window."""
        return capacity + refill_per_second * self.window_seconds

    def permits(self, capacity: int, refill_per_second: float) -> bool:
        return self.worst_case_issued(capacity, refill_per_second) <= self.max_requests


# How much of the tightest documented rate is spent on sustained throughput; the remainder
# is what leaves room for a burst without breaching the window.
SUSTAINED_FRACTION: Final[float] = 0.7

# Headroom kept under every documented window, so a co-tenant or a retry does not tip us over.
BURST_SAFETY_FRACTION: Final[float] = 0.9


def token_bucket_within(
    limits: Sequence[DocumentedRateLimit],
    *,
    shared_across_connections: bool = False,
) -> RateLimitPolicySpec:
    """
    Derive a token bucket that cannot breach any of the vendor's documented limits.

    Sizing a bucket by hand gets this wrong in a way that looks right: `capacity` is an
    *instantaneous* burst which **adds** to whatever the bucket refills during the window, so
    the quantity a vendor caps is `capacity + refill x window`, not `capacity`. On 2026-07-30
    four registered policies — three new, plus HubSpot's, which predates them — were over
    their documented limit for exactly that reason. Deriving the numbers makes the invariant
    hold by construction instead of by arithmetic nobody re-checks.

    `refill` is the tightest documented rate scaled by `SUSTAINED_FRACTION`; `capacity` is
    then the largest burst every window still permits at `BURST_SAFETY_FRACTION`.
    """
    if not limits:
        raise ValueError("A token bucket must be derived from at least one documented limit.")
    refill = min(limit.max_requests / limit.window_seconds for limit in limits)
    refill *= SUSTAINED_FRACTION
    headroom = min(
        limit.max_requests * BURST_SAFETY_FRACTION - refill * limit.window_seconds
        for limit in limits
    )
    capacity = max(1, int(headroom))
    spec = RateLimitPolicySpec(
        RateLimitStrategy.TOKEN_BUCKET,
        capacity=capacity,
        refill_per_second=refill,
        shared_across_connections=shared_across_connections,
    )
    breached = [limit for limit in limits if not limit.permits(capacity, refill)]
    if breached:
        # Unreachable by construction; asserted because a silent breach is a vendor
        # relationship problem, not a test failure someone can shrug off.
        raise ValueError(
            f"Derived bucket (capacity={capacity}, refill={refill:.3f}/s) still breaches "
            f"{[(b.max_requests, b.window_seconds) for b in breached]}."
        )
    return spec


class RateLimitPolicyRegistry:
    """
    Named policy specs plus one instance per (policy, connection).

    A provider whose quota is shared across a tenant's connections declares
    `shared_across_connections`, in which case every connection receives the same
    instance so the shared budget is genuinely shared (DL-SCOPE-07).
    """

    def __init__(self) -> None:
        self._specs: dict[str, RateLimitPolicySpec] = {}
        self._instances: dict[tuple[str, str], RateLimitPolicy] = {}

    def register(self, name: str, spec: RateLimitPolicySpec) -> None:
        if name in self._specs:
            raise ValueError(f"Rate-limit policy {name!r} is already registered.")
        self._specs[name] = spec

    def registered_names(self) -> list[str]:
        return sorted(self._specs)

    def resolve(self, name: str, connection_id: str) -> RateLimitPolicy:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(
                f"No rate-limit policy registered under {name!r}. "
                f"Registered: {self.registered_names()}."
            )
        key = (name, "__shared__" if spec.shared_across_connections else connection_id)
        existing = self._instances.get(key)
        if existing is not None:
            return existing
        policy = self._build(spec, connection_id)
        self._instances[key] = policy
        return policy

    def reset(self) -> None:
        """Testing only — clears registered specs and cached instances."""
        self._specs.clear()
        self._instances.clear()

    @staticmethod
    def _build(spec: RateLimitPolicySpec, connection_id: str) -> RateLimitPolicy:
        if spec.strategy is RateLimitStrategy.FIXED_WINDOW:
            return FixedWindowRateLimitPolicy(
                connection_id=connection_id,
                max_requests=spec.max_requests,
                window_seconds=spec.window_seconds,
            )
        if spec.strategy is RateLimitStrategy.TOKEN_BUCKET:
            return TokenBucketRateLimitPolicy(
                connection_id=connection_id,
                capacity=spec.capacity,
                refill_per_second=spec.refill_per_second,
            )
        return RetryAfterRateLimitPolicy(
            connection_id=connection_id,
            base_backoff_seconds=spec.base_backoff_seconds,
        )


rate_limit_policy_registry: Final[RateLimitPolicyRegistry] = RateLimitPolicyRegistry()


def _register_platform_policies() -> None:
    """Provider-observed defaults; a connection may override via its config."""
    rate_limit_policy_registry.register(
        "hubspot-standard",
        # HubSpot: 110 requests / 10 s per private app token. Previously capacity=100 with
        # a 10/s refill, which permits 200 in that same 10 s — 82% over. Derived now.
        token_bucket_within([DocumentedRateLimit(110, 10)]),
    )
    rate_limit_policy_registry.register(
        "wellsky-conservative",
        # WellSky Personal Care states it does not explicitly throttle but asks for no more
        # than 100 req/s and advises against batch use. Sized an order of magnitude below
        # the stated ceiling: an unenforced request is still a request.
        RateLimitPolicySpec(RateLimitStrategy.TOKEN_BUCKET, capacity=10, refill_per_second=5.0),
    )
    rate_limit_policy_registry.register(
        "google-ads-standard",
        RateLimitPolicySpec(RateLimitStrategy.FIXED_WINDOW, max_requests=10, window_seconds=1.0),
    )
    rate_limit_policy_registry.register(
        "google-analytics-standard",
        RateLimitPolicySpec(RateLimitStrategy.FIXED_WINDOW, max_requests=10, window_seconds=1.0),
    )
    rate_limit_policy_registry.register(
        "meta-ads-standard",
        # Meta's ad-account budget is per app, shared across a tenant's connections.
        RateLimitPolicySpec(
            RateLimitStrategy.RETRY_AFTER,
            base_backoff_seconds=5.0,
            shared_across_connections=True,
        ),
    )
    rate_limit_policy_registry.register(
        "dialpad-standard",
        # DialPad documents 20 requests/second per company. Endpoint-specific per-minute
        # caps sit under that global limit; the derived headroom covers the common ones.
        token_bucket_within([DocumentedRateLimit(20, 1)]),
    )
    rate_limit_policy_registry.register(
        "housecall-pro-standard",
        RateLimitPolicySpec(RateLimitStrategy.FIXED_WINDOW, max_requests=5, window_seconds=1.0),
    )
    # MaidCentral documents "1000 requests per hour per API key" and "burst limit: 100
    # requests per MINUTE" — the tightest budget on the platform at 0.28 req/s sustained.
    # A capacity of 100 was the error the derivation exists to prevent: it permits 100
    # instantly, which is 100 inside one second, not one minute.
    maid_central = token_bucket_within(
        [DocumentedRateLimit(1_000, 3_600), DocumentedRateLimit(100, 60)]
    )
    rate_limit_policy_registry.register("maid-central-hourly", maid_central)
    rate_limit_policy_registry.register(
        # Retained so a connection still configured with the pre-rewrite policy name
        # resolves rather than failing at build time.
        "maid-central-standard",
        maid_central,
    )
    rate_limit_policy_registry.register(
        "servman-pro-standard",
        RateLimitPolicySpec(RateLimitStrategy.RETRY_AFTER, base_backoff_seconds=1.0),
    )
    rate_limit_policy_registry.register(
        "seniorplace-standard",
        RateLimitPolicySpec(RateLimitStrategy.FIXED_WINDOW, max_requests=5, window_seconds=1.0),
    )
    rate_limit_policy_registry.register(
        "sage-intacct-standard",
        RateLimitPolicySpec(RateLimitStrategy.TOKEN_BUCKET, capacity=10, refill_per_second=1.0),
    )


_register_platform_policies()


@dataclass
class RateLimitTelemetry:
    """Snapshot for `RateLimitHits` and `RateLimitBackoffMs` emission."""

    connection_id: str
    hits: int
    backoff_ms: float
    checkpointed: bool = False
    observations: list[RateLimitObservation] = field(default_factory=list)


def telemetry_for(policy: RateLimitPolicy, *, checkpointed: bool = False) -> RateLimitTelemetry:
    """Read the counters a stage emits at the end of an extraction, recording them as it goes."""
    record_platform_metric(
        PlatformMetric.RATE_LIMIT_HITS, policy.total_throttles, ConnectionId=policy.connection_id
    )
    record_platform_metric(
        PlatformMetric.RATE_LIMIT_BACKOFF_MS,
        policy.total_backoff_ms,
        ConnectionId=policy.connection_id,
    )
    if checkpointed:
        record_platform_metric(
            PlatformMetric.CHECKPOINTED_RUNS, 1.0, ConnectionId=policy.connection_id
        )
    return RateLimitTelemetry(
        connection_id=policy.connection_id,
        hits=policy.total_throttles,
        backoff_ms=policy.total_backoff_ms,
        checkpointed=checkpointed,
    )
