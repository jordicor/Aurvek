"""
Security Middleware for Scanner/Bot Protection.

Provides multi-layer protection:
1. Instant block for known malicious patterns (wp-*, .env, .git, etc.)
2. Progressive block based on 404 accumulation (10 404s in 3 min = ban)
3. Shared state support (Redis) with controlled fallback to in-memory
4. Optional Cloudflare escalation for repeated offenders

Compatible with:
- FastAPI standalone (direct connection)
- FastAPI + nginx (X-Forwarded-For, X-Real-IP)
- FastAPI + nginx + Cloudflare (CF-Connecting-IP)
"""

import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from middleware.ip_reputation import reputation_manager
from middleware.nginx_blocklist import nginx_blocklist_manager

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class SecurityConfig:
    """Security middleware configuration."""

    # Instant block patterns - 1 request = immediate ban
    # These are ALWAYS malicious, no legitimate user would request them
    INSTANT_BLOCK_PATTERNS = [
        # WordPress
        r"^/wp-",
        r"^/wordpress",
        r"/xmlrpc\.php",
        r"/wp-login\.php",
        r"/wp-config\.php",

        # PHP admin panels
        r"^/phpmyadmin",
        r"^/pma",
        r"^/myadmin",
        r"^/mysql",
        r"^/phpMyAdmin",

        # Hidden files and configs
        r"/\.git",
        r"/\.env",
        r"/\.aws",
        r"/\.ssh",
        r"/\.htaccess",
        r"/\.htpasswd",
        r"/\.DS_Store",
        r"/\.svn",
        r"/\.hg",

        # Config files
        r"/config\.(php|yml|yaml|json|ini|xml|bak)$",
        r"/settings\.(php|yml|yaml|json|ini|xml)$",
        r"/database\.(php|yml|yaml|json|ini|xml)$",
        r"/credentials",
        r"/secrets",

        # Shell/exploit attempts
        r"/(shell|cmd|exec|eval|system|passthru)\.php",
        r"/c99\.php",
        r"/r57\.php",
        r"/alfa\.php",
        r"/wso\.php",
        r"/b374k",

        # CGI attacks
        r"^/cgi-bin/",
        r"^/cgi/",

        # Admin panels
        r"^/admin\.php",
        r"^/administrator",
        r"^/admin/.*\.(php|asp|aspx|jsp)$",
        r"^/manager/html",
        r"^/manager/status",

        # Backup files
        r"\.(sql|bak|backup|old|orig|save|swp|tmp)$",
        r"\.(tar|tar\.gz|tgz|zip|rar|7z)$",
        r"~$",  # Backup files like config.php~

        # AWS/Cloud metadata
        r"/latest/meta-data",
        r"/169\.254\.169\.254",

        # Common CMS
        r"^/joomla",
        r"^/drupal",
        r"^/magento",
        r"^/typo3",

        # Java/Tomcat (specific endpoints, not generic /manager/ which conflicts with app routes)
        r"^/manager/html",
        r"^/manager/status",
        r"^/manager/text",
        r"^/manager/jmxproxy",
        r"^/jenkins",
        r"^/hudson",
        r"^/solr",
        r"^/actuator",
        r"^/console",

        # Debug endpoints
        r"/phpinfo\.php",
        r"/info\.php",
        r"/test\.php",
        r"/debug",
        r"/trace",

        # GraphQL introspection abuse
        r"/graphql.*introspection",

        # Kubernetes/Docker
        r"^/k8s/",
        r"^/kubernetes/",
        r"^/docker",
        r"^/portainer",

        # Microsoft Exchange / Outlook
        r"^/owa/",
        r"^/ecp/",
        r"/autodiscover",
        r"/aspnet_client",
        r"^/remote/login",

        # Non-Python file extensions (ASP.NET, Java, ColdFusion, Perl)
        r"\.(asp|aspx|jsp|jspx|do|action)$",
        r"\.(cgi|pl|cfm|cfc)$",

        # Framework debug tools (Laravel, Symfony, Django)
        r"^/telescope",
        r"^/horizon",
        r"^/_profiler",
        r"^/__debug__",

        # ASP.NET diagnostics
        r"/elmah\.axd",
        r"/trace\.axd",
        r"/web\.config",

        # Apache status pages
        r"^/server-status",
        r"^/server-info",

        # Dependency / build files
        r"^/vendor/",
        r"^/node_modules/",
        r"/composer\.(json|lock)$",
        r"/package\.json$",
        r"/package-lock\.json$",
        r"/requirements\.txt$",
        r"/Pipfile",
        r"/Gemfile",
        r"/Makefile$",
        r"/Gruntfile",
        r"/Gulpfile",
        r"/webpack\.config",

        # IDE / editor configs
        r"/\.vscode/",
        r"/\.idea/",
        r"/\.project$",
        r"/\.settings/",

        # Java enterprise (JBoss, WebLogic, Axis, Struts)
        r"^/jmx-console",
        r"^/web-console",
        r"^/invoker/",
        r"/jolokia",
        r"/hawtio",
        r"/wls-wsat",
        r"/ws_utc",
        r"^/axis2/",
        r"^/struts/",

        # ColdFusion
        r"^/CFIDE",
        r"^/cfide",
        r"^/lucee",
        r"^/railo",

        # Monitoring tools
        r"^/nagios",
        r"^/zabbix",
        r"^/munin",
        r"^/cacti",
        r"^/grafana",
        r"^/kibana",
        r"^/prometheus",

        # Router / IoT exploits
        r"^/HNAP1/",
        r"^/boaform/",
        r"^/GponForm/",
        r"^/goform/",
        r"/setup\.cgi",
        r"/apply\.cgi",

        # Windows metadata
        r"/Thumbs\.db",
        r"/desktop\.ini",

        # CMS additional
        r"^/prestashop",
        r"^/shopify",
        r"^/moodle",
        r"^/confluence",
        r"^/bitbucket",

        # Other classic scanner targets
        r"/login\.php",
        r"/index\.php",
        r"/crossdomain\.xml",
        r"/clientaccesspolicy\.xml",
        r"/nmaplowercheck",
    ]

    # Compiled patterns for performance
    COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INSTANT_BLOCK_PATTERNS]

    # Whitelist - these 404s don't count toward threshold
    # Browsers/bots request these automatically
    WHITELIST_PATHS: Set[str] = {
        "/favicon.ico",
        "/favicon.png",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/apple-touch-icon-120x120.png",
        "/apple-touch-icon-152x152.png",
        "/apple-touch-icon-180x180.png",
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/browserconfig.xml",
        "/manifest.json",
        "/site.webmanifest",
        "/humans.txt",
        "/ads.txt",
        "/security.txt",
    }

    WHITELIST_PREFIXES = [
        "/.well-known/",  # ACME, security.txt, etc.
    ]

    # Landing page routes - these are user-generated content, skip pattern blocking
    # Only 404 accumulation applies to these routes
    LANDING_PAGE_PATTERNS = [
        re.compile(r"^/p/[a-zA-Z0-9]{8}/"),      # Public landing pages: /p/{public_id}/{slug}/
        re.compile(r"^/\d+/[^/]+/"),             # Legacy prompt routes: /{prompt_id}/{prompt_name}/
    ]

    # Known legitimate bots (more permissive threshold)
    KNOWN_BOTS = [
        "Googlebot",
        "Bingbot",
        "Slurp",      # Yahoo
        "DuckDuckBot",
        "Yandex",
        "Baiduspider",
        "facebot",    # Facebook
        "Twitterbot",
        "LinkedInBot",
        "Applebot",
    ]

    # Thresholds: (max_404s, window_minutes)
    NORMAL_THRESHOLD: Tuple[int, int] = (10, 3)   # 10 404s in 3 min
    BOT_THRESHOLD: Tuple[int, int] = (50, 3)      # 50 404s in 3 min for known bots

    # Admin IPs - resolved dynamically from DDNS hostnames, never blocked
    # Updated by tools/cf_whitelist_admin_ips.py
    ADMIN_DDNS_HOSTS: List[str] = [
        "your-ddns-1.example.com",   # Location 1
        "your-ddns-2.example.com",  # Location 2
    ]
    _admin_ips: Set[str] = set()
    _admin_ips_last_resolved: float = 0.0
    _ADMIN_RESOLVE_INTERVAL: int = 300  # Re-resolve every 5 minutes

    @classmethod
    def get_admin_ips(cls) -> Set[str]:
        now = time.time()
        if now - cls._admin_ips_last_resolved > cls._ADMIN_RESOLVE_INTERVAL:
            import socket
            new_ips: Set[str] = set()
            for host in cls.ADMIN_DDNS_HOSTS:
                try:
                    new_ips.add(socket.gethostbyname(host))
                except socket.gaierror:
                    pass
            if new_ips:
                cls._admin_ips = new_ips
            cls._admin_ips_last_resolved = now
        return cls._admin_ips

    # Block duration
    BLOCK_DURATION_HOURS: int = 24

    # Response for blocked requests (444 = nginx silent drop, 403 for others)
    BLOCK_STATUS_CODE: int = 403
    BLOCK_MESSAGE: str = "Forbidden"

    # Shared backend mode
    # - auto: use Redis when available, fallback to in-memory on error
    # - off: force in-memory backend only
    REDIS_MODE: str = os.getenv("SECURITY_REDIS_MODE", "auto").strip().lower()
    REDIS_KEY_PREFIX: str = os.getenv("SECURITY_REDIS_PREFIX", "security").strip() or "security"
    REDIS_RECOVERY_COOLDOWN_SECONDS: int = int(os.getenv("SECURITY_REDIS_RECOVERY_COOLDOWN_SECONDS", "30"))
    REDIS_HEALTHCHECK_INTERVAL_SECONDS: int = int(os.getenv("SECURITY_REDIS_HEALTHCHECK_INTERVAL_SECONDS", "10"))

    # Telemetry retention
    EVENTS_RETENTION: int = int(os.getenv("SECURITY_EVENTS_RETENTION", "500"))

    # Optional Cloudflare escalation
    CF_ESCALATION_ENABLED: bool = os.getenv("SECURITY_CF_ESCALATION_ENABLED", "0") == "1"
    CF_ESCALATION_THRESHOLD: int = int(os.getenv("SECURITY_CF_ESCALATION_THRESHOLD", "3"))
    CF_ESCALATION_TIMEOUT_SECONDS: float = float(os.getenv("SECURITY_CF_ESCALATION_TIMEOUT_SECONDS", "5"))
    CF_ESCALATION_NOTES_PREFIX: str = os.getenv("SECURITY_CF_ESCALATION_NOTES_PREFIX", "aurvek-security-auto")
    CF_ACCOUNT_ID: str = os.getenv("SECURITY_CF_ACCOUNT_ID", "").strip()
    CF_EMAIL: str = os.getenv("CLOUDFLARE_EMAIL", "").strip().strip('"').strip("'")
    CF_API_KEY: str = os.getenv("CLOUDFLARE_API_KEY", "").strip().strip('"').strip("'")
    CF_ZONE_ID: str = os.getenv("CLOUDFLARE_ZONE_ID", "").strip().strip('"').strip("'")
    CF_ESCALATION_BLOCK_HOURS: int = int(os.getenv("SECURITY_CF_ESCALATION_BLOCK_HOURS", "168"))
    # Opportunistic Cloudflare cleanup (no cron/worker): triggered on new block events only.
    # Set interval <= 0 to disable.
    CF_CLEANUP_INTERVAL_SECONDS: int = int(os.getenv("SECURITY_CF_CLEANUP_INTERVAL_SECONDS", "600"))
    CF_CLEANUP_MAX_IPS_PER_RUN: int = int(os.getenv("SECURITY_CF_CLEANUP_MAX_IPS_PER_RUN", "50"))
    CF_CLEANUP_TIME_BUDGET_SECONDS: float = float(os.getenv("SECURITY_CF_CLEANUP_TIME_BUDGET_SECONDS", "1.5"))


