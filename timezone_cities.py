"""Offline city suggestions backed by a regenerable, read-only SQLite catalog."""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import os
import sqlite3
import tempfile
import unicodedata

from babel import Locale
import geonamescache

from i18n import LANGUAGES, normalize_language
from user_timezone import normalize_user_timezone


MAX_CITY_QUERY_LENGTH = 100
MAX_CITY_RESULTS = 8
_REGION_DATA = Path(__file__).resolve().parent / "data/config/geonames/admin1CodesASCII.txt"
CITY_CATALOG_PATH = _REGION_DATA.with_name("cities.sqlite")


def _search_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def _readonly_catalog(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def build_timezone_city_catalog(path: str | Path | None = None) -> dict[str, bool]:
    """Build during deployment, replacing the prior catalog only after success."""
    target = Path(path) if path is not None else CITY_CATALOG_PATH
    region_data = _REGION_DATA.read_bytes()
    metadata = {
        "schema_version": "1",
        "geonamescache_version": geonamescache.__version__,
        "min_city_population": "500",
        "regions_sha256": sha256(region_data).hexdigest(),
    }
    if target.exists():
        try:
            with closing(_readonly_catalog(target)) as connection:
                if dict(connection.execute("SELECT key, value FROM metadata")) == metadata:
                    return {"catalog_built": False}
        except sqlite3.DatabaseError:
            pass  # A damaged derived catalog can be rebuilt from its source data.

    regions = {}
    for line in region_data.decode("utf-8").splitlines():
        code, name, _ascii_name, _geoname_id = line.split("\t")
        regions[code] = name
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE cities (
                    id INTEGER PRIMARY KEY, city TEXT NOT NULL, search_name TEXT NOT NULL,
                    country_code TEXT NOT NULL, region TEXT NOT NULL,
                    timezone TEXT NOT NULL, population INTEGER NOT NULL
                );
                CREATE TABLE names (name TEXT NOT NULL, city_id INTEGER NOT NULL);
            """)
            remaining = geonamescache.GeonamesCache(min_city_population=500).get_cities()
            cities, names = [], []
            while remaining:
                _key, row = remaining.popitem()
                city_id = int(row["geonameid"])
                name = _search_text(row["name"])
                cities.append((
                    city_id, row["name"], name, row["countrycode"],
                    regions.get(f"{row['countrycode']}.{row['admin1code']}", ""),
                    row["timezone"], row["population"],
                ))
                aliases = {_search_text(alias) for alias in row["alternatenames"]}
                aliases.add(name)
                names.extend((alias, city_id) for alias in aliases if alias)
                if len(cities) >= 1000 or not remaining:
                    connection.executemany("INSERT INTO cities VALUES (?,?,?,?,?,?,?)", cities)
                    connection.executemany("INSERT INTO names VALUES (?,?)", names)
                    cities.clear()
                    names.clear()
            connection.execute("CREATE INDEX names_lookup ON names(name, city_id)")
            connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"catalog_built": True}


@lru_cache(maxsize=1)
def _country_data() -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Cache only the small country catalog; city names stay on disk."""
    countries = geonamescache.GeonamesCache().get_countries()
    labels = {}
    for language in LANGUAGES:
        territories = Locale.parse(language).territories
        labels[language] = {
            code: territories.get(code, country["name"])
            for code, country in countries.items()
        }
    aliases: dict[str, set[str]] = {}
    for code, country in countries.items():
        names = [code, country["iso3"], country["name"]]
        names.extend(localized[code] for localized in labels.values())
        for name in names:
            aliases.setdefault(_search_text(name), set()).add(code)
    return aliases, labels


def search_timezone_cities(query: str, language: str = "en") -> list[dict]:
    """Suggest up to eight cities by name/alias, optionally followed by a country.

    Exact names precede prefixes, with population breaking ties. Names and country
    filters ignore case and accents; countries accept all supported UI languages
    and ISO codes. No catalog is created or geographic data downloaded at runtime.
    """
    if not isinstance(query, str) or len(query) > MAX_CITY_QUERY_LENGTH:
        return []
    city_part, _separator, country_part = query.partition(",")
    city_query = _search_text(city_part)
    if len(city_query) < 2:
        return []
    country_query = _search_text(country_part)
    country_aliases, country_labels = _country_data()
    country_codes = country_aliases.get(country_query) if country_query else None
    if country_query and not country_codes:
        return []
    country_clause = ""
    parameters = [city_query, city_query + "\U0010ffff"]
    if country_codes:
        country_clause = f"AND c.country_code IN ({','.join('?' for _ in country_codes)})"
        parameters.extend(sorted(country_codes))
    parameters.extend((city_query, city_query))
    labels = country_labels[normalize_language(language) or "en"]
    results = []
    with closing(_readonly_catalog(CITY_CATALOG_PATH)) as connection:
        rows = connection.execute(f"""
            SELECT c.id, c.city, c.region, c.country_code, c.timezone
            FROM names n JOIN cities c ON c.id = n.city_id
            WHERE n.name >= ? AND n.name < ? {country_clause}
            GROUP BY c.id
            ORDER BY MIN(CASE WHEN c.search_name = ? THEN 0
                             WHEN n.name = ? THEN 1 ELSE 2 END),
                     c.population DESC, c.search_name, c.id
        """, parameters)
        for city_id, city, region, country_code, timezone in rows:
            try:
                timezone = normalize_user_timezone(timezone)
            except ValueError:
                continue
            if not timezone:
                continue
            results.append({
                "id": city_id, "city": city, "region": region,
                "country": labels.get(country_code, country_code), "timezone": timezone,
            })
            if len(results) == MAX_CITY_RESULTS:
                break
    return results
