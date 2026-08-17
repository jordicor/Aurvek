"""
Rate Limiter Module for Aurvek

In-memory rate limiter with multiple strategies:
- By IP (all attempts)
- By IP (failures only)
- By identifier (email/username)

For production with multiple workers, consider Redis-based implementation.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Tuple
import logging

from middleware.security import get_client_ip

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    In-memory rate limiter with multiple strategies.
    Thread-safe for single-process async applications.
    """

    def __init__(self):
        # {key: [timestamp, timestamp, ...]}
        self._attempts = defaultdict(list)
        self._last_cleanup = datetime.now()

    def _cleanup_old_entries(self, max_age_hours: int = 25):
        """Periodic cleanup of old entries to prevent memory bloat."""
        now = datetime.now()
        # Only cleanup every hour
        if (now - self._last_cleanup).total_seconds() < 3600:
            return

        cutoff = now - timedelta(hours=max_age_hours)
        keys_to_delete = []

        for key, timestamps in self._attempts.items():
            self._attempts[key] = [t for t in timestamps if t > cutoff]
            if not self._attempts[key]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._attempts[key]

        self._last_cleanup = now
        logger.debug(f"Rate limiter cleanup: removed {len(keys_to_delete)} stale keys")

    def is_allowed(
        self,
        key: str,
        max_attempts: int,
        window_minutes: int
    ) -> Tuple[bool, int]:
        """
        Check if action is allowed and record attempt.

        Args:
            key: Unique identifier for this limit (e.g., "ip_all:login:1.2.3.4")
            max_attempts: Maximum attempts allowed in window
            window_minutes: Time window in minutes

        Returns:
            Tuple of (allowed: bool, remaining: int)
        """
        self._cleanup_old_entries()

        now = datetime.now()
        window_start = now - timedelta(minutes=window_minutes)

        # Filter to window
        self._attempts[key] = [t for t in self._attempts[key] if t > window_start]
        current_count = len(self._attempts[key])

        if current_count >= max_attempts:
            return False, 0

        self._attempts[key].append(now)
        return True, max_attempts - current_count - 1

    def record_failure(self, key: str, window_minutes: int = 1440) -> int:
        """Record a failure and return its count in the requested window."""
        now = datetime.now()
        window_start = now - timedelta(minutes=window_minutes)
        recent = [t for t in self._attempts[key] if t > window_start]
        recent.append(now)
        self._attempts[key] = recent
        return len(recent)

    def clear_key(self, key: str) -> bool:
        """Clear one exact bucket without affecting other accounts or IPs."""
        return self._attempts.pop(key, None) is not None

    def check_only(
        self,
        key: str,
        max_attempts: int,
        window_minutes: int
    ) -> Tuple[bool, int]:
        """Check limit without recording (for pre-check)."""
        now = datetime.now()
        window_start = now - timedelta(minutes=window_minutes)

        timestamps = [t for t in self._attempts.get(key, []) if t > window_start]
        current_count = len(timestamps)

        if current_count >= max_attempts:
            return False, 0
        return True, max_attempts - current_count

    def get_retry_after(self, key: str, window_minutes: int) -> int:
        """Get seconds until the oldest attempt in window expires."""
        now = datetime.now()
        window_start = now - timedelta(minutes=window_minutes)

        timestamps = [t for t in self._attempts.get(key, []) if t > window_start]
        if not timestamps:
            return 0

        oldest = min(timestamps)
        expires_at = oldest + timedelta(minutes=window_minutes)
        seconds_remaining = (expires_at - now).total_seconds()

        return max(0, int(seconds_remaining))

    def clear_for_identifier(self, identifier: str) -> int:
        """Clear all identifier-scoped buckets for a username or email."""
        suffix = f":{identifier.lower()}"
        keys_to_delete = [key for key in self._attempts if key.lower().endswith(suffix)]
        for key in keys_to_delete:
            del self._attempts[key]
        return len(keys_to_delete)

    def clear_for_ip(self, ip: str) -> int:
        """Clear all IP-scoped buckets for a specific IP."""
        suffix = f":{ip}"
        pair_marker = f":{ip}:"
        keys_to_delete = [
            key
            for key in self._attempts
            if key.endswith(suffix)
            or (key.startswith("pair_fail:") and pair_marker in key)
        ]
        for key in keys_to_delete:
            del self._attempts[key]
        return len(keys_to_delete)

    def get_status_for_identifier(self, identifier: str, limits_config: dict = None) -> dict:
        """
        Return current rate-limit status for all buckets matching an identifier.
        """
        self._cleanup_old_entries()

        suffix = f":{identifier.lower()}"
        now = datetime.now()
        status = {}

        for key, timestamps in self._attempts.items():
            if not key.lower().endswith(suffix):
                continue

            matched_limit = None
            matched_window = 60
            for prefix, (max_attempts, window_minutes) in (limits_config or {}).items():
                if key.startswith(prefix):
                    matched_limit = max_attempts
                    matched_window = window_minutes
                    break

            window_start = now - timedelta(minutes=matched_window)
            recent = [timestamp for timestamp in timestamps if timestamp > window_start]
            if not recent and matched_limit is None:
                continue

            blocked = len(recent) >= matched_limit if matched_limit else False
            retry_after = 0
            if blocked and recent:
                oldest = min(recent)
                retry_after = max(
                    0,
                    int((oldest + timedelta(minutes=matched_window) - now).total_seconds())
                )

            status[key] = {
                "attempts_in_window": len(recent),
                "limit": matched_limit,
                "window_minutes": matched_window,
                "blocked": blocked,
                "retry_after_seconds": retry_after,
            }

        return status

    def get_blocked_ips_for_action(self, action: str, limits: dict) -> list:
        """Return IPs currently blocked for a specific action across IP buckets."""
        self._cleanup_old_entries()

        now = datetime.now()
        blocked = []
        seen_ips = set()

        for bucket_type, (max_attempts, window_minutes) in limits.items():
            prefix = f"{bucket_type}:{action}:"
            window_start = now - timedelta(minutes=window_minutes)

            for key, timestamps in self._attempts.items():
                if not key.startswith(prefix):
                    continue

                recent = [timestamp for timestamp in timestamps if timestamp > window_start]
                if len(recent) < max_attempts:
                    continue

                ip = key[len(prefix):]
                if ip in seen_ips:
                    continue

                blocked.append({
                    "ip": ip,
                    "bucket": bucket_type,
                    "attempts": len(recent),
                })
                seen_ips.add(ip)

        return blocked


