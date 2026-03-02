import sqlite3
import os

def init_db():
    db_path = 'db/Aurvek.db'
    schema_path = 'aurvek_schema.sql'

    # Create db directory if it doesn't exist
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    try:
        with sqlite3.connect(db_path) as conn:
            with open(schema_path, 'r') as f:
                conn.executescript(f.read())

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
            conn.commit()

        print(f"Database {db_path} initialized successfully.")
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    except FileNotFoundError as e:
        print(f"Schema file not found: {e}")

if __name__ == '__main__':
    init_db()