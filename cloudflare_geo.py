"""
Cloudflare WAF-based Geo-Fencing Service.

Provides 4 components:
1. Data loader: Loads geo_countries.json at import time
2. CloudflareGeoClient: CF API wrapper for WAF Custom Rules
3. GeoExpressionCompiler: Stateless expression builder for CF rule expressions
4. GeoSyncEngine: Orchestrator singleton that reads DB, compiles rules, pushes to CF
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from babel import Locale

from database import get_db_connection
from i18n import LANGUAGES

logger = logging.getLogger(__name__)


# =============================================================================
# Component 1: Data Loader
# =============================================================================

_GEO_DATA_PATH = Path(__file__).parent / "data" / "config" / "geo_countries.json"

_geo_data: dict = {}

try:
    with open(_GEO_DATA_PATH, "r", encoding="utf-8") as f:
        _geo_data = json.load(f)
    logger.debug("Loaded geo data: %d countries, %d continents",
                 len(_geo_data.get("countries", [])),
                 len(_geo_data.get("continents", {})))
except FileNotFoundError:
    logger.warning("geo_countries.json not found at %s - geo features will be unavailable", _GEO_DATA_PATH)
except json.JSONDecodeError as exc:
    logger.error("Failed to parse geo_countries.json: %s", exc)

# Pre-build lookup sets for fast validation
_VALID_COUNTRY_CODES: set[str] = {c["code"] for c in _geo_data.get("countries", [])}
_VALID_CONTINENT_CODES: set[str] = set(_geo_data.get("continents", {}).keys())
_CONTINENT_TO_COUNTRIES: dict[str, list[str]] = {}

for _country in _geo_data.get("countries", []):
    _continent = _country.get("continent", "")
    if _continent not in _CONTINENT_TO_COUNTRIES:
        _CONTINENT_TO_COUNTRIES[_continent] = []
    _CONTINENT_TO_COUNTRIES[_continent].append(_country["code"])


def get_all_geo_data(language: str | None = None) -> dict:
    """Return geo codes, optionally with a localized copy of their display names."""
    if language is None:
        return _geo_data
    territories = Locale.parse(LANGUAGES[language], sep="-").territories
    regions = {"AF": "002", "AN": "AQ", "AS": "142", "EU": "150", "NA": "003", "OC": "009", "SA": "005"}
    return {
        **_geo_data,
        "continents": {code: territories.get(regions.get(code, code), name)
                       for code, name in _geo_data.get("continents", {}).items()},
        "countries": [{**country, "name": territories.get(country["code"], country["name"])}
                      for country in _geo_data.get("countries", [])],
    }


def validate_country_codes(codes: list[str]) -> list[str]:
    """Return only valid ISO alpha-2 country codes from the input list."""
    return [c.upper() for c in codes if c.upper() in _VALID_COUNTRY_CODES]


def validate_continent_codes(codes: list[str]) -> list[str]:
    """Return only valid continent codes from the input list."""
    return [c.upper() for c in codes if c.upper() in _VALID_CONTINENT_CODES]


def get_countries_for_continent(continent_code: str) -> list[str]:
    """Return country codes belonging to a continent."""
    return list(_CONTINENT_TO_COUNTRIES.get(continent_code.upper(), []))


# =============================================================================
# Component 2: CloudflareGeoClient
# =============================================================================

class CloudflareGeoClient:
    """Wraps Cloudflare API calls for WAF Custom Rules management."""

    CF_API_BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        self._email = os.getenv("CLOUDFLARE_EMAIL", "").strip().strip('"').strip("'")
        self._api_key = os.getenv("CLOUDFLARE_API_KEY", "").strip().strip('"').strip("'")
        self._zone_id = os.getenv("CLOUDFLARE_ZONE_ID", "").strip().strip('"').strip("'")

    def is_configured(self) -> bool:
        """True if email, api_key, and zone_id are all non-empty."""
        return bool(self._email and self._api_key and self._zone_id)

    def _headers(self) -> dict:
        """Return authentication headers for CF API."""
        return {
            "X-Auth-Email": self._email,
            "X-Auth-Key": self._api_key,
            "Content-Type": "application/json",
        }

    async def get_zone_info(self) -> dict:
        """GET zone details including plan level."""
        url = f"{self.CF_API_BASE}/zones/{self._zone_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            logger.debug("CF API: GET %s", url)
            resp = await client.get(url, headers=self._headers())
            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error getting zone info: {errors}")
            return data.get("result", {})

    async def get_ruleset(self) -> dict:
        """GET the WAF custom rules ruleset. Returns empty rules list on 404."""
        url = f"{self.CF_API_BASE}/zones/{self._zone_id}/rulesets/phases/http_request_firewall_custom/entrypoint"
        async with httpx.AsyncClient(timeout=15) as client:
            logger.debug("CF API: GET %s", url)
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 404:
                logger.info("No existing WAF custom ruleset found (404) - will create on first sync")
                return {"id": None, "rules": []}
            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error getting ruleset: {errors}")
            return data.get("result", {"id": None, "rules": []})

    async def update_ruleset(self, rules: list[dict]) -> dict:
        """Merge aurvek-managed rules with existing non-aurvek rules and push to CF.

        This is the key method:
        1. GET the current ruleset to get its ID and all existing rules
        2. Filter out rules whose description starts with '[aurvek-geo-'
        3. Merge in the new rules list (aurvek rules FIRST for priority)
        4. PUT the merged ruleset (or POST to create if none exists)
        """
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: Get current ruleset
            current = await self.get_ruleset()
            ruleset_id = current.get("id")
            existing_rules = current.get("rules", [])

            # Step 2: Filter out our managed rules
            non_aurvek_rules = [
                r for r in existing_rules
                if not (r.get("description", "").startswith("[aurvek-geo-"))
            ]

            # Step 3: Merge - aurvek rules first (global before landing) for priority
            merged_rules = rules + non_aurvek_rules

            # Strip 'id', 'ref', 'version', 'last_updated' from rules before PUT
            # (CF rejects these in the request body)
            cleaned_rules = []
            for rule in merged_rules:
                cleaned = {
                    "action": rule["action"],
                    "expression": rule["expression"],
                    "description": rule.get("description", ""),
                    "enabled": rule.get("enabled", True),
                }
                if "action_parameters" in rule:
                    cleaned["action_parameters"] = rule["action_parameters"]
                cleaned_rules.append(cleaned)

            if ruleset_id:
                # Step 4a: PUT to update existing ruleset
                url = f"{self.CF_API_BASE}/zones/{self._zone_id}/rulesets/{ruleset_id}"
                payload = {"rules": cleaned_rules}
                logger.debug("CF API: PUT %s (%d rules)", url, len(cleaned_rules))
                resp = await client.put(url, headers=self._headers(), json=payload)
            else:
                # Step 4b: POST to create new ruleset
                url = f"{self.CF_API_BASE}/zones/{self._zone_id}/rulesets"
                payload = {
                    "name": "Aurvek Geo-Fencing Rules",
                    "kind": "zone",
                    "phase": "http_request_firewall_custom",
                    "rules": cleaned_rules,
                }
                logger.debug("CF API: POST %s (%d rules)", url, len(cleaned_rules))
                resp = await client.post(url, headers=self._headers(), json=payload)

            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error updating ruleset: {errors}")

            result = data.get("result", {})
            logger.info("CF ruleset updated: %d total rules (%d aurvek-managed)",
                        len(result.get("rules", [])), len(rules))
            return result

    async def check_managed_transforms(self) -> bool:
        """Check if visitor location headers (CF-IPCountry) are enabled."""
        url = f"{self.CF_API_BASE}/zones/{self._zone_id}/managed_headers"
        async with httpx.AsyncClient(timeout=15) as client:
            logger.debug("CF API: GET %s", url)
            resp = await client.get(url, headers=self._headers())
            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error checking managed transforms: {errors}")

            managed_request_headers = data.get("result", {}).get("managed_request_headers", [])
            for header in managed_request_headers:
                if header.get("id") == "add_visitor_location_headers" and header.get("enabled"):
                    return True
            return False

    async def enable_managed_transforms(self) -> dict:
        """Enable visitor location headers via managed transforms."""
        url = f"{self.CF_API_BASE}/zones/{self._zone_id}/managed_headers"
        async with httpx.AsyncClient(timeout=15) as client:
            # First get current state to preserve other settings
            logger.debug("CF API: GET %s (for enable)", url)
            get_resp = await client.get(url, headers=self._headers())
            get_data = get_resp.json()
            if not get_data.get("success"):
                errors = get_data.get("errors", [])
                raise RuntimeError(f"CF API error reading managed transforms: {errors}")

            result = get_data.get("result", {})
            managed_request_headers = result.get("managed_request_headers", [])
            managed_response_headers = result.get("managed_response_headers", [])

            # Enable visitor location headers
            updated = False
            for header in managed_request_headers:
                if header.get("id") == "add_visitor_location_headers":
                    header["enabled"] = True
                    updated = True
                    break

            if not updated:
                # Header not in list - add it
                managed_request_headers.append({
                    "id": "add_visitor_location_headers",
                    "enabled": True,
                })

            payload = {
                "managed_request_headers": managed_request_headers,
                "managed_response_headers": managed_response_headers,
            }
            logger.debug("CF API: PATCH %s", url)
            resp = await client.patch(url, headers=self._headers(), json=payload)
            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error enabling managed transforms: {errors}")

            logger.info("CF managed transforms: visitor location headers enabled")
            return data.get("result", {})

    async def upsert_image_skip_rule(self) -> dict:
        """Create or update the WAF skip rule that allows AI providers to download authenticated images.

        This rule skips Super Bot Fight Mode (via phases) and Security Level checks
        (via products) for requests that match the authenticated image URL pattern,
        preventing Cloudflare from blocking AI provider servers when they try to
        download user-uploaded images.
        """
        skip_rule = {
            "action": "skip",
            "action_parameters": {
                "phases": ["http_request_sbfm"],
                "products": ["securityLevel"],
            },
            "expression": (
                '(http.request.uri.path contains "/users/" '
                'and http.request.uri.query contains "token=" '
                'and http.request.uri.path contains "/img/")'
            ),
            "description": "[aurvek-img-skip] Allow AI providers to download authenticated images",
            "enabled": True,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: Get current ruleset
            current = await self.get_ruleset()
            ruleset_id = current.get("id")
            existing_rules = current.get("rules", [])

            if not ruleset_id:
                raise RuntimeError("No existing WAF ruleset found - run a geo sync first to create one")

            # Step 2: Filter out any existing aurvek-img rules to avoid duplicates
            other_rules = [
                r for r in existing_rules
                if not r.get("description", "").startswith("[aurvek-img-")
            ]

            # Step 3: Prepend skip rule (skip rules must come before block rules)
            merged_rules = [skip_rule] + other_rules

            # Strip CF-managed fields before PUT
            cleaned_rules = []
            for rule in merged_rules:
                cleaned = {
                    "action": rule["action"],
                    "expression": rule["expression"],
                    "description": rule.get("description", ""),
                    "enabled": rule.get("enabled", True),
                }
                if "action_parameters" in rule:
                    cleaned["action_parameters"] = rule["action_parameters"]
                cleaned_rules.append(cleaned)

            # Step 4: PUT the merged ruleset
            url = f"{self.CF_API_BASE}/zones/{self._zone_id}/rulesets/{ruleset_id}"
            payload = {"rules": cleaned_rules}
            logger.debug("CF API: PUT %s (%d rules, including img-skip)", url, len(cleaned_rules))
            resp = await client.put(url, headers=self._headers(), json=payload)

            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                raise RuntimeError(f"CF API error upserting image skip rule: {errors}")

            result = data.get("result", {})
            logger.info("CF image skip rule upserted: %d total rules in ruleset", len(result.get("rules", [])))
            return result


# =============================================================================
# Component 3: GeoExpressionCompiler
# =============================================================================

class GeoExpressionCompiler:
    """Stateless expression builder for Cloudflare WAF rule expressions."""

    @staticmethod
    def expand_to_countries(countries: list[str], continents: list[str]) -> set[str]:
        """Expand explicit country codes + continent codes into a deduplicated country set."""
        result: set[str] = set()
        for code in countries:
            upper = code.upper()
            if upper in _VALID_COUNTRY_CODES:
                result.add(upper)
        for continent in continents:
            for country_code in get_countries_for_continent(continent):
                result.add(country_code)
        return result

    @staticmethod
    def build_global_expression(mode: str, country_codes: set[str], hostname: str = "") -> str:
        """Build a CF expression for global geo-blocking.

        - deny mode: blocks listed countries
        - allow mode: blocks everyone EXCEPT listed countries

        When hostname is provided, the rule is scoped to that specific host
        so it does not affect other (sub)domains in the same Cloudflare zone.
        """
        if not country_codes:
            return ""
        # CF uses space-separated codes inside braces, no commas
        codes_str = " ".join(f'"{c}"' for c in sorted(country_codes))
        if mode == "deny":
            country_expr = f"ip.src.country in {{{codes_str}}}"
        else:
            # allow mode: block everyone NOT in the list
            country_expr = f"not ip.src.country in {{{codes_str}}}"

        if hostname:
            return f'({country_expr} and http.host eq "{hostname}")'
        return country_expr

    @staticmethod
    def build_landing_fragment(
        public_id: str,
        custom_domain: Optional[str],
        platform_domain: str,
        mode: str,
        country_codes: set[str],
    ) -> str:
        """Build a CF expression fragment for a single landing page."""
        if not country_codes:
            return ""

        codes_str = " ".join(f'"{c}"' for c in sorted(country_codes))

        if mode == "deny":
            country_expr = f"ip.src.country in {{{codes_str}}}"
        else:
            country_expr = f"not ip.src.country in {{{codes_str}}}"

        # URL matching: scope platform paths to the platform hostname,
        # custom domains are already host-specific
        url_parts = []
        if platform_domain:
            url_parts.append(
                f'(http.request.uri.path contains "/p/{public_id}/" and http.host eq "{platform_domain}")'
            )
        else:
            url_parts.append(f'http.request.uri.path contains "/p/{public_id}/"')
        if custom_domain:
            url_parts.append(f'http.host eq "{custom_domain}"')
        url_expr = " or ".join(url_parts)

        return f"({country_expr} and ({url_expr}))"

    @staticmethod
    def pack_fragments(fragments: list[str], max_bytes: int = 4096) -> list[str]:
        """Pack multiple landing fragments into rule expressions, ORing them together.

        Each rule expression must be under max_bytes. Returns a list of combined
        expressions, one per CF rule.
        """
        if not fragments:
            return []

        packed: list[str] = []
        current_parts: list[str] = []
        current_size = 0

        for fragment in fragments:
            fragment_bytes = len(fragment.encode("utf-8"))

            if not current_parts:
                # First fragment in a new batch
                current_parts.append(fragment)
                current_size = fragment_bytes
                continue

            # Calculate size if we added this fragment: existing + " or " + new fragment
            separator = " or "
            combined_size = current_size + len(separator.encode("utf-8")) + fragment_bytes

            if combined_size > max_bytes:
                # Flush current batch
                packed.append(separator.join(current_parts))
                current_parts = [fragment]
                current_size = fragment_bytes
            else:
                current_parts.append(fragment)
                current_size = combined_size

        # Flush remaining
        if current_parts:
            packed.append(" or ".join(current_parts))

        return packed


# =============================================================================
# Component 4: GeoSyncEngine
# =============================================================================

# Default 451 response HTML
_DEFAULT_451_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unavailable</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:2rem}
.c{max-width:480px}
h1{font-size:1.5rem;margin-bottom:1rem;color:#fff}
p{font-size:1rem;line-height:1.6;color:#aaa}
.code{font-size:3rem;font-weight:700;color:#4a4a6a;margin-bottom:1.5rem}
</style>
</head>
<body>
<div class="c">
<div class="code">451</div>
<h1>Content Not Available</h1>
<p>This content is not available in your region.</p>
</div>
</body>
</html>"""