# Singleton instance
rate_limiter = RateLimiter()


# =============================================================================
# Configuration
# =============================================================================

class RateLimitConfig:
    """
    Centralized rate limit configuration.
    Format: (max_attempts, window_minutes)
    """

    # --- Login endpoints ---
    LOGIN_BY_IP_ALL = (20, 60)           # 20 attempts per hour per IP
    LOGIN_BY_IP_FAILURES = (20, 60)      # 20 credential failures/hour/IP
    LOGIN_BY_ACCOUNT_IP_FAILURES = (5, 15)  # 5 failures/account/IP/15min
    LOGIN_ACCOUNT_OBSERVATION = (20, 1440)  # Alerting only; never blocks

    # --- Registration endpoints ---
    REGISTER_BY_IP_ALL = (10, 60)        # 10 attempts per hour per IP
    REGISTER_BY_IP_FAILURES = (5, 60)    # 5 failures per hour per IP
    REGISTER_BY_EMAIL = (3, 1440)        # 3 per email per 24h

    # --- Magic link recovery ---
    RECOVERY_BY_IP = (10, 60)            # 10 per hour per IP
    RECOVERY_BY_EMAIL = (3, 1440)        # 3 per email per 24h

    # --- OAuth ---
    OAUTH_BY_IP = (15, 60)               # 15 per hour per IP
    OAUTH_CALLBACK_FAILURES = (5, 60)    # 5 failures per hour

    # --- Email verification ---
    VERIFY_BY_IP = (20, 60)              # 20 per hour per IP
    VERIFY_FAILURES = (10, 60)           # 10 failures per hour


def check_rate_limits(
    request,
    ip_limit: Tuple[int, int] = None,
    identifier: str = None,
    identifier_limit: Tuple[int, int] = None,
    action_name: str = "request"
) -> Optional[dict]:
    """
    Check multiple rate limits at once.

    Args:
        request: FastAPI/Starlette request object
        ip_limit: (max_attempts, window_minutes) for IP-based limit
        identifier: Email or username to track
        identifier_limit: (max_attempts, window_minutes) for identifier-based limit
        action_name: Name of action for logging and key generation

    Returns:
        None if all limits pass, or error dict if blocked.
    """
    ip = get_client_ip(request)

    # Check IP limit (all attempts)
    if ip_limit:
        key = f"ip_all:{action_name}:{ip}"
        allowed, remaining = rate_limiter.is_allowed(key, ip_limit[0], ip_limit[1])

        if not allowed:
            retry_after = rate_limiter.get_retry_after(key, ip_limit[1])
            logger.warning(
                f"Rate limit exceeded: {action_name} by IP {ip} "
                f"(limit: {ip_limit[0]}/{ip_limit[1]}min)"
            )
            return {
                "status": "error",
                "message": "Too many attempts. Please try again later.",
                "retry_after_seconds": retry_after
            }

    # Check identifier limit (email/username)
    if identifier and identifier_limit:
        key = f"id:{action_name}:{identifier.lower()}"
        allowed, remaining = rate_limiter.is_allowed(
            key, identifier_limit[0], identifier_limit[1]
        )

        if not allowed:
            retry_after = rate_limiter.get_retry_after(key, identifier_limit[1])
            logger.warning(
                f"Rate limit exceeded: {action_name} for identifier {identifier} "
                f"(limit: {identifier_limit[0]}/{identifier_limit[1]}min)"
            )
            return {
                "status": "error",
                "message": "Too many attempts for this account. Please try again later.",
                "retry_after_seconds": retry_after
            }

    return None  # All checks passed


