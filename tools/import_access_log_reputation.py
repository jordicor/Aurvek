"""
One-time import: Parse nginx access.log and seed IP Reputation database.

Reads the historical access.log, applies the same scoring logic as the
IP Reputation system, and inserts aggregated results into db/security.db.

Usage: python import_access_log_reputation.py [--dry-run]

This script is idempotent: it uses INSERT OR REPLACE so running it
multiple times won't create duplicates (but will overwrite).
"""

import ipaddress
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

# =========================================================================
# Configuration
# =========================================================================

_NGINX_BASE = os.getenv("NGINX_BASE_PATH", "")
ACCESS_LOG_PATH = os.getenv("ACCESS_LOG_PATH", os.path.join(_NGINX_BASE, "logs", "access.log") if _NGINX_BASE else "")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECURITY_DB_PATH = os.path.join(_PROJECT_ROOT, "db", "security.db")

DRY_RUN = "--dry-run" in sys.argv

# Cloudflare edge IP ranges (these are proxies, not real clients)
CF_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]

# Private/local ranges to skip
SKIP_RANGES = ["127.0.0.0/8", "10.0.0.0/8", "192.168.0.0/16", "169.254.0.0/16"]

# Combine all skip ranges
ALL_SKIP_NETS = [ipaddress.ip_network(r) for r in CF_RANGES + SKIP_RANGES]

# Scoring (same values as ip_reputation.py ReputationConfig)
SCORE_PATTERN_MATCH = 50.0
SCORE_404 = 3.0
SCORE_403 = 2.0
SCORE_200_REWARD = -0.5

# Instant block patterns (subset from security.py, same ones used in analysis)
INSTANT_BLOCK_PATTERNS = [
    r"^/wp-", r"^/wordpress", r"/xmlrpc\.php", r"/wp-login\.php", r"/wp-config\.php",
    r"^/phpmyadmin", r"^/pma", r"^/myadmin", r"^/mysql", r"^/phpMyAdmin",
    r"/\.git", r"/\.env", r"/\.aws", r"/\.ssh", r"/\.htaccess", r"/\.htpasswd",
    r"/\.DS_Store", r"/\.svn", r"/\.hg",
    r"/config\.(php|yml|yaml|json|ini|xml|bak)$",
    r"/settings\.(php|yml|yaml|json|ini|xml)$",
    r"/database\.(php|yml|yaml|json|ini|xml)$",
    r"/credentials", r"/secrets",
    r"/(shell|cmd|exec|eval|system|passthru)\.php",
    r"/c99\.php", r"/r57\.php", r"/alfa\.php", r"/wso\.php", r"/b374k",
    r"^/cgi-bin/", r"^/cgi/",
    r"^/admin\.php", r"^/administrator",
    r"^/admin/.*\.(php|asp|aspx|jsp)$",
    r"^/manager/html", r"^/manager/status", r"^/manager/text",
    r"\.(sql|bak|backup|old|orig|save|swp|tmp)$",
    r"\.(tar|tar\.gz|tgz|zip|rar|7z)$",
    r"/latest/meta-data", r"/169\.254\.169\.254",
    r"^/joomla", r"^/drupal", r"^/magento", r"^/typo3",
    r"^/jenkins", r"^/hudson", r"^/solr", r"^/actuator", r"^/console",
    r"/phpinfo\.php", r"/info\.php", r"/test\.php", r"/debug", r"/trace",
    r"^/k8s/", r"^/kubernetes/", r"^/docker", r"^/portainer",
    r"^/owa/", r"^/ecp/", r"/autodiscover", r"/aspnet_client", r"^/remote/login",
    r"\.(asp|aspx|jsp|jspx|do|action)$", r"\.(cgi|pl|cfm|cfc)$",
    r"^/telescope", r"^/horizon", r"^/_profiler", r"^/__debug__",
    r"/elmah\.axd", r"/trace\.axd", r"/web\.config",
    r"^/server-status", r"^/server-info",
    r"^/vendor/", r"^/node_modules/",
    r"/composer\.(json|lock)$", r"/package\.json$", r"/package-lock\.json$",
    r"/requirements\.txt$", r"/Pipfile", r"/Gemfile",
    r"/\.vscode/", r"/\.idea/", r"/\.project$", r"/\.settings/",
    r"^/jmx-console", r"^/web-console", r"^/invoker/",
    r"/jolokia", r"/hawtio", r"/wls-wsat", r"/ws_utc",
    r"^/axis2/", r"^/struts/",
    r"^/CFIDE", r"^/cfide", r"^/lucee", r"^/railo",
    r"^/nagios", r"^/zabbix", r"^/munin", r"^/cacti",
    r"^/grafana", r"^/kibana", r"^/prometheus",
    r"^/HNAP1/", r"^/boaform/", r"^/GponForm/", r"^/goform/",
    r"/setup\.cgi", r"/apply\.cgi",
    r"/Thumbs\.db", r"/desktop\.ini",
    r"^/prestashop", r"^/shopify", r"^/moodle", r"^/confluence", r"^/bitbucket",
    r"/login\.php", r"/index\.php",
    r"/crossdomain\.xml", r"/clientaccesspolicy\.xml", r"/nmaplowercheck",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INSTANT_BLOCK_PATTERNS]