class GeoSyncEngine:
    """Orchestrator singleton that reads DB, compiles rules, and pushes to CF."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._client = CloudflareGeoClient()
        self._compiler = GeoExpressionCompiler()

    async def sync_all(self) -> dict:
        """Full sync: read config from DB, compile rules, push to Cloudflare."""
        async with self._lock:
            if not self._client.is_configured():
                return {
                    "success": False,
                    "error": "Cloudflare credentials not configured",
                    "rules_pushed": 0,
                }

            # Read global geo config from SYSTEM_CONFIG
            geo_enabled = False
            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.execute(
                    "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'geo_%'"
                )
                rows = await cursor.fetchall()
                config = {row[0]: row[1] for row in rows}
                geo_enabled = config.get("geo_enabled", "0") == "1"

            if not geo_enabled:
                # Remove all aurvek-managed rules
                result = await self.remove_all_rules()
                result["reason"] = "geo_enabled is false - all rules removed"
                return result

            # Build rules
            all_rules: list[dict] = []

            global_rules = await self._build_global_rule(config)
            all_rules.extend(global_rules)

            landing_rules = await self._build_landing_rules()
            all_rules.extend(landing_rules)

            if not all_rules:
                # No rules to push, clean up any stale ones
                result = await self.remove_all_rules()
                result["reason"] = "no geo rules to apply"
                return result

            # Push to CF
            logger.info("Pushing %d geo rules to Cloudflare (%d global, %d landing)",
                        len(all_rules), len(global_rules), len(landing_rules))
            cf_result = await self._client.update_ruleset(all_rules)

            # Track rule IDs
            await self._track_rule_ids(cf_result)

            return {
                "success": True,
                "rules_pushed": len(all_rules),
                "global_rules": len(global_rules),
                "landing_rules": len(landing_rules),
                "cf_ruleset_id": cf_result.get("id"),
            }

    async def remove_all_rules(self) -> dict:
        """Remove all aurvek geo rules from Cloudflare."""
        if not self._client.is_configured():
            return {
                "success": False,
                "error": "Cloudflare credentials not configured",
                "rules_removed": 0,
            }

        logger.info("Removing all aurvek geo rules from Cloudflare")
        cf_result = await self._client.update_ruleset([])

        # Clear tracked rule IDs
        async with get_db_connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO SYSTEM_CONFIG (key, value, updated_at) "
                "VALUES ('geo_global_cf_rule_id', '', CURRENT_TIMESTAMP)"
            )
            await conn.execute(
                "INSERT OR REPLACE INTO SYSTEM_CONFIG (key, value, updated_at) "
                "VALUES ('geo_landing_cf_rule_ids', '[]', CURRENT_TIMESTAMP)"
            )
            await conn.commit()

        return {
            "success": True,
            "rules_removed": True,
            "cf_ruleset_id": cf_result.get("id"),
        }

    async def _build_global_rule(self, config: dict) -> list[dict]:
        """Build the global geo-block rule from SYSTEM_CONFIG values."""
        mode = config.get("geo_global_mode", "deny")
        if mode not in ("deny", "allow"):
            mode = "deny"

        blocked_countries_raw = config.get("geo_global_blocked_countries", "[]")
        blocked_continents_raw = config.get("geo_global_blocked_continents", "[]")

        try:
            blocked_countries = json.loads(blocked_countries_raw) if blocked_countries_raw else []
        except (json.JSONDecodeError, TypeError):
            blocked_countries = []

        try:
            blocked_continents = json.loads(blocked_continents_raw) if blocked_continents_raw else []
        except (json.JSONDecodeError, TypeError):
            blocked_continents = []

        if not blocked_countries and not blocked_continents:
            return []

        country_codes = self._compiler.expand_to_countries(blocked_countries, blocked_continents)
        if not country_codes:
            return []

        app_hostname = os.getenv("PRIMARY_APP_DOMAIN", "").strip().strip('"').strip("'")
        expression = self._compiler.build_global_expression(mode, country_codes, hostname=app_hostname)
        if not expression:
            return []

        # Custom response HTML
        response_html = config.get("geo_global_response_html", "") or _DEFAULT_451_HTML

        rule = {
            "action": "block",
            "expression": expression,
            "description": f"[aurvek-geo-global] Platform-wide geo-block ({len(country_codes)} countries, mode={mode})",
            "enabled": True,
            "action_parameters": {
                "response": {
                    "status_code": 451,
                    "content_type": "text/html",
                    "content": response_html,
                },
            },
        }

        logger.info("Built global geo rule: mode=%s, %d countries", mode, len(country_codes))
        return [rule]

    async def _build_landing_rules(self) -> list[dict]:
        """Build per-landing geo-block rules from PROMPTS with geo_policy."""
        platform_domain = os.getenv("PRIMARY_APP_DOMAIN", "").strip().strip('"').strip("'")

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """SELECT p.id, p.public_id, p.geo_policy, cd.custom_domain
                   FROM PROMPTS p
                   LEFT JOIN PROMPT_CUSTOM_DOMAINS cd
                       ON p.id = cd.prompt_id AND cd.is_active = 1
                   WHERE p.geo_policy IS NOT NULL AND p.public_id IS NOT NULL"""
            )
            rows = await cursor.fetchall()

        fragments: list[str] = []
        for row in rows:
            prompt_id = row[0]
            public_id = row[1]
            geo_policy_raw = row[2]
            custom_domain = row[3]

            try:
                policy = json.loads(geo_policy_raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid geo_policy JSON for prompt %d, skipping", prompt_id)
                continue

            if not policy.get("enabled"):
                continue

            mode = policy.get("mode", "deny")
            if mode not in ("deny", "allow"):
                mode = "deny"

            countries = policy.get("countries", [])
            continents = policy.get("continents", [])

            country_codes = self._compiler.expand_to_countries(countries, continents)
            if not country_codes:
                continue

            fragment = self._compiler.build_landing_fragment(
                public_id=public_id,
                custom_domain=custom_domain,
                platform_domain=platform_domain,
                mode=mode,
                country_codes=country_codes,
            )
            if fragment:
                fragments.append(fragment)

        if not fragments:
            return []

        # Pack fragments into rules
        packed_expressions = self._compiler.pack_fragments(fragments)
        total_batches = len(packed_expressions)
        rules: list[dict] = []

        for i, expression in enumerate(packed_expressions, 1):
            rule = {
                "action": "block",
                "expression": expression,
                "description": f"[aurvek-geo-landing] Per-landing geo-block (batch {i}/{total_batches})",
                "enabled": True,
                "action_parameters": {
                    "response": {
                        "status_code": 451,
                        "content_type": "text/html",
                        "content": _DEFAULT_451_HTML,
                    },
                },
            }
            rules.append(rule)

        logger.info("Built %d landing geo rules from %d prompt fragments", len(rules), len(fragments))
        return rules

    async def _track_rule_ids(self, result: dict) -> None:
        """Extract rule IDs from CF response and save to SYSTEM_CONFIG."""
        rules = result.get("rules", [])

        global_rule_id = ""
        landing_rule_ids: list[str] = []

        for rule in rules:
            desc = rule.get("description", "")
            rule_id = rule.get("id", "")
            if desc.startswith("[aurvek-geo-global]"):
                global_rule_id = rule_id
            elif desc.startswith("[aurvek-geo-landing]"):
                landing_rule_ids.append(rule_id)

        async with get_db_connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO SYSTEM_CONFIG (key, value, updated_at) "
                "VALUES ('geo_global_cf_rule_id', ?, CURRENT_TIMESTAMP)",
                (global_rule_id,),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO SYSTEM_CONFIG (key, value, updated_at) "
                "VALUES ('geo_landing_cf_rule_ids', ?, CURRENT_TIMESTAMP)",
                (json.dumps(landing_rule_ids),),
            )
            await conn.commit()

        logger.debug("Tracked CF rule IDs: global=%s, landing=%s", global_rule_id, landing_rule_ids)


# =============================================================================
# Module-level singleton
# =============================================================================

geo_sync_engine = GeoSyncEngine()
