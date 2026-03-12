"""
NginxBlocklistManager - Dynamic IP blocklist for nginx's geo directive.

Maintains a blocklist file that nginx includes via `geo $blocked_ip`.
When IPs are blocked/unblocked by SecurityTracker, changes are synced
to the file and nginx is reloaded with debounce to avoid excessive reloads.
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env vars with sensible defaults)
# ---------------------------------------------------------------------------
_NGINX_BASE = (os.getenv("NGINX_BASE_PATH", "") or "").strip()
NGINX_EXE = (os.getenv("NGINX_BLOCKLIST_EXE", "") or "").strip()
NGINX_PREFIX = (os.getenv("NGINX_BLOCKLIST_PREFIX", _NGINX_BASE) or "").strip()
NGINX_CONF = (os.getenv("NGINX_BLOCKLIST_CONF", "") or "").strip()

if not NGINX_EXE:
    NGINX_EXE = os.path.join(_NGINX_BASE, "nginx.exe") if _NGINX_BASE else "nginx"
if not NGINX_CONF and _NGINX_BASE:
    NGINX_CONF = os.path.join(_NGINX_BASE, "conf", "nginx.conf")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKLIST_PATH = os.getenv("NGINX_BLOCKLIST_PATH", os.path.join(_PROJECT_ROOT, "data", "nginx_blocklist.conf"))
DEBOUNCE_SECONDS = int(os.getenv("NGINX_BLOCKLIST_DEBOUNCE", "180"))
ENABLED = os.getenv("NGINX_BLOCKLIST_ENABLED", "true").lower() in ("true", "1", "yes")


class NginxBlocklistManager:
    """Manages a dynamic nginx IP blocklist with debounced reload."""

    def __init__(self):
        self._blocked_ips: set = set()
        self._dirty: bool = False
        self._last_reload_ts: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_ip(self, ip: str) -> None:
        """Add IP to blocklist. Marks dirty. Skips admin IPs."""
        if not ENABLED:
            return
        from middleware.security import SecurityConfig
        if ip in SecurityConfig.get_admin_ips():
            return
        if ip not in self._blocked_ips:
            self._blocked_ips.add(ip)
            self._dirty = True

    def remove_ip(self, ip: str) -> None:
        """Remove IP from blocklist. Marks dirty."""
        if not ENABLED:
            return
        if ip in self._blocked_ips:
            self._blocked_ips.discard(ip)
            self._dirty = True

    async def maybe_reload(self) -> None:
        """
        If dirty AND debounce period elapsed, write blocklist file and reload nginx.
        Called piggyback on request dispatch (same pattern as reputation flush).
        """
        if not ENABLED or not self._dirty:
            return

        now = time.time()
        if (now - self._last_reload_ts) < DEBOUNCE_SECONDS:
            return

        self._write_blocklist()

        ok = self._nginx_test()
        if not ok:
            logger.error("NGINX_BLOCKLIST: Config test failed, skipping reload")
            self._last_reload_ts = now
            return

        reloaded = self._nginx_reload()
        if reloaded:
            self._dirty = False
            logger.info("NGINX_BLOCKLIST: Reloaded with %d blocked IPs", len(self._blocked_ips))
        else:
            logger.warning("NGINX_BLOCKLIST: Reload failed; keeping pending blocklist changes for retry")
        self._last_reload_ts = now

    async def initialize(self) -> None:
        """
        Called at app startup (after reputation_manager.initialize()).
        Ensures the blocklist file exists so nginx can start, and loads
        any previously written IPs back into memory.
        """
        if not ENABLED:
            logger.info("NGINX_BLOCKLIST: Disabled via config")
            return

        if not os.path.exists(BLOCKLIST_PATH):
            self._write_blocklist()
            logger.info("NGINX_BLOCKLIST: Created initial empty blocklist")
        else:
            self._load_existing()

        logger.info("NGINX_BLOCKLIST: Initialized with %d IPs", len(self._blocked_ips))

    async def shutdown(self) -> None:
        """Flush final changes if dirty."""
        if not ENABLED or not self._dirty:
            return

        self._write_blocklist()
        ok = self._nginx_test()
        reloaded = False
        if ok:
            reloaded = self._nginx_reload()
            if reloaded:
                logger.info("NGINX_BLOCKLIST: Shutdown flush completed (%d IPs)", len(self._blocked_ips))
            else:
                logger.error("NGINX_BLOCKLIST: Shutdown flush reload failed")
        else:
            logger.error("NGINX_BLOCKLIST: Shutdown flush - config test failed, skipped reload")
        if ok and reloaded:
            self._dirty = False

    def _build_nginx_cmd(self, *args: str) -> list[str]:
        """
        Build nginx command safely.
        Only include -p / -c when configured to avoid invalid empty values.
        """
        cmd = [NGINX_EXE, *args]
        if NGINX_PREFIX:
            cmd.extend(["-p", NGINX_PREFIX])
        if NGINX_CONF:
            cmd.extend(["-c", NGINX_CONF])
        return cmd

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_blocklist(self) -> None:
        """Write the blocklist file in nginx geo format."""
        lines = [
            "# Auto-generated by AURVEK NginxBlocklistManager",
            "# DO NOT EDIT MANUALLY - changes will be overwritten",
            f"# Last updated: {datetime.now(timezone.utc).isoformat()}",
        ]
        for ip in sorted(self._blocked_ips):
            lines.append(f"{ip} 1;")

        content = "\n".join(lines) + "\n"

        with open(BLOCKLIST_PATH, "w", encoding="utf-8") as f:
            f.write(content)

    def _load_existing(self) -> None:
        """Load IPs from existing blocklist file (geo format: 'IP 1;')."""
        try:
            with open(BLOCKLIST_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Expected format: "1.2.3.4 1;"
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "1;":
                        self._blocked_ips.add(parts[0])
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: Failed to load existing file: %s", exc)

    def _nginx_test(self) -> bool:
        """Run nginx -t to validate config. Returns True if valid."""
        try:
            cmd = self._build_nginx_cmd("-t")
            result = subprocess.run(
                cmd,
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("NGINX_BLOCKLIST: nginx -t failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("NGINX_BLOCKLIST: nginx -t timed out")
            return False
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: nginx -t error: %s", exc)
            return False

    def _nginx_reload(self) -> bool:
        """Send reload signal to nginx. Returns True if reload command succeeds."""
        try:
            cmd = self._build_nginx_cmd("-s", "reload")
            result = subprocess.run(
                cmd,
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("NGINX_BLOCKLIST: nginx reload failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("NGINX_BLOCKLIST: nginx reload timed out")
            return False
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: nginx reload error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------
nginx_blocklist_manager = NginxBlocklistManager()