WHITELIST_PATHS = {
    "/favicon.ico", "/favicon.png", "/robots.txt", "/sitemap.xml",
    "/sitemap_index.xml", "/manifest.json", "/site.webmanifest",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
    "/browserconfig.xml", "/humans.txt", "/ads.txt", "/security.txt",
    "/.well-known/security.txt",
}

# Nginx combined log format parser
LOG_PATTERN = re.compile(
    r'^(\S+) - - \[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}) [^\]]*\] '
    r'"(\S+) (\S+) \S+" (\d+)'
)

DATE_FORMAT = "%d/%b/%Y:%H:%M:%S"


# =========================================================================
# Helpers
# =========================================================================

def should_skip_ip(ip_str: str) -> bool:
    """Skip Cloudflare edge IPs, localhost, and private networks."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in ALL_SKIP_NETS)
    except ValueError:
        return True


def is_pattern_match(path: str) -> bool:
    """Check if path matches any instant-block pattern."""
    return any(p.search(path) for p in COMPILED_PATTERNS)


def is_whitelisted(path: str) -> bool:
    """Check if path is whitelisted (browser auto-requests)."""
    return path.lower() in WHITELIST_PATHS


def parse_timestamp(date_str: str) -> float:
    """Parse nginx date string to Unix timestamp."""
    try:
        dt = datetime.strptime(date_str, DATE_FORMAT)
        return dt.timestamp()
    except ValueError:
        return time.time()


# =========================================================================
# Main
# =========================================================================

def main():
    if not os.path.exists(ACCESS_LOG_PATH):
        print(f"ERROR: Access log not found at {ACCESS_LOG_PATH}")
        sys.exit(1)

    if not os.path.exists(SECURITY_DB_PATH):
        print(f"ERROR: Security database not found at {SECURITY_DB_PATH}")
        print("Run migration_ip_reputation.py first.")
        sys.exit(1)

    print(f"Parsing {ACCESS_LOG_PATH} ...")
    if DRY_RUN:
        print("*** DRY RUN - no database writes ***")
    print()

    # Accumulate per-IP stats
    ip_data = defaultdict(lambda: {
        "total": 0, "errors": 0, "patterns": 0,
        "first_seen": float("inf"), "last_seen": 0.0,
        "last_path": None, "ok_requests": 0,
    })

    total_lines = 0
    parsed_lines = 0
    skipped_cf = 0
    skipped_local = 0

    with open(ACCESS_LOG_PATH, "r", errors="replace") as f:
        for line in f:
            total_lines += 1
            m = LOG_PATTERN.match(line)
            if not m:
                continue

            ip_str = m.group(1)
            date_str = m.group(2)
            path = m.group(4)
            status = int(m.group(5))

            if should_skip_ip(ip_str):
                # Count for stats
                try:
                    ip_obj = ipaddress.ip_address(ip_str)
                    if ip_obj.is_private or ip_obj.is_loopback:
                        skipped_local += 1
                    else:
                        skipped_cf += 1
                except ValueError:
                    skipped_local += 1
                continue

            parsed_lines += 1
            ts = parse_timestamp(date_str)
            d = ip_data[ip_str]
            d["total"] += 1
            d["last_path"] = path

            if ts < d["first_seen"]:
                d["first_seen"] = ts
            if ts > d["last_seen"]:
                d["last_seen"] = ts

            if is_pattern_match(path):
                d["patterns"] += 1
                d["errors"] += 1
            elif status in (403, 444):
                d["errors"] += 1
            elif status == 404 and not is_whitelisted(path):
                d["errors"] += 1
            elif 200 <= status < 400:
                d["ok_requests"] += 1

    print(f"Total lines: {total_lines}")
    print(f"Skipped (Cloudflare proxy): {skipped_cf}")
    print(f"Skipped (localhost/private): {skipped_local}")
    print(f"Parsed (real IPs): {parsed_lines}")
    print(f"Unique IPs: {len(ip_data)}")
    print()

    # Calculate scores
    records = []
    for ip, d in ip_data.items():
        score = (
            d["patterns"] * SCORE_PATTERN_MATCH
            + (d["errors"] - d["patterns"]) * SCORE_404
            + d["ok_requests"] * SCORE_200_REWARD
        )
        score = max(0.0, score)

        # Estimate times_banned from historical score thresholds
        times_banned = 0
        if score >= 200:
            times_banned = 3
        elif score >= 50:
            times_banned = 2
        elif score >= 20:
            times_banned = 1

        records.append({
            "ip": ip,
            "score": score,
            "total_requests": d["total"],
            "error_requests": d["errors"],
            "pattern_hits": d["patterns"],
            "times_banned": times_banned,
            "first_seen": d["first_seen"] if d["first_seen"] != float("inf") else time.time(),
            "last_seen": d["last_seen"],
            "last_path": d["last_path"],
        })

    # Filter: only import IPs with score > 0 or times_banned > 0
    meaningful = [r for r in records if r["score"] > 0 or r["times_banned"] > 0]
    clean = [r for r in records if r["score"] <= 0 and r["times_banned"] == 0]

    meaningful.sort(key=lambda r: r["score"], reverse=True)

    print(f"IPs with score > 0: {len(meaningful)}")
    print(f"IPs with score = 0 (clean, skipped): {len(clean)}")
    print()

    # Preview top 25
    print(f"{'IP':42s} {'Score':>8s} {'Total':>6s} {'Errors':>6s} {'Patterns':>8s} {'Ratio':>6s} {'Bans':>5s}")
    print("-" * 90)
    for r in meaningful[:25]:
        ratio = r["error_requests"] / r["total_requests"] if r["total_requests"] > 0 else 0
        print(
            f"{r['ip']:42s} {r['score']:8.0f} {r['total_requests']:6d} "
            f"{r['error_requests']:6d} {r['pattern_hits']:8d} {ratio:5.0%} {r['times_banned']:5d}"
        )

    if len(meaningful) > 25:
        print(f"  ... and {len(meaningful) - 25} more IPs")
    print()

    if DRY_RUN:
        print("DRY RUN complete. Run without --dry-run to import.")
        return

    # Insert into security.db
    now = time.time()
    conn = sqlite3.connect(SECURITY_DB_PATH)
    cursor = conn.cursor()

    inserted = 0
    for r in meaningful:
        cursor.execute(
            """INSERT OR REPLACE INTO IP_REPUTATION
                (ip, score, total_requests, error_requests, pattern_hits,
                 times_banned, banned_until, first_seen, last_seen,
                 last_decay, last_path, last_ban_reason)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                r["ip"], r["score"], r["total_requests"], r["error_requests"],
                r["pattern_hits"], r["times_banned"],
                r["first_seen"], r["last_seen"], now,
                r["last_path"], "Imported from nginx access.log",
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()

    print(f"Imported {inserted} IPs into {SECURITY_DB_PATH}")
    print("Done. The reputation system will load these on next app startup.")


if __name__ == "__main__":
    main()
