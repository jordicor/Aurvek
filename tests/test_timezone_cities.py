"""Check useful search behavior against the actual offline GeoNames catalog."""

from zoneinfo import ZoneInfo

import pytest

import timezone_cities
from timezone_cities import build_timezone_city_catalog, search_timezone_cities


@pytest.fixture(scope="module", autouse=True)
def city_catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("timezone-cities") / "cities.sqlite"
    assert build_timezone_city_catalog(path) == {"catalog_built": True}
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(timezone_cities, "CITY_CATALOG_PATH", path)
        yield path


def test_catalog_build_is_idempotent(city_catalog):
    before = city_catalog.stat().st_mtime_ns
    assert build_timezone_city_catalog(city_catalog) == {"catalog_built": False}
    assert city_catalog.stat().st_mtime_ns == before


def test_cordoba_homonyms_include_argentina_and_spain_with_correct_timezones():
    results = search_timezone_cities("Córdoba", "es")
    assert results[0]["country"] == "Argentina"
    by_country = {row["country"]: row for row in results}
    assert by_country["Argentina"]["timezone"] == "America/Argentina/Cordoba"
    assert by_country["España"]["timezone"] == "Europe/Madrid"
    assert by_country["Argentina"]["region"]
    assert by_country["España"]["region"]
    assert len(results) <= 8
    assert len({row["id"] for row in results}) == len(results)
    for row in results:
        assert ZoneInfo(row["timezone"]).key == row["timezone"]


@pytest.mark.parametrize("query", [
    "Córdoba, España", "  CORDOBA , espana  ", "Cordoba, Spain", "Cordoba, ESP",
    "Cordoba, ES", "Cordoba, Espagne", "Cordoba, Spanien", "Cordoba, Spagna",
    "Cordoba, Espanha", "Cordoba, スペイン",
])
def test_country_qualifier_accepts_accents_languages_and_iso_codes(query):
    results = search_timezone_cities(query, "es")
    assert results
    assert results[0]["city"] == "Córdoba"
    assert results[0]["timezone"] == "Europe/Madrid"
    assert all(row["country"] == "España" for row in results)


def test_miami_prioritizes_florida_and_disambiguates_other_exact_matches():
    results = search_timezone_cities("Miami")
    assert results[0] == {
        "id": 4164138, "city": "Miami", "region": "Florida",
        "country": "United States", "timezone": "America/New_York",
    }
    assert any(row["city"] == "Miami" and row["region"] == "Oklahoma"
               and row["timezone"] == "America/Chicago" for row in results)
    assert any(row["city"] == "Miami" and row["country"] == "Australia"
               and row["timezone"] == "Australia/Brisbane" for row in results)


def test_prefixes_and_alternate_names_find_the_city():
    assert search_timezone_cities("Cordob, ES")[0]["city"] == "Córdoba"
    assert search_timezone_cities("Cordoue, ES")[0]["timezone"] == "Europe/Madrid"
    assert search_timezone_cities("miami", "es-ES")[0]["country"] == "Estados Unidos"


@pytest.mark.parametrize("query", [
    "", "  ", "a", ", España", "city-does-not-exist-qzx", "Cordoba, Atlantis", "x" * 101,
])
def test_empty_invalid_and_unmatched_queries_have_no_suggestions(query):
    assert search_timezone_cities(query) == []