def check_failure_limit(
    request,
    action_name: str,
    limit: Tuple[int, int]
) -> Optional[dict]:
    """
    Check failure-only limit (doesn't record, just checks).

    Args:
        request: FastAPI/Starlette request object
        action_name: Name of action for key generation
        limit: (max_failures, window_minutes)

    Returns:
        None if under limit, or error dict if blocked.
    """
    ip = get_client_ip(request)
    key = f"ip_fail:{action_name}:{ip}"

    allowed, _ = rate_limiter.check_only(key, limit[0], limit[1])

    if not allowed:
        retry_after = rate_limiter.get_retry_after(key, limit[1])
        logger.warning(
            f"Failure rate limit exceeded: {action_name} by IP {ip} "
            f"(limit: {limit[0]}/{limit[1]}min)"
        )
        return {
            "status": "error",
            "message": "Too many failed attempts. Please try again later.",
            "retry_after_seconds": retry_after
        }

    return None


def record_failure(request, action_name: str, identifier: str = None):
    """
    Record a failed attempt for failure-based limiting.

    Args:
        request: FastAPI/Starlette request object
        action_name: Name of action for key generation
        identifier: Optional email/username to also track by identifier
    """
    ip = get_client_ip(request)
    rate_limiter.record_failure(f"ip_fail:{action_name}:{ip}")

    if identifier:
        rate_limiter.record_failure(f"id_fail:{action_name}:{identifier.lower()}")

    logger.debug(f"Recorded failure: {action_name} from IP {ip}")


def _normalize_login_identifier(identifier: str) -> str:
    return (identifier or "").strip().lower()[:128]


def _login_pair_key(request, identifier: str) -> str:
    return (
        f"pair_fail:login:{get_client_ip(request)}:"
        f"{_normalize_login_identifier(identifier)}"
    )


def check_login_failure_limits(request, identifier: str) -> Optional[dict]:
    """Check blocking login-failure buckets without recording an attempt."""
    ip = get_client_ip(request)
    checks = (
        (
            f"ip_fail:login:{ip}",
            RateLimitConfig.LOGIN_BY_IP_FAILURES,
            "Too many failed attempts from this network. Please try again later.",
        ),
        (
            _login_pair_key(request, identifier),
            RateLimitConfig.LOGIN_BY_ACCOUNT_IP_FAILURES,
            "Too many failed attempts for this account. Please try again later.",
        ),
    )

    for key, limit, message in checks:
        allowed, _ = rate_limiter.check_only(key, limit[0], limit[1])
        if not allowed:
            return {
                "status": "error",
                "message": message,
                "retry_after_seconds": rate_limiter.get_retry_after(
                    key,
                    limit[1],
                ),
            }
    return None


def record_login_failure(request, identifier: str) -> int:
    """Record one credential failure and return the account/IP failure count."""
    normalized = _normalize_login_identifier(identifier)
    ip = get_client_ip(request)
    rate_limiter.record_failure(
        f"ip_fail:login:{ip}",
        RateLimitConfig.LOGIN_BY_IP_FAILURES[1],
    )
    pair_count = rate_limiter.record_failure(
        _login_pair_key(request, normalized),
        RateLimitConfig.LOGIN_BY_ACCOUNT_IP_FAILURES[1],
    )
    observation_count = rate_limiter.record_failure(
        f"id_fail:login:{normalized}",
        RateLimitConfig.LOGIN_ACCOUNT_OBSERVATION[1],
    )
    if observation_count == RateLimitConfig.LOGIN_ACCOUNT_OBSERVATION[0]:
        logger.warning(
            "High login failure volume observed for account identifier %s",
            normalized,
        )
    return pair_count


def clear_login_failures(request, identifier: str) -> int:
    """Clear account-specific failures after a successful authentication."""
    normalized = _normalize_login_identifier(identifier)
    keys = (
        _login_pair_key(request, normalized),
        f"id_fail:login:{normalized}",
    )
    return sum(rate_limiter.clear_key(key) for key in keys)


def get_login_backoff_seconds(failure_count: int) -> int:
    """Return a small progressive delay for repeated credential failures."""
    if failure_count <= 0:
        return 0
    return min(2 ** failure_count, 8)