# =============================================================================
# Utility helpers
# =============================================================================


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> float:
    return time.time()


def _iso_from_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _reason_category(reason: str) -> str:
    reason_lower = (reason or "").lower()
    if reason_lower.startswith("instant block pattern"):
        return "instant_pattern"
    if reason_lower.startswith("too many 404s"):
        return "too_many_404s"
    if reason_lower.startswith("manual"):
        return "manual"
    return "other"


def _normalize_ip(ip: str) -> str:
    value = (ip or "").strip()
    if not value:
        raise ValueError("IP is required")
    try:
        return str(ipaddress.ip_address(value))
    except Exception as exc:
        raise ValueError(f"Invalid IP address: {ip}") from exc


# =============================================================================
# Storage Backends
# =============================================================================

class BaseSecurityBackend:
    """Backend contract for tracker storage."""

    name = "base"

    async def ping(self) -> bool:
        return True

    async def is_blocked(self, ip: str) -> bool:
        raise NotImplementedError

    async def block_ip(self, ip: str, hours: int, reason: str, source: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def unblock_ip(self, ip: str) -> bool:
        raise NotImplementedError

    async def record_404(self, ip: str) -> None:
        raise NotImplementedError

    async def get_404_count(self, ip: str, window_minutes: int) -> int:
        raise NotImplementedError

    async def get_ip_block_count(self, ip: str) -> int:
        raise NotImplementedError

    async def get_stats(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def is_cloudflare_escalated(self, ip: str) -> bool:
        raise NotImplementedError

    async def mark_cloudflare_escalated(self, ip: str, details: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def get_blocked_ips(self, limit: int = 200) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def get_cloudflare_escalation(self, ip: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def clear_cloudflare_escalation(self, ip: str) -> bool:
        raise NotImplementedError

    async def get_all_cloudflare_escalated_ips(self) -> List[str]:
        raise NotImplementedError

    async def extend_block(self, ip: str, hours: int) -> bool:
        raise NotImplementedError


class InMemorySecurityBackend(BaseSecurityBackend):
    """Single-process in-memory tracker backend."""

    name = "memory"

    def __init__(self):
        self._404_counts: Dict[str, List[float]] = defaultdict(list)
        self._blocked_ips: Dict[str, float] = {}
        self._blocked_meta: Dict[str, Dict[str, Any]] = {}
        self._block_events_total: int = 0
        self._block_events_by_reason: Counter = Counter()
        self._ip_block_count: Counter = Counter()
        self._recent_events: Deque[Dict[str, Any]] = deque(maxlen=SecurityConfig.EVENTS_RETENTION)
        self._cloudflare_escalated: Dict[str, Dict[str, Any]] = {}
        self._cloudflare_escalations_total: int = 0
        self._last_cleanup: float = _now_ts()
        self._lock = asyncio.Lock()

    async def is_blocked(self, ip: str) -> bool:
        async with self._lock:
            unblock_ts = self._blocked_ips.get(ip)
            if unblock_ts is None:
                return False
            if _now_ts() >= unblock_ts:
                self._blocked_ips.pop(ip, None)
                self._blocked_meta.pop(ip, None)
                return False
            return True

    async def block_ip(self, ip: str, hours: int, reason: str, source: str) -> Dict[str, Any]:
        async with self._lock:
            unblock_ts = _now_ts() + max(1, int(hours * 3600))
            self._blocked_ips[ip] = unblock_ts

            self._block_events_total += 1
            category = _reason_category(reason)
            self._block_events_by_reason[category] += 1
            self._ip_block_count[ip] += 1

            event = {
                "timestamp": _utc_now().isoformat(),
                "ip": ip,
                "reason": reason,
                "reason_category": category,
                "source": source,
                "blocked_until": _iso_from_ts(unblock_ts),
                "block_count_for_ip": self._ip_block_count[ip],
            }
            self._blocked_meta[ip] = dict(event)
            self._recent_events.appendleft(event)
            self._cleanup_old_entries()
            return event

    async def unblock_ip(self, ip: str) -> bool:
        async with self._lock:
            removed = self._blocked_ips.pop(ip, None) is not None
            self._blocked_meta.pop(ip, None)
            return removed

    async def record_404(self, ip: str) -> None:
        async with self._lock:
            self._404_counts[ip].append(_now_ts())
            self._cleanup_old_entries()

    async def get_404_count(self, ip: str, window_minutes: int) -> int:
        async with self._lock:
            cutoff = _now_ts() - window_minutes * 60
            recent = [ts for ts in self._404_counts.get(ip, []) if ts > cutoff]
            self._404_counts[ip] = recent
            return len(recent)

    async def get_ip_block_count(self, ip: str) -> int:
        async with self._lock:
            return int(self._ip_block_count.get(ip, 0))

    async def get_stats(self) -> Dict[str, Any]:
        async with self._lock:
            self._cleanup_old_entries()
            blocked_ips = {
                ip: _iso_from_ts(unblock_ts)
                for ip, unblock_ts in self._blocked_ips.items()
            }
            return {
                "backend": self.name,
                "blocked_ips_count": len(self._blocked_ips),
                "tracked_ips_count": len(self._404_counts),
                "blocked_ips": blocked_ips,
                "block_events_total": self._block_events_total,
                "block_events_by_reason": dict(self._block_events_by_reason),
                "cloudflare_escalations_total": self._cloudflare_escalations_total,
                "recent_events_count": len(self._recent_events),
            }

    async def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        async with self._lock:
            if limit <= 0:
                return []
            return list(self._recent_events)[:limit]

    async def is_cloudflare_escalated(self, ip: str) -> bool:
        async with self._lock:
            return ip in self._cloudflare_escalated

    async def mark_cloudflare_escalated(self, ip: str, details: Dict[str, Any]) -> None:
        async with self._lock:
            if ip not in self._cloudflare_escalated:
                self._cloudflare_escalations_total += 1
            self._cloudflare_escalated[ip] = details

    async def get_blocked_ips(self, limit: int = 200) -> List[Dict[str, Any]]:
        async with self._lock:
            self._cleanup_old_entries()
            if limit <= 0:
                return []

            entries = sorted(
                self._blocked_ips.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:limit]

            blocked: List[Dict[str, Any]] = []
            for ip, unblock_ts in entries:
                event = dict(self._blocked_meta.get(ip, {}))
                event.update({
                    "ip": ip,
                    "blocked_until": _iso_from_ts(unblock_ts),
                    "is_active": _now_ts() < unblock_ts,
                })
                cloudflare = self._cloudflare_escalated.get(ip)
                event["cloudflare"] = dict(cloudflare) if isinstance(cloudflare, dict) else None
                event["cloudflare_synced"] = bool(cloudflare)
                blocked.append(event)
            return blocked

    async def get_cloudflare_escalation(self, ip: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            details = self._cloudflare_escalated.get(ip)
            return dict(details) if isinstance(details, dict) else None

    async def clear_cloudflare_escalation(self, ip: str) -> bool:
        async with self._lock:
            return self._cloudflare_escalated.pop(ip, None) is not None

    async def get_all_cloudflare_escalated_ips(self) -> List[str]:
        async with self._lock:
            return list(self._cloudflare_escalated.keys())

    async def extend_block(self, ip: str, hours: int) -> bool:
        async with self._lock:
            if ip not in self._blocked_ips:
                return False
            new_ts = _now_ts() + max(1, int(hours * 3600))
            self._blocked_ips[ip] = new_ts
            if ip in self._blocked_meta:
                self._blocked_meta[ip]["blocked_until"] = _iso_from_ts(new_ts)
            return True

    def _cleanup_old_entries(self) -> None:
        now = _now_ts()
        if now - self._last_cleanup < 300:
            return

        cutoff_404 = now - 3600
        empty_404_ips = []
        for ip, timestamps in self._404_counts.items():
            filtered = [ts for ts in timestamps if ts > cutoff_404]
            self._404_counts[ip] = filtered
            if not filtered:
                empty_404_ips.append(ip)

        for ip in empty_404_ips:
            self._404_counts.pop(ip, None)

        expired_ips = [ip for ip, unblock_ts in self._blocked_ips.items() if now >= unblock_ts]
        for ip in expired_ips:
            self._blocked_ips.pop(ip, None)
            self._blocked_meta.pop(ip, None)

        self._last_cleanup = now


class RedisSecurityBackend(BaseSecurityBackend):
    """Redis-backed tracker backend shared across workers."""

    name = "redis"

    def __init__(self, redis_client: Any, key_prefix: str):
        self.redis = redis_client
        self.prefix = key_prefix.strip(":")

    def _k(self, suffix: str) -> str:
        return f"{self.prefix}:{suffix}"

    def _k_404(self, ip: str) -> str:
        return self._k(f"404:{ip}")

    def _k_block_meta(self, ip: str) -> str:
        return self._k(f"blocked:meta:{ip}")

    def _k_blocked_z(self) -> str:
        return self._k("blocked:z")

    def _k_total_blocks(self) -> str:
        return self._k("metrics:block_events_total")

    def _k_reason_blocks(self) -> str:
        return self._k("metrics:block_events_by_reason")

    def _k_ip_block_count(self, ip: str) -> str:
        return self._k(f"metrics:block_events:ip:{ip}")

    def _k_recent_events(self) -> str:
        return self._k("events")

    def _k_cf_escalated(self, ip: str) -> str:
        return self._k(f"cloudflare:escalated:{ip}")

    def _k_cf_total(self) -> str:
        return self._k("metrics:cloudflare_escalations_total")

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    async def ping(self) -> bool:
        pong = await self.redis.ping()
        if isinstance(pong, bool):
            return pong
        pong = self._decode(pong)
        return str(pong).upper() == "PONG"

    async def is_blocked(self, ip: str) -> bool:
        now = _now_ts()
        score = await self.redis.zscore(self._k_blocked_z(), ip)
        if score is None:
            return False

        unblock_ts = float(score)
        if now >= unblock_ts:
            await self.redis.zrem(self._k_blocked_z(), ip)
            await self.redis.delete(self._k_block_meta(ip))
            return False
        return True

    async def block_ip(self, ip: str, hours: int, reason: str, source: str) -> Dict[str, Any]:
        now = _now_ts()
        ttl_seconds = max(1, int(hours * 3600))
        unblock_ts = now + ttl_seconds
        category = _reason_category(reason)

        ip_block_count = await self.redis.incr(self._k_ip_block_count(ip))
        await self.redis.expire(self._k_ip_block_count(ip), 90 * 24 * 3600)

        event = {
            "timestamp": _utc_now().isoformat(),
            "ip": ip,
            "reason": reason,
            "reason_category": category,
            "source": source,
            "blocked_until": _iso_from_ts(unblock_ts),
            "block_count_for_ip": _safe_int(ip_block_count),
        }

        await self.redis.set(self._k_block_meta(ip), json.dumps(event, ensure_ascii=True), ex=ttl_seconds)
        await self.redis.zadd(self._k_blocked_z(), {ip: unblock_ts})
        await self.redis.incr(self._k_total_blocks())
        await self.redis.hincrby(self._k_reason_blocks(), category, 1)
        await self.redis.lpush(self._k_recent_events(), json.dumps(event, ensure_ascii=True))
        await self.redis.ltrim(self._k_recent_events(), 0, max(0, SecurityConfig.EVENTS_RETENTION - 1))
        return event

    async def unblock_ip(self, ip: str) -> bool:
        removed = await self.redis.zrem(self._k_blocked_z(), ip)
        await self.redis.delete(self._k_block_meta(ip))
        return _safe_int(removed) > 0

    async def record_404(self, ip: str) -> None:
        now = _now_ts()
        member = f"{int(now * 1_000_000)}:{random.randint(1000, 9999)}"
        key = self._k_404(ip)
        await self.redis.zadd(key, {member: now})
        await self.redis.expire(key, 3600 + 120)

    async def get_404_count(self, ip: str, window_minutes: int) -> int:
        key = self._k_404(ip)
        cutoff = _now_ts() - window_minutes * 60
        await self.redis.zremrangebyscore(key, 0, cutoff)
        return _safe_int(await self.redis.zcard(key))

    async def get_ip_block_count(self, ip: str) -> int:
        return _safe_int(await self.redis.get(self._k_ip_block_count(ip)))

    async def get_stats(self) -> Dict[str, Any]:
        now = _now_ts()
        blocked_z = self._k_blocked_z()

        # Cleanup expired entries from sorted set (cheap maintenance)
        await self.redis.zremrangebyscore(blocked_z, 0, now)

        blocked_ips_count = _safe_int(await self.redis.zcard(blocked_z))
        total_blocks = _safe_int(await self.redis.get(self._k_total_blocks()))
        reason_counts_raw = await self.redis.hgetall(self._k_reason_blocks())
        reason_counts = {
            str(self._decode(k)): _safe_int(self._decode(v))
            for k, v in (reason_counts_raw or {}).items()
        }
        cloudflare_escalations_total = _safe_int(await self.redis.get(self._k_cf_total()))

        blocked_ips: Dict[str, str] = {}
        entries = await self.redis.zrange(blocked_z, 0, 199, withscores=True)
        for entry in entries or []:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                ip = str(self._decode(entry[0]))
                unblock_ts = float(entry[1])
                blocked_ips[ip] = _iso_from_ts(unblock_ts)

        # Estimate number of tracked 404 IPs by key count
        tracked_ips_count = 0
        pattern = self._k("404:*")
        scan_iter = getattr(self.redis, "scan_iter", None)
        if callable(scan_iter):
            async for _ in scan_iter(match=pattern):
                tracked_ips_count += 1
        else:
            keys = await self.redis.keys(pattern)
            tracked_ips_count = len(keys or [])

        return {
            "backend": self.name,
            "blocked_ips_count": blocked_ips_count,
            "tracked_ips_count": tracked_ips_count,
            "blocked_ips": blocked_ips,
            "block_events_total": total_blocks,
            "block_events_by_reason": reason_counts,
            "cloudflare_escalations_total": cloudflare_escalations_total,
            "recent_events_count": _safe_int(await self.redis.llen(self._k_recent_events())),
        }

    async def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        rows = await self.redis.lrange(self._k_recent_events(), 0, max(0, limit - 1))
        events: List[Dict[str, Any]] = []
        for row in rows or []:
            try:
                decoded = self._decode(row)
                event = json.loads(decoded)
                if isinstance(event, dict):
                    events.append(event)
            except Exception:
                continue
        return events

    async def is_cloudflare_escalated(self, ip: str) -> bool:
        exists = await self.redis.exists(self._k_cf_escalated(ip))
        return _safe_int(exists) > 0

    async def mark_cloudflare_escalated(self, ip: str, details: Dict[str, Any]) -> None:
        key = self._k_cf_escalated(ip)
        payload = json.dumps(details, ensure_ascii=True)

        # Set once to avoid inflating total counter on retries
        created = await self.redis.set(key, payload, ex=180 * 24 * 3600, nx=True)
        if created:
            await self.redis.incr(self._k_cf_total())

    async def get_blocked_ips(self, limit: int = 200) -> List[Dict[str, Any]]:
        now = _now_ts()
        blocked_z = self._k_blocked_z()

        await self.redis.zremrangebyscore(blocked_z, 0, now)
        if limit <= 0:
            return []

        entries = await self.redis.zrevrange(blocked_z, 0, max(0, limit - 1), withscores=True)
        blocked: List[Dict[str, Any]] = []
        for entry in entries or []:
            if not (isinstance(entry, (tuple, list)) and len(entry) == 2):
                continue

            ip = str(self._decode(entry[0]))
            unblock_ts = float(entry[1])
            raw_meta = await self.redis.get(self._k_block_meta(ip))
            meta: Dict[str, Any] = {}
            if raw_meta:
                try:
                    parsed = json.loads(str(self._decode(raw_meta)))
                    if isinstance(parsed, dict):
                        meta = parsed
                except Exception:
                    meta = {}

            cloudflare = await self.get_cloudflare_escalation(ip)
            meta.update({
                "ip": ip,
                "blocked_until": _iso_from_ts(unblock_ts),
                "is_active": _now_ts() < unblock_ts,
                "cloudflare": cloudflare,
                "cloudflare_synced": bool(cloudflare),
            })
            blocked.append(meta)

        return blocked

    async def get_cloudflare_escalation(self, ip: str) -> Optional[Dict[str, Any]]:
        raw_value = await self.redis.get(self._k_cf_escalated(ip))
        if not raw_value:
            return None
        try:
            parsed = json.loads(str(self._decode(raw_value)))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    async def clear_cloudflare_escalation(self, ip: str) -> bool:
        removed = await self.redis.delete(self._k_cf_escalated(ip))
        return _safe_int(removed) > 0

    async def get_all_cloudflare_escalated_ips(self) -> List[str]:
        ips: List[str] = []
        pattern = self._k("cloudflare:escalated:*")
        prefix = self._k("cloudflare:escalated:")
        scan_iter = getattr(self.redis, "scan_iter", None)
        if callable(scan_iter):
            async for key in scan_iter(match=pattern):
                raw_key = str(self._decode(key))
                if raw_key.startswith(prefix):
                    ips.append(raw_key[len(prefix):])
        else:
            keys = await self.redis.keys(pattern)
            for key in keys or []:
                raw_key = str(self._decode(key))
                if raw_key.startswith(prefix):
                    ips.append(raw_key[len(prefix):])
        return ips

    async def extend_block(self, ip: str, hours: int) -> bool:
        score = await self.redis.zscore(self._k_blocked_z(), ip)
        if score is None:
            return False
        ttl_seconds = max(1, int(hours * 3600))
        new_ts = _now_ts() + ttl_seconds
        await self.redis.zadd(self._k_blocked_z(), {ip: new_ts})
        raw_meta = await self.redis.get(self._k_block_meta(ip))
        if raw_meta:
            try:
                meta = json.loads(str(self._decode(raw_meta)))
                meta["blocked_until"] = _iso_from_ts(new_ts)
                await self.redis.set(
                    self._k_block_meta(ip),
                    json.dumps(meta, ensure_ascii=True),
                    ex=ttl_seconds,
                )
            except Exception:
                pass
        return True


# =============================================================================
# Optional Cloudflare escalation
# =============================================================================

class CloudflareEscalator:
    """Optional Cloudflare escalation for repeated offenders."""

    def __init__(self):
        self.enabled = SecurityConfig.CF_ESCALATION_ENABLED
        self.threshold = max(1, SecurityConfig.CF_ESCALATION_THRESHOLD)
        self.timeout = SecurityConfig.CF_ESCALATION_TIMEOUT_SECONDS
        self.notes_prefix = SecurityConfig.CF_ESCALATION_NOTES_PREFIX
        self._account_id = SecurityConfig.CF_ACCOUNT_ID
        self._lock = asyncio.Lock()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "threshold": self.threshold,
            "account_id_configured": bool(self._account_id),
            "credentials_configured": bool(SecurityConfig.CF_EMAIL and SecurityConfig.CF_API_KEY),
            "zone_id_configured": bool(SecurityConfig.CF_ZONE_ID),
        }

    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        try:
            parsed = ipaddress.ip_address(ip)
            return not (
                parsed.is_private
                or parsed.is_loopback
                or parsed.is_link_local
                or parsed.is_multicast
                or parsed.is_reserved
                or parsed.is_unspecified
            )
        except Exception:
            return False

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Auth-Email": SecurityConfig.CF_EMAIL,
            "X-Auth-Key": SecurityConfig.CF_API_KEY,
            "Content-Type": "application/json",
        }

    async def _resolve_account_id(self, client: httpx.AsyncClient) -> str:
        if self._account_id:
            return self._account_id

        if not SecurityConfig.CF_ZONE_ID:
            return ""

        zone_url = f"https://api.cloudflare.com/client/v4/zones/{SecurityConfig.CF_ZONE_ID}"
        response = await client.get(zone_url, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            return ""

        account_id = ((payload.get("result") or {}).get("account") or {}).get("id")
        if account_id:
            self._account_id = str(account_id)
        return self._account_id

    async def maybe_escalate(
        self,
        tracker: "SecurityTracker",
        ip: str,
        block_count_for_ip: int,
        reason: str,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"attempted": False, "status": "disabled"}

        if block_count_for_ip < self.threshold:
            return {
                "attempted": False,
                "status": "below_threshold",
                "block_count_for_ip": block_count_for_ip,
                "threshold": self.threshold,
            }

        if not self._is_public_ip(ip):
            return {"attempted": False, "status": "non_public_ip"}

        if not (SecurityConfig.CF_EMAIL and SecurityConfig.CF_API_KEY):
            return {"attempted": False, "status": "missing_credentials"}

        if await tracker.is_cloudflare_escalated(ip):
            return {"attempted": False, "status": "already_escalated"}

        async with self._lock:
            # Re-check inside lock
            if await tracker.is_cloudflare_escalated(ip):
                return {"attempted": False, "status": "already_escalated"}

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    account_id = await self._resolve_account_id(client)
                    if not account_id:
                        return {"attempted": False, "status": "missing_account_id"}

                    # Check existing rule first to avoid duplicates
                    query_url = (
                        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/"
                        f"firewall/access_rules/rules"
                    )
                    query_params = {
                        "mode": "block",
                        "configuration.target": "ip",
                        "configuration.value": ip,
                    }
                    query_response = await client.get(query_url, headers=self._headers(), params=query_params)
                    query_response.raise_for_status()
                    query_payload = query_response.json()

                    existing_rules = (query_payload or {}).get("result") or []
                    if existing_rules:
                        existing_rule = existing_rules[0]
                        details = {
                            "rule_id": existing_rule.get("id"),
                            "mode": existing_rule.get("mode"),
                            "value": ((existing_rule.get("configuration") or {}).get("value")),
                            "source": "existing",
                            "timestamp": _utc_now().isoformat(),
                        }
                        await tracker.mark_cloudflare_escalated(ip, details)
                        return {
                            "attempted": True,
                            "status": "existing",
                            **details,
                        }

                    # Create new block rule
                    notes = f"{self.notes_prefix}: ip={ip}; count={block_count_for_ip}; reason={_reason_category(reason)}"
                    create_payload = {
                        "mode": "block",
                        "configuration": {
                            "target": "ip",
                            "value": ip,
                        },
                        "notes": notes[:250],
                    }
                    create_response = await client.post(query_url, headers=self._headers(), json=create_payload)
                    create_response.raise_for_status()
                    created = create_response.json()

                    if not created.get("success"):
                        return {
                            "attempted": True,
                            "status": "api_error",
                            "errors": created.get("errors", []),
                        }

                    result = created.get("result") or {}
                    details = {
                        "rule_id": result.get("id"),
                        "mode": result.get("mode"),
                        "value": ((result.get("configuration") or {}).get("value")),
                        "notes": result.get("notes"),
                        "source": "created",
                        "timestamp": result.get("created_on") or _utc_now().isoformat(),
                    }
                    await tracker.mark_cloudflare_escalated(ip, details)
                    return {
                        "attempted": True,
                        "status": "created",
                        **details,
                    }
            except Exception as exc:
                logger.error(f"SECURITY: Cloudflare escalation failed for {ip}: {exc}")
                return {
                    "attempted": True,
                    "status": "error",
                    "error": str(exc),
                }

    async def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        """Delete a Cloudflare Access Rule by ID."""
        if not self.enabled:
            return {"deleted": False, "status": "disabled"}

        if not (SecurityConfig.CF_EMAIL and SecurityConfig.CF_API_KEY):
            return {"deleted": False, "status": "missing_credentials"}

        if not rule_id:
            return {"deleted": False, "status": "missing_rule_id"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                account_id = await self._resolve_account_id(client)
                if not account_id:
                    return {"deleted": False, "status": "missing_account_id"}

                url = (
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/"
                    f"firewall/access_rules/rules/{rule_id}"
                )
                response = await client.delete(url, headers=self._headers())
                response.raise_for_status()
                payload = response.json()

                if payload.get("success"):
                    logger.info("SECURITY: Cloudflare rule %s deleted", rule_id)
                    return {"deleted": True, "rule_id": rule_id}

                return {
                    "deleted": False,
                    "status": "api_error",
                    "errors": payload.get("errors", []),
                }
        except Exception as exc:
            logger.error("SECURITY: Cloudflare rule deletion failed for %s: %s", rule_id, exc)
            return {"deleted": False, "status": "error", "error": str(exc)}


# =============================================================================
# Tracker Orchestrator
# =============================================================================

class SecurityTracker:
    """
    Tracker facade with backend selection + fallback.

    Backend mode:
    - auto: Redis preferred (if available), fallback to in-memory on failure
    - off: in-memory only
    """

    def __init__(
        self,
        mode: Optional[str] = None,
        redis_backend: Optional[BaseSecurityBackend] = None,
        memory_backend: Optional[InMemorySecurityBackend] = None,
        escalator: Optional[CloudflareEscalator] = None,
    ):
        self.mode = (mode or SecurityConfig.REDIS_MODE or "auto").lower()
        if self.mode not in {"auto", "off"}:
            self.mode = "auto"

        self.memory_backend = memory_backend or InMemorySecurityBackend()
        if redis_backend is not None:
            self.redis_backend = redis_backend
        elif self.mode == "off":
            self.redis_backend = None
        else:
            self.redis_backend = self._build_default_redis_backend()

        self._active_backend = "memory"
        self._last_backend_error: str = ""
        self._redis_cooldown_until: float = 0.0
        self._last_redis_healthcheck: float = 0.0
        self._backend_lock = asyncio.Lock()
        self._cf_cleanup_lock = asyncio.Lock()
        self._last_cf_cleanup_ts: float = 0.0

        self.escalator = escalator or CloudflareEscalator()

    @staticmethod
    def _build_default_redis_backend() -> Optional[BaseSecurityBackend]:
        try:
            from rediscfg import redis_client  # Lazy import to avoid hard dependency in tests
            return RedisSecurityBackend(redis_client, SecurityConfig.REDIS_KEY_PREFIX)
        except Exception as exc:
            logger.warning(f"SECURITY: Redis backend unavailable, using in-memory backend: {exc}")
            return None

    async def _select_backend(self) -> BaseSecurityBackend:
        if self.mode == "off" or not self.redis_backend:
            self._active_backend = "memory"
            return self.memory_backend

        now = _now_ts()
        if (
            self._active_backend == "redis"
            and (now - self._last_redis_healthcheck) < SecurityConfig.REDIS_HEALTHCHECK_INTERVAL_SECONDS
        ):
            return self.redis_backend

        if now < self._redis_cooldown_until:
            self._active_backend = "memory"
            return self.memory_backend

        async with self._backend_lock:
            now = _now_ts()
            if now < self._redis_cooldown_until:
                self._active_backend = "memory"
                return self.memory_backend

            try:
                await self.redis_backend.ping()
                self._active_backend = "redis"
                self._last_redis_healthcheck = now
                return self.redis_backend
            except Exception as exc:
                self._last_backend_error = str(exc)
                self._redis_cooldown_until = now + SecurityConfig.REDIS_RECOVERY_COOLDOWN_SECONDS
                self._active_backend = "memory"
                self._last_redis_healthcheck = now
                logger.warning(
                    "SECURITY: Redis backend unavailable, falling back to in-memory for %ss. Error: %s",
                    SecurityConfig.REDIS_RECOVERY_COOLDOWN_SECONDS,
                    exc,
                )
                return self.memory_backend

    async def _run(self, method: str, *args, **kwargs):
        backend = await self._select_backend()
        return await getattr(backend, method)(*args, **kwargs)

    async def is_blocked(self, ip: str) -> bool:
        return await self._run("is_blocked", ip)

    async def block_ip(self, ip: str, hours: int = 24, reason: str = "", source: str = "manual") -> Dict[str, Any]:
        event = await self._run("block_ip", ip, hours, reason, source)
        logger.warning(f"SECURITY: Blocked IP {ip} for {hours}h. Reason: {reason}")

        # Optional Cloudflare escalation for repeated offenders
        try:
            block_count_for_ip = _safe_int(event.get("block_count_for_ip"), 0)
            escalation = await self.escalator.maybe_escalate(self, ip, block_count_for_ip, reason)
            if escalation.get("status") in {"created", "existing"}:
                event["cloudflare"] = escalation
                cf_hours = SecurityConfig.CF_ESCALATION_BLOCK_HOURS
                if cf_hours > hours:
                    await self._run("extend_block", ip, cf_hours)
                    event["blocked_until"] = _iso_from_ts(_now_ts() + cf_hours * 3600)
                    logger.info("SECURITY: Extended block for %s to %dh (CF escalation)", ip, cf_hours)
        except Exception as exc:
            logger.error(f"SECURITY: escalation check failed for {ip}: {exc}")

        # Sync to nginx blocklist
        nginx_blocklist_manager.add_ip(ip)

        # Opportunistic cleanup is traffic-driven: when new blocks happen, run a
        # bounded stale cleanup at most once per interval. No cron/worker required.
        try:
            await self._maybe_cleanup_stale_cloudflare_blocks()
        except Exception as exc:
            logger.error("SECURITY: opportunistic CF cleanup trigger failed: %s", exc)

        return event

    async def _maybe_cleanup_stale_cloudflare_blocks(self) -> None:
        interval = SecurityConfig.CF_CLEANUP_INTERVAL_SECONDS
        if interval <= 0:
            return

        now = _now_ts()
        if (now - self._last_cf_cleanup_ts) < interval:
            return
        if self._cf_cleanup_lock.locked():
            return

        async with self._cf_cleanup_lock:
            now = _now_ts()
            if (now - self._last_cf_cleanup_ts) < interval:
                return
            self._last_cf_cleanup_ts = now

            summary = await self._cleanup_stale_cloudflare_blocks()
            if summary["processed"] > 0:
                logger.info(
                    "SECURITY: Opportunistic CF cleanup processed=%s deleted=%s cleared=%s active=%s errors=%s elapsed_ms=%s",
                    summary["processed"],
                    summary["deleted"],
                    summary["cleared"],
                    summary["still_active"],
                    summary["errors"],
                    summary["elapsed_ms"],
                )

    async def _cleanup_stale_cloudflare_blocks(self) -> Dict[str, int]:
        start_ts = _now_ts()
        max_ips = max(1, SecurityConfig.CF_CLEANUP_MAX_IPS_PER_RUN)
        time_budget_seconds = max(0.1, SecurityConfig.CF_CLEANUP_TIME_BUDGET_SECONDS)

        processed = 0
        deleted = 0
        cleared = 0
        still_active = 0
        errors = 0

        cf_ips = await self._run("get_all_cloudflare_escalated_ips")
        for cf_ip in cf_ips:
            if processed >= max_ips:
                break
            if (_now_ts() - start_ts) >= time_budget_seconds:
                break

            processed += 1
            try:
                if await self._run("is_blocked", cf_ip):
                    still_active += 1
                    continue

                escalation = await self._run("get_cloudflare_escalation", cf_ip)
                rule_id = (escalation or {}).get("rule_id")
                if rule_id:
                    remaining = max(0.1, time_budget_seconds - (_now_ts() - start_ts))
                    escalator_timeout = getattr(self.escalator, "timeout", SecurityConfig.CF_ESCALATION_TIMEOUT_SECONDS)
                    delete_timeout = min(max(0.1, float(escalator_timeout)), remaining)
                    try:
                        cf_result = await asyncio.wait_for(
                            self.escalator.delete_rule(rule_id),
                            timeout=delete_timeout,
                        )
                    except asyncio.TimeoutError:
                        errors += 1
                        logger.warning(
                            "SECURITY: Opportunistic CF cleanup timeout for %s (rule_id=%s)",
                            cf_ip,
                            rule_id,
                        )
                        continue

                    if not cf_result.get("deleted"):
                        errors += 1
                        logger.warning(
                            "SECURITY: Opportunistic CF cleanup could not delete rule for %s (status=%s)",
                            cf_ip,
                            cf_result.get("status"),
                        )
                        continue
                    deleted += 1

                if await self._run("clear_cloudflare_escalation", cf_ip):
                    cleared += 1
            except Exception as exc:
                errors += 1
                logger.error("SECURITY: Opportunistic CF cleanup failed for %s: %s", cf_ip, exc)

        elapsed_ms = int((_now_ts() - start_ts) * 1000)
        return {
            "processed": processed,
            "deleted": deleted,
            "cleared": cleared,
            "still_active": still_active,
            "errors": errors,
            "elapsed_ms": elapsed_ms,
        }

    async def unblock_ip(self, ip: str) -> Dict[str, Any]:
        unblocked = await self._run("unblock_ip", ip)
        nginx_blocklist_manager.remove_ip(ip)
        escalation = await self.get_cloudflare_escalation(ip)
        result: Dict[str, Any] = {
            "ip": ip,
            "unblocked": unblocked,
            "cloudflare_deleted": False,
            "cloudflare_rule_id": None,
        }

        if escalation and escalation.get("rule_id"):
            rule_id = escalation["rule_id"]
            cf_result = await self.escalator.delete_rule(rule_id)
            result["cloudflare_deleted"] = cf_result.get("deleted", False)
            result["cloudflare_rule_id"] = rule_id

            # Keep metadata if Cloudflare deletion failed, so it can be retried.
            if cf_result.get("deleted"):
                await self._run("clear_cloudflare_escalation", ip)
        elif escalation:
            await self._run("clear_cloudflare_escalation", ip)

        return result

    async def record_404(self, ip: str) -> None:
        await self._run("record_404", ip)

    async def get_404_count(self, ip: str, window_minutes: int) -> int:
        return await self._run("get_404_count", ip, window_minutes)

    async def get_ip_block_count(self, ip: str) -> int:
        return await self._run("get_ip_block_count", ip)

    async def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._run("get_recent_events", limit)

    async def get_blocked_ips(self, limit: int = 200) -> List[Dict[str, Any]]:
        # Keep this endpoint read-only/fast: no external network calls here.
        return await self._run("get_blocked_ips", limit)

    async def is_cloudflare_escalated(self, ip: str) -> bool:
        return await self._run("is_cloudflare_escalated", ip)

    async def mark_cloudflare_escalated(self, ip: str, details: Dict[str, Any]) -> None:
        await self._run("mark_cloudflare_escalated", ip, details)

    async def get_cloudflare_escalation(self, ip: str) -> Optional[Dict[str, Any]]:
        return await self._run("get_cloudflare_escalation", ip)

    async def clear_cloudflare_escalation(self, ip: str) -> bool:
        return await self._run("clear_cloudflare_escalation", ip)

    async def get_all_cloudflare_escalated_ips(self) -> List[str]:
        return await self._run("get_all_cloudflare_escalated_ips")

    async def extend_block(self, ip: str, hours: int) -> bool:
        return await self._run("extend_block", ip, hours)

    async def retry_cloudflare_sync(self, ip: str, reason: str = "Manual sync retry") -> Dict[str, Any]:
        block_count_for_ip = await self.get_ip_block_count(ip)
        block_count_for_ip = max(block_count_for_ip, self.escalator.threshold)
        result = await self.escalator.maybe_escalate(self, ip, block_count_for_ip, reason)
        if result.get("status") == "already_escalated":
            details = await self.get_cloudflare_escalation(ip)
            if details:
                result = {**result, **details}
        return result

    async def get_stats(self) -> Dict[str, Any]:
        stats = await self._run("get_stats")
        stats.update({
            "mode": self.mode,
            "active_backend": self._active_backend,
            "redis_available": self.redis_backend is not None,
            "last_backend_error": self._last_backend_error,
            "cloudflare_escalation": self.escalator.status(),
        })
        return stats


# Singleton tracker instance
_tracker = SecurityTracker()


# =============================================================================
# Helper Functions
# =============================================================================


def get_client_ip(request: Request) -> str:
    """
    Extract real client IP, considering reverse proxies.

    Priority:
    1. CF-Connecting-IP (Cloudflare)
    2. X-Forwarded-For (nginx/proxies - first IP in chain)
    3. X-Real-IP (nginx)
    4. Direct connection
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def is_known_bot(request: Request) -> bool:
    """Check if request is from a known legitimate bot."""
    user_agent = request.headers.get("User-Agent", "")
    return any(bot.lower() in user_agent.lower() for bot in SecurityConfig.KNOWN_BOTS)


def is_whitelisted_path(path: str) -> bool:
    """Check if path is whitelisted (shouldn't count as 404)."""
    path_lower = path.lower()

    if path_lower in SecurityConfig.WHITELIST_PATHS:
        return True

    for prefix in SecurityConfig.WHITELIST_PREFIXES:
        if path_lower.startswith(prefix):
            return True

    return False


def matches_instant_block_pattern(path: str) -> Optional[str]:
    """
    Check if path matches any instant-block pattern.
    Returns the matched pattern string or None.
    """
    for i, pattern in enumerate(SecurityConfig.COMPILED_PATTERNS):
        if pattern.search(path):
            return SecurityConfig.INSTANT_BLOCK_PATTERNS[i]
    return None


def is_landing_page_route(path: str) -> bool:
    """
    Check if path is a landing page route.

    Landing pages are user-generated content where the slug comes from
    the prompt name. We skip instant-block pattern matching for these
    to avoid false positives (e.g., a prompt named "docker-tutorial"
    would match the /docker pattern).

    404 accumulation still applies to catch actual scanners.
    """
    for pattern in SecurityConfig.LANDING_PAGE_PATTERNS:
        if pattern.match(path):
            return True
    return False


def _has_valid_session(request: Request) -> bool:
    """Lightweight JWT cookie check. Authenticated users are exempt from reputation scoring."""
    token = request.cookies.get("access_token")
    if not token:
        return False
    try:
        from common import decode_jwt_cached, verify_token_expiration, SECRET_KEY
        payload = decode_jwt_cached(token, SECRET_KEY)
        return verify_token_expiration(payload)
    except Exception:
        return False


# =============================================================================
# Main Middleware
# =============================================================================

class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Security middleware for scanner/bot protection.

    Must be added EARLY in the middleware chain to intercept
    malicious requests before they consume resources.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        ip = get_client_ip(request)
        path = request.url.path

        # 0. Admin IP bypass - never block admin IPs
        if ip in SecurityConfig.get_admin_ips():
            return await call_next(request)

        # 0.5. Check reputation ban (persistent layer)
        reputation_ban = reputation_manager.check_reputation_ban(ip)
        if reputation_ban is not None:
            # Sync reputation ban into real-time tracker if not already blocked
            if not await _tracker.is_blocked(ip):
                remaining_hours = max(1, int((reputation_ban - time.time()) / 3600))
                await _tracker.block_ip(ip, hours=remaining_hours, reason="Reputation ban (synced)", source="reputation")

        # 1. Check if IP is already blocked
        if await _tracker.is_blocked(ip):
            logger.debug(f"SECURITY: Blocked request from banned IP {ip}: {path}")
            if not _has_valid_session(request):
                reputation_manager.record_request(ip, 403, path, is_blocked_ip=True, ban_already_handled=True)
            await nginx_blocklist_manager.maybe_reload()
            return Response(
                content=SecurityConfig.BLOCK_MESSAGE,
                status_code=SecurityConfig.BLOCK_STATUS_CODE,
            )

        # 2. Check for instant-block patterns
        # Skip pattern matching for landing page routes (user-generated slugs)
        # to avoid false positives like /p/xxx/docker-tutorial/
        if not is_landing_page_route(path):
            matched_pattern = matches_instant_block_pattern(path)
            if matched_pattern:
                await _tracker.block_ip(
                    ip,
                    hours=SecurityConfig.BLOCK_DURATION_HOURS,
                    reason=f"Instant block pattern: {matched_pattern} (path: {path})",
                    source="instant_pattern",
                )
                reputation_manager.record_request(
                    ip, 403, path,
                    is_pattern_hit=True,
                    ban_already_handled=True,
                    external_ban_until=time.time() + SecurityConfig.BLOCK_DURATION_HOURS * 3600,
                )
                await nginx_blocklist_manager.maybe_reload()
                return Response(
                    content=SecurityConfig.BLOCK_MESSAGE,
                    status_code=SecurityConfig.BLOCK_STATUS_CODE,
                )

        # 3. Process the request normally
        response = await call_next(request)

        # 4. After response: check if 404 and track
        if response.status_code == 404 and not is_whitelisted_path(path):
            if is_known_bot(request):
                max_404s, window_min = SecurityConfig.BOT_THRESHOLD
            else:
                max_404s, window_min = SecurityConfig.NORMAL_THRESHOLD

            await _tracker.record_404(ip)
            count = await _tracker.get_404_count(ip, window_min)
            logger.debug(f"SECURITY: 404 from {ip}: {path} (count: {count}/{max_404s})")

            if count >= max_404s:
                await _tracker.block_ip(
                    ip,
                    hours=SecurityConfig.BLOCK_DURATION_HOURS,
                    reason=f"Too many 404s: {count} in {window_min} min (last: {path})",
                    source="404_threshold",
                )
                # Note: This response already went out as 404
                # The NEXT request from this IP will be blocked

        # 5. Record in reputation system (non-authenticated only)
        if not _has_valid_session(request):
            is_wl = is_whitelisted_path(path)
            ban_info = reputation_manager.record_request(ip, response.status_code, path, is_whitelisted_path=is_wl)
            if ban_info:
                # Reputation triggered a ban - sync to real-time tracker
                await _tracker.block_ip(
                    ip,
                    hours=ban_info["ban_hours"],
                    reason=f"Reputation: {ban_info['reason']}",
                    source="reputation",
                )
                if ban_info.get("cf_escalate"):
                    await _tracker.retry_cloudflare_sync(ip, reason=f"Reputation CF escalation: {ban_info.get('reason', '')}")

        # 6. Piggyback nginx blocklist reload (debounced, only if dirty)
        await nginx_blocklist_manager.maybe_reload()

        return response


# =============================================================================
# API for external access (admin endpoints, etc.)
# =============================================================================

async def get_security_stats_async() -> Dict[str, Any]:
    """Get current security statistics."""
    return await _tracker.get_stats()


async def get_security_events_async(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent security block events."""
    return await _tracker.get_recent_events(limit=limit)


async def get_security_blocked_ips_async(limit: int = 200) -> List[Dict[str, Any]]:
    """Get currently blocked IPs with metadata and Cloudflare sync status."""
    return await _tracker.get_blocked_ips(limit=limit)


async def manually_block_ip_async(ip: str, hours: int = 24, reason: str = "Manual block") -> Dict[str, Any]:
    """Manually block an IP (for admin use)."""
    normalized_ip = _normalize_ip(ip)
    return await _tracker.block_ip(ip=normalized_ip, hours=hours, reason=reason, source="manual")


async def manually_unblock_ip_async(ip: str) -> Dict[str, Any]:
    """Manually unblock an IP. Returns dict with unblock details and CF deletion status."""
    normalized_ip = _normalize_ip(ip)
    result = await _tracker.unblock_ip(normalized_ip)
    if result.get("unblocked"):
        cf_info = ""
        if result.get("cloudflare_deleted"):
            cf_info = f" (CF rule {result.get('cloudflare_rule_id')} also deleted)"
        logger.info(f"SECURITY: Manually unblocked IP {normalized_ip}{cf_info}")
    return result


async def is_ip_blocked_async(ip: str) -> bool:
    """Check if an IP is currently blocked."""
    normalized_ip = _normalize_ip(ip)
    return await _tracker.is_blocked(normalized_ip)


async def retry_cloudflare_sync_async(ip: str, reason: str = "Manual sync retry") -> Dict[str, Any]:
    """Retry Cloudflare sync for a blocked IP."""
    normalized_ip = _normalize_ip(ip)
    return await _tracker.retry_cloudflare_sync(ip=normalized_ip, reason=reason)


# Backward-compatible wrappers for legacy sync callers (avoid using in async contexts)
def get_security_stats() -> Dict[str, Any]:
    try:
        asyncio.get_running_loop()
        return {
            "error": "get_security_stats() called in async context. Use get_security_stats_async()."
        }
    except RuntimeError:
        return asyncio.run(get_security_stats_async())


def manually_block_ip(ip: str, hours: int = 24, reason: str = "Manual block"):
    try:
        asyncio.get_running_loop()
        logger.warning("manually_block_ip() called in async context. Use manually_block_ip_async().")
        return None
    except RuntimeError:
        return asyncio.run(manually_block_ip_async(ip=ip, hours=hours, reason=reason))


def manually_unblock_ip(ip: str) -> Dict[str, Any]:
    try:
        asyncio.get_running_loop()
        logger.warning("manually_unblock_ip() called in async context. Use manually_unblock_ip_async().")
        return {"ip": ip, "unblocked": False, "cloudflare_deleted": False, "cloudflare_rule_id": None}
    except RuntimeError:
        return asyncio.run(manually_unblock_ip_async(ip))


def is_ip_blocked(ip: str) -> bool:
    try:
        asyncio.get_running_loop()
        logger.warning("is_ip_blocked() called in async context. Use is_ip_blocked_async().")
        return False
    except RuntimeError:
        return asyncio.run(is_ip_blocked_async(ip))
