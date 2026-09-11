import sqlite3
import os
import uuid
from help_schema import ensure_help_schema


def _default_mem0_platform_id():
    value = os.getenv("MEM0_PLATFORM_ID") or os.getenv("AURVEK_INSTANCE_ID")
    if value and value.strip():
        return _sanitize_platform_id(value)
    return "aurvek-%s" % uuid.uuid4().hex[:12]


def _sanitize_platform_id(value):
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value).strip())
    safe = safe.strip("._-")
    return (safe or "aurvek-local")[:64]


def _clean(value):
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _parse_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _memory_defaults():
    active_provider = (_clean(os.getenv("MEMORY_ACTIVE_PROVIDER")) or "").lower()
    if active_provider not in {"none", "atagia", "mem0"}:
        active_provider = "atagia" if _parse_bool(os.getenv("ATAGIA_ENABLED")) else "none"

    default_scope = (_clean(os.getenv("MEMORY_DEFAULT_SCOPE")) or "prompt").lower()
    if default_scope not in {"global", "prompt"}:
        default_scope = "prompt"

    try:
        timeout = float(_clean(os.getenv("MEM0_TIMEOUT_SECONDS")) or "30.0")
    except ValueError:
        timeout = 30.0
    if timeout <= 0:
        timeout = 30.0

    try:
        top_k = int(_clean(os.getenv("MEM0_TOP_K")) or "8")
    except ValueError:
        top_k = 8
    top_k = min(max(top_k, 1), 50)

    try:
        none_context_max_tokens = int(
            _clean(os.getenv("MEMORY_NONE_CONTEXT_MAX_TOKENS")) or "128000"
        )
    except ValueError:
        none_context_max_tokens = 128000
    none_context_max_tokens = min(max(none_context_max_tokens, 0), 2_000_000)

    return [
        ("memory_active_provider", active_provider),
        ("memory_default_scope", default_scope),
        ("mem0_base_url", _clean(os.getenv("MEM0_BASE_URL")) or "http://127.0.0.1:8888"),
        ("mem0_platform_id", _default_mem0_platform_id()),
        ("mem0_timeout_seconds", str(timeout)),
        ("mem0_top_k", str(top_k)),
        ("memory_none_context_max_tokens", str(none_context_max_tokens)),
        ("memory_none_context_exceptions", _clean(os.getenv("MEMORY_NONE_CONTEXT_EXCEPTIONS")) or "[]"),
    ]

def init_db():
    db_path = 'db/Aurvek.db'
    schema_path = 'aurvek_schema.sql'

    # Create db directory if it doesn't exist
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    try:
        with sqlite3.connect(db_path) as conn:
            with open(schema_path, 'r') as f:
                conn.executescript(f.read())
            ensure_help_schema(conn)

            # Seed SYSTEM_CONFIG with ranking defaults
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('ranking_mode', 'piggyback')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('ranking_interval_hours', '6')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('ranking_weights', '{\"W1\":3,\"W2\":5,\"W3\":4,\"W4\":6,\"W5\":2,\"W6\":15,\"W7\":30}')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('ranking_last_updated', '0')")

            # Seed SYSTEM_CONFIG with geo-blocking defaults
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_enabled', '0')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_global_mode', 'deny')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_global_blocked_countries', '[]')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_global_blocked_continents', '[]')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_global_response_html', '')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_global_cf_rule_id', '')")
            conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES ('geo_landing_cf_rule_ids', '[]')")

            # Seed SYSTEM_CONFIG with storage-quota default (30 GB binary; 0 = unlimited)
            conn.execute(
                "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value, description) VALUES (?, ?, ?)",
                (
                    'storage_quota_default_bytes',
                    '32212254720',
                    'Default per-user storage quota in bytes (0 = unlimited)',
                ),
            )

            # Seed SYSTEM_CONFIG with GranSabio defaults
            gransabio_defaults = [
                ("gransabio_enabled", "false"),
                ("gransabio_url", "http://127.0.0.1:8000"),
                ("gransabio_default_generator", ""),
                ("gransabio_default_qa_models", "[]"),
                ("gransabio_default_min_score", "8.0"),
                ("gransabio_default_max_iterations", "3"),
                ("gransabio_default_gran_sabio_model", ""),
                ("gransabio_default_arbiter_model", ""),
                ("gransabio_default_smart_edit", "auto"),
                ("gransabio_default_gran_sabio_fallback", "true"),
                ("gransabio_default_verbose", "false"),
                ("gransabio_default_context_max_tokens", "4000"),
                ("gransabio_cost_safety_multiplier", "3"),
                ("gransabio_extra_allowed_ips", ""),
            ]
            for key, value in gransabio_defaults:
                conn.execute(
                    "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
                    (key, value),
                )

            # Seed generic memory provider defaults
            for key, value in _memory_defaults():
                conn.execute(
                    "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
                    (key, value),
                )

            # Seed synthetic GranSabio LLM row (existence check - no UNIQUE on LLM)
            existing = conn.execute(
                "SELECT id FROM LLM WHERE machine = 'GranSabio' AND model = 'gransabio-pipeline'"
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO LLM (machine, model, input_token_cost, output_token_cost) "
                    "VALUES ('GranSabio', 'gransabio-pipeline', 0, 0)"
                )

            conn.commit()

        from timezone_cities import build_timezone_city_catalog

        build_timezone_city_catalog()
        print(f"Database {db_path} initialized successfully.")
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    except FileNotFoundError as e:
        print(f"Schema file not found: {e}")

if __name__ == '__main__':
    init_db()
