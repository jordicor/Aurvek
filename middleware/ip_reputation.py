"""
IP Reputation Manager - Persistent behavioral scoring for scanner detection.

Tracks IP behavior over time in SQLite (db/security.db), detects slow scanners
via error ratio analysis, and remembers repeat offenders across restarts.

Architecture:
- Hot path is pure in-memory (nanoseconds per request)
- Periodic flush writes accumulated deltas to SQLite (~60s piggyback)
- Startup loads active/banned IPs into memory cache
- Score decay applies automatically based on inactivity
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from middleware.security_database import ensure_security_wal_mode, get_security_db

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class ReputationConfig:
    """All reputation system thresholds in one place."""

    # Scoring per event
    SCORE_PATTERN_MATCH = 50.0
    SCORE_404 = 3.0
    SCORE_403 = 2.0
    SCORE_BLOCKED_STILL_HITTING = 5.0
    SCORE_200_REWARD = -0.5  # floor at 0

    # Ban thresholds
    BAN_SCORE_SEVERE = 200.0        # 30 days
    BAN_SCORE_REPEAT = 50.0         # 7 days if times_banned >= 2
    BAN_SCORE_MODERATE = 50.0       # 24h
    BAN_SCORE_RECIDIVIST = 20.0     # 24h if times_banned >= 1
    BAN_ERROR_RATIO = 0.7           # 24h if ratio > 0.7 AND requests >= 15
    BAN_ERROR_MIN_REQUESTS = 15
    BAN_TIMES_CF_PERMANENT = 3      # permanent CF escalation

    # Ban durations (hours)
    BAN_HOURS_SEVERE = 30 * 24      # 30 days
    BAN_HOURS_REPEAT = 7 * 24       # 7 days
    BAN_HOURS_MODERATE = 24         # 24h

    # Flush interval (seconds)
    FLUSH_INTERVAL = 60.0

    # Purge old records
    PURGE_INTERVAL = 24 * 3600      # every 24h
    PURGE_MAX_AGE = 90 * 24 * 3600  # 90 days of inactivity

    # Decay: score *= 0.8 per 7 days of inactivity
    DECAY_FACTOR = 0.8
    DECAY_PERIOD = 7 * 24 * 3600    # 7 days


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class IPReputationDelta:
    """Accumulated changes in memory between flushes."""
    score_delta: float = 0.0
    total_requests: int = 0
    error_requests: int = 0
    pattern_hits: int = 0
    times_banned_delta: int = 0
    banned_until: Optional[float] = None
    last_seen: float = 0.0
    last_path: Optional[str] = None
    last_ban_reason: Optional[str] = None


@dataclass
class IPReputationRecord:
    """Full persisted state for an IP."""
    ip: str
    score: float = 0.0
    total_requests: int = 0
    error_requests: int = 0
    pattern_hits: int = 0
    times_banned: int = 0
    banned_until: Optional[float] = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_decay: float = 0.0
    last_path: Optional[str] = None
    last_ban_reason: Optional[str] = None


# =============================================================================
# Manager
# =============================================================================

class IPReputationManager:
    """
    Singleton manager for IP reputation tracking.

    Hot path (record_request, check_reputation_ban) is pure in-memory.
    Periodic flush writes accumulated deltas to SQLite.
    """

    def __init__(self):
        self._cache: Dict[str, IPReputationRecord] = {}
        self._pending: Dict[str, IPReputationDelta] = {}
        self._lock = asyncio.Lock()
        self._last_flush: float = 0.0
        self._last_purge: float = 0.0
        self._initialized = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def initialize(self):
        """Load IPs with score>0 or times_banned>0 into cache, apply decay."""
        ensure_security_wal_mode()

        try:
            async with get_security_db() as db:
                cursor = await db.execute(
                    "SELECT * FROM IP_REPUTATION WHERE score > 0 OR times_banned > 0"
                )
                rows = await cursor.fetchall()

                now = time.time()
                for row in rows:
                    record = IPReputationRecord(
                        ip=row["ip"],
                        score=row["score"],
                        total_requests=row["total_requests"],
                        error_requests=row["error_requests"],
                        pattern_hits=row["pattern_hits"],
                        times_banned=row["times_banned"],
                        banned_until=row["banned_until"],
                        first_seen=row["first_seen"],
                        last_seen=row["last_seen"],
                        last_decay=row["last_decay"],
                        last_path=row["last_path"],
                        last_ban_reason=row["last_ban_reason"],
                    )
                    # Apply decay at load time
                    self._apply_decay(record, now)
                    self._cache[record.ip] = record

                self._last_flush = now
                self._last_purge = now
                self._initialized = True
                logger.info("REPUTATION: Initialized with %d tracked IPs", len(self._cache))
        except Exception as exc:
            logger.error("REPUTATION: Initialization failed: %s", exc)
            self._initialized = True  # Continue without cached data

    async def shutdown(self):
        """Final flush to SQLite before process exit."""
        if self._pending:
            await self._flush()
            logger.info("REPUTATION: Shutdown flush completed")
        else:
            logger.info("REPUTATION: Shutdown - no pending data to flush")

    # -----------------------------------------------------------------
    # Hot path (called every request, pure in-memory)
    # -----------------------------------------------------------------

    def record_request(
        self,
        ip: str,
        status_code: int,
        path: str,
        is_pattern_hit: bool = False,
        is_blocked_ip: bool = False,
        is_whitelisted_path: bool = False,
        ban_already_handled: bool = False,
        external_ban_until: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Record a request and evaluate ban rules. Pure in-memory, O(1).

        Returns ban_info dict if a new ban was triggered, None otherwise.
        """
        now = time.time()

        # Get or create pending delta
        delta = self._pending.get(ip)
        if delta is None:
            delta = IPReputationDelta()
            self._pending[ip] = delta

        delta.total_requests += 1
        delta.last_seen = now
        delta.last_path = path

        # Score calculation
        if is_pattern_hit:
            delta.score_delta += ReputationConfig.SCORE_PATTERN_MATCH
            delta.pattern_hits += 1
            delta.error_requests += 1
        elif is_blocked_ip:
            delta.score_delta += ReputationConfig.SCORE_BLOCKED_STILL_HITTING
            delta.error_requests += 1
        elif status_code == 404 and not is_whitelisted_path:
            delta.score_delta += ReputationConfig.SCORE_404
            delta.error_requests += 1
        elif status_code == 403:
            delta.score_delta += ReputationConfig.SCORE_403
            delta.error_requests += 1
        elif status_code == 200:
            delta.score_delta += ReputationConfig.SCORE_200_REWARD
        # Other status codes (301, 302, 500, etc.) are neutral

        # Persist external ban if provided (tracker already decided)
        if external_ban_until is not None:
            delta.banned_until = external_ban_until
            delta.times_banned_delta += 1

        # Evaluate ban with combined cache + pending state
        ban_info = None
        if not ban_already_handled:
            ban_info = self._evaluate_ban(ip, delta, now)

        # Piggyback flush check
        if now - self._last_flush >= ReputationConfig.FLUSH_INTERVAL:
            asyncio.create_task(self._maybe_flush())

        return ban_info

    def check_reputation_ban(self, ip: str) -> Optional[float]:
        """
        Check if IP has an active reputation ban.
        Returns banned_until timestamp or None. O(1).
        """
        # Check pending first (most recent data)
        delta = self._pending.get(ip)
        if delta and delta.banned_until and delta.banned_until > time.time():
            return delta.banned_until

        # Check cache
        record = self._cache.get(ip)
        if record and record.banned_until and record.banned_until > time.time():
            return record.banned_until

        return None

    # -----------------------------------------------------------------
    # Ban evaluation
    # -----------------------------------------------------------------

    def _evaluate_ban(
        self, ip: str, delta: IPReputationDelta, now: float
    ) -> Optional[Dict[str, Any]]:
        """Check if combined state triggers a ban. Returns ban_info or None."""
        # Already banned in this flush cycle
        if delta.banned_until and delta.banned_until > now:
            return None

        # Combine cache + pending for full picture
        cached = self._cache.get(ip)
        total_score = delta.score_delta + (cached.score if cached else 0.0)
        total_score = max(0.0, total_score)  # Floor at 0
        total_requests = delta.total_requests + (cached.total_requests if cached else 0)
        error_requests = delta.error_requests + (cached.error_requests if cached else 0)
        times_banned = delta.times_banned_delta + (cached.times_banned if cached else 0)
        pattern_hits = delta.pattern_hits + (cached.pattern_hits if cached else 0)

        ban_hours = 0
        ban_reason = ""
        cf_escalate = False

        # Rule 1: Severe score
        if total_score >= ReputationConfig.BAN_SCORE_SEVERE:
            ban_hours = ReputationConfig.BAN_HOURS_SEVERE
            ban_reason = f"Reputation score {total_score:.0f} >= {ReputationConfig.BAN_SCORE_SEVERE:.0f}"
            cf_escalate = True

        # Rule 2: Moderate score + repeat offender
        elif (total_score >= ReputationConfig.BAN_SCORE_REPEAT
              and times_banned >= 2):
            ban_hours = ReputationConfig.BAN_HOURS_REPEAT
            ban_reason = f"Repeat offender: score {total_score:.0f}, banned {times_banned} times"
            cf_escalate = True

        # Rule 3: Moderate score (first time)
        elif total_score >= ReputationConfig.BAN_SCORE_MODERATE:
            ban_hours = ReputationConfig.BAN_HOURS_MODERATE
            ban_reason = f"Reputation score {total_score:.0f} >= {ReputationConfig.BAN_SCORE_MODERATE:.0f}"

        # Rule 4: Recidivist with lower score
        elif times_banned >= 1 and total_score >= ReputationConfig.BAN_SCORE_RECIDIVIST:
            ban_hours = ReputationConfig.BAN_HOURS_MODERATE
            ban_reason = f"Recidivist: score {total_score:.0f}, previously banned {times_banned} times"

        # Rule 5: High error ratio
        elif total_requests >= ReputationConfig.BAN_ERROR_MIN_REQUESTS:
            error_ratio = error_requests / total_requests if total_requests > 0 else 0.0
            if error_ratio > ReputationConfig.BAN_ERROR_RATIO:
                ban_hours = ReputationConfig.BAN_HOURS_MODERATE
                ban_reason = (
                    f"Error ratio {error_ratio:.2f} > {ReputationConfig.BAN_ERROR_RATIO} "
                    f"({error_requests}/{total_requests} requests)"
                )

        if not ban_hours:
            return None

        # Rule 6: Permanent CF for chronic offenders (applied on top)
        if times_banned >= ReputationConfig.BAN_TIMES_CF_PERMANENT:
            cf_escalate = True

        banned_until = now + ban_hours * 3600
        delta.banned_until = banned_until
        delta.times_banned_delta += 1
        delta.last_ban_reason = ban_reason

        logger.warning(
            "REPUTATION: Ban triggered for %s - %s (ban_hours=%d, cf_escalate=%s)",
            ip, ban_reason, ban_hours, cf_escalate,
        )

        return {
            "ip": ip,
            "ban_hours": ban_hours,
            "reason": ban_reason,
            "cf_escalate": cf_escalate,
            "banned_until": banned_until,
            "score": total_score,
            "times_banned": times_banned + 1,
            "pattern_hits": pattern_hits,
        }

    # -----------------------------------------------------------------
    # Decay
    # -----------------------------------------------------------------

    def _apply_decay(self, record: IPReputationRecord, now: float):
        """Apply score decay based on inactivity. Modifies record in place."""
        if record.score <= 0:
            return

        elapsed = now - record.last_decay
        if elapsed < ReputationConfig.DECAY_PERIOD:
            return

        periods = int(elapsed / ReputationConfig.DECAY_PERIOD)
        if periods <= 0:
            return

        record.score *= ReputationConfig.DECAY_FACTOR ** periods
        record.last_decay = now

        # Zero out negligible scores
        if record.score < 0.1:
            record.score = 0.0

    # -----------------------------------------------------------------
    # Flush (piggyback, every ~60s)
    # -----------------------------------------------------------------

    async def _maybe_flush(self):
        """Piggyback flush: swap pending dict and write to SQLite."""
        async with self._lock:
            now = time.time()
            if now - self._last_flush < ReputationConfig.FLUSH_INTERVAL:
                return

            if not self._pending:
                self._last_flush = now
                return

            # Atomic swap
            to_flush = self._pending
            self._pending = {}
            self._last_flush = now

        # Write outside the lock
        try:
            await self._write_batch(to_flush)
        except Exception as exc:
            logger.error("REPUTATION: Flush failed, merging back %d IPs: %s", len(to_flush), exc)
            # Merge unflushed deltas back into pending
            async with self._lock:
                for ip, delta in to_flush.items():
                    existing = self._pending.get(ip)
                    if existing:
                        existing.score_delta += delta.score_delta
                        existing.total_requests += delta.total_requests
                        existing.error_requests += delta.error_requests
                        existing.pattern_hits += delta.pattern_hits
                        existing.times_banned_delta += delta.times_banned_delta
                        if delta.banned_until:
                            existing.banned_until = delta.banned_until
                        if delta.last_seen > (existing.last_seen or 0):
                            existing.last_seen = delta.last_seen
                            existing.last_path = delta.last_path
                        if delta.last_ban_reason:
                            existing.last_ban_reason = delta.last_ban_reason
                    else:
                        self._pending[ip] = delta

        # Piggyback purge check
        now = time.time()
        if now - self._last_purge >= ReputationConfig.PURGE_INTERVAL:
            await self._purge_old_records(now)
            self._last_purge = now

    async def _flush(self):
        """Force flush (used during shutdown)."""
        async with self._lock:
            if not self._pending:
                return
            to_flush = self._pending
            self._pending = {}
        await self._write_batch(to_flush)

    async def _write_batch(self, batch: Dict[str, IPReputationDelta]):
        """Write a batch of deltas to SQLite using UPSERT."""
        if not batch:
            return

        async with get_security_db() as db:
            for ip, delta in batch.items():
                now = delta.last_seen or time.time()
                cached = self._cache.get(ip)

                if cached:
                    # Update existing record
                    new_score = max(0.0, cached.score + delta.score_delta)
                    new_total = cached.total_requests + delta.total_requests
                    new_errors = cached.error_requests + delta.error_requests
                    new_pattern = cached.pattern_hits + delta.pattern_hits
                    new_banned_count = cached.times_banned + delta.times_banned_delta
                    new_banned_until = delta.banned_until or cached.banned_until
                    new_last_path = delta.last_path or cached.last_path
                    new_last_reason = delta.last_ban_reason or cached.last_ban_reason

                    await db.execute(
                        """UPDATE IP_REPUTATION SET
                            score = ?, total_requests = ?, error_requests = ?,
                            pattern_hits = ?, times_banned = ?, banned_until = ?,
                            last_seen = ?, last_path = ?, last_ban_reason = ?
                        WHERE ip = ?""",
                        (
                            new_score, new_total, new_errors,
                            new_pattern, new_banned_count, new_banned_until,
                            now, new_last_path, new_last_reason,
                            ip,
                        ),
                    )

                    # Update cache
                    cached.score = new_score
                    cached.total_requests = new_total
                    cached.error_requests = new_errors
                    cached.pattern_hits = new_pattern
                    cached.times_banned = new_banned_count
                    cached.banned_until = new_banned_until
                    cached.last_seen = now
                    cached.last_path = new_last_path
                    cached.last_ban_reason = new_last_reason
                else:
                    # Insert new record
                    new_score = max(0.0, delta.score_delta)
                    await db.execute(
                        """INSERT INTO IP_REPUTATION
                            (ip, score, total_requests, error_requests, pattern_hits,
                             times_banned, banned_until, first_seen, last_seen,
                             last_decay, last_path, last_ban_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ip) DO UPDATE SET
                            score = score + excluded.score,
                            total_requests = total_requests + excluded.total_requests,
                            error_requests = error_requests + excluded.error_requests,
                            pattern_hits = pattern_hits + excluded.pattern_hits,
                            times_banned = times_banned + excluded.times_banned,
                            banned_until = COALESCE(excluded.banned_until, banned_until),
                            last_seen = excluded.last_seen,
                            last_path = COALESCE(excluded.last_path, last_path),
                            last_ban_reason = COALESCE(excluded.last_ban_reason, last_ban_reason)
                        """,
                        (
                            ip, new_score, delta.total_requests, delta.error_requests,
                            delta.pattern_hits, delta.times_banned_delta, delta.banned_until,
                            now, now, now, delta.last_path, delta.last_ban_reason,
                        ),
                    )

                    # Add to cache
                    self._cache[ip] = IPReputationRecord(
                        ip=ip,
                        score=new_score,
                        total_requests=delta.total_requests,
                        error_requests=delta.error_requests,
                        pattern_hits=delta.pattern_hits,
                        times_banned=delta.times_banned_delta,
                        banned_until=delta.banned_until,
                        first_seen=now,
                        last_seen=now,
                        last_decay=now,
                        last_path=delta.last_path,
                        last_ban_reason=delta.last_ban_reason,
                    )

            await db.commit()

    # -----------------------------------------------------------------
    # Purge (piggyback on flush)
    # -----------------------------------------------------------------

    async def _purge_old_records(self, now: float):
        """Delete stale records from DB and cache."""
        cutoff = now - ReputationConfig.PURGE_MAX_AGE
        try:
            async with get_security_db() as db:
                cursor = await db.execute(
                    """DELETE FROM IP_REPUTATION
                    WHERE score <= 0 AND times_banned = 0 AND last_seen < ?""",
                    (cutoff,),
                )
                deleted = cursor.rowcount
                await db.commit()

            # Clean cache too
            stale_ips = [
                ip for ip, rec in self._cache.items()
                if rec.score <= 0 and rec.times_banned == 0 and rec.last_seen < cutoff
            ]
            for ip in stale_ips:
                self._cache.pop(ip, None)

            if deleted or stale_ips:
                logger.info(
                    "REPUTATION: Purged %d DB records, %d cache entries (older than 90 days)",
                    deleted, len(stale_ips),
                )
        except Exception as exc:
            logger.error("REPUTATION: Purge failed: %s", exc)

    # -----------------------------------------------------------------
    # Admin queries
    # -----------------------------------------------------------------

    async def get_top_ips(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return top IPs by score from DB."""
        try:
            async with get_security_db() as db:
                cursor = await db.execute(
                    """SELECT * FROM IP_REPUTATION
                    ORDER BY score DESC, times_banned DESC
                    LIMIT ?""",
                    (limit,),
                )
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("REPUTATION: get_top_ips failed: %s", exc)
            return []

    async def get_ip_detail(self, ip: str) -> Optional[Dict[str, Any]]:
        """Return full record for a single IP (DB + pending data)."""
        try:
            async with get_security_db() as db:
                cursor = await db.execute(
                    "SELECT * FROM IP_REPUTATION WHERE ip = ?", (ip,)
                )
                row = await cursor.fetchone()
                if row:
                    result = self._row_to_dict(row)
                    # Enrich with pending data
                    delta = self._pending.get(ip)
                    if delta:
                        result["pending_score_delta"] = delta.score_delta
                        result["pending_requests"] = delta.total_requests
                    return result

            # Check pending-only IPs (not yet flushed to DB)
            delta = self._pending.get(ip)
            if delta:
                now = time.time()
                return {
                    "ip": ip,
                    "score": max(0.0, delta.score_delta),
                    "total_requests": delta.total_requests,
                    "error_requests": delta.error_requests,
                    "error_ratio": (
                        delta.error_requests / delta.total_requests
                        if delta.total_requests > 0 else 0.0
                    ),
                    "pattern_hits": delta.pattern_hits,
                    "times_banned": delta.times_banned_delta,
                    "banned_until": delta.banned_until,
                    "first_seen": delta.last_seen,
                    "last_seen": delta.last_seen,
                    "last_decay": now,
                    "last_path": delta.last_path,
                    "last_ban_reason": delta.last_ban_reason,
                    "pending_only": True,
                }
            return None
        except Exception as exc:
            logger.error("REPUTATION: get_ip_detail failed: %s", exc)
            return None

    async def get_stats(self) -> Dict[str, Any]:
        """Summary stats for admin panel."""
        now = time.time()
        tracked = len(self._cache)
        banned = sum(
            1 for rec in self._cache.values()
            if rec.banned_until and rec.banned_until > now
        )
        pending_count = len(self._pending)

        # Also count pending bans
        for delta in self._pending.values():
            if delta.banned_until and delta.banned_until > now:
                banned += 1

        return {
            "tracked_ips": tracked,
            "reputation_banned": banned,
            "pending_flush": pending_count,
            "initialized": self._initialized,
        }

    async def reset_ip_score(self, ip: str) -> bool:
        """Admin action: reset score + error counters to 0, keep times_banned history."""
        try:
            async with get_security_db() as db:
                cursor = await db.execute(
                    """UPDATE IP_REPUTATION
                    SET score = 0, banned_until = NULL,
                        error_requests = 0, total_requests = 0
                    WHERE ip = ?""",
                    (ip,),
                )
                await db.commit()
                updated = cursor.rowcount > 0

            # Update cache
            cached = self._cache.get(ip)
            if cached:
                cached.score = 0.0
                cached.banned_until = None
                cached.error_requests = 0
                cached.total_requests = 0

            # Clear any pending data
            delta = self._pending.get(ip)
            if delta:
                delta.score_delta = 0.0
                delta.banned_until = None
                delta.error_requests = 0
                delta.total_requests = 0

            if updated:
                logger.info("REPUTATION: Score reset for IP %s (ban history preserved)", ip)
            return updated
        except Exception as exc:
            logger.error("REPUTATION: reset_ip_score failed for %s: %s", ip, exc)
            return False

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a DB row to a plain dict."""
        return {
            "ip": row["ip"],
            "score": row["score"],
            "total_requests": row["total_requests"],
            "error_requests": row["error_requests"],
            "error_ratio": (
                row["error_requests"] / row["total_requests"]
                if row["total_requests"] > 0 else 0.0
            ),
            "pattern_hits": row["pattern_hits"],
            "times_banned": row["times_banned"],
            "banned_until": row["banned_until"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "last_decay": row["last_decay"],
            "last_path": row["last_path"],
            "last_ban_reason": row["last_ban_reason"],
        }


# Singleton
reputation_manager = IPReputationManager()
