# GeoNames region names

`admin1CodesASCII.txt` is an unmodified GeoNames export downloaded on 2026-09-08:

- Source: https://download.geonames.org/export/dump/admin1CodesASCII.txt
- Format: https://download.geonames.org/export/dump/readme.txt
- Attribution: GeoNames, https://www.geonames.org/
- License: Creative Commons Attribution 4.0 International,
  https://creativecommons.org/licenses/by/4.0/
- SHA-256: `590651498043f674accda2b7f46d21286cda0e290b02f8561c5005eee9a5448c`

Aurvek joins these first-level region names to the city records bundled with
`geonamescache==3.0.2` (the cities500 catalog) for its offline time-zone search.
The package code is MIT licensed; its underlying geographic data is from GeoNames
under CC BY 4.0. Both catalogs are used locally without requests to GeoNames.
Country labels come from the application's existing Babel/CLDR dependency.
GeoNames supplies region names in English; the city names come from its city
catalog and may be in the local language. No region translations are inferred.

Fresh installation runs `init_db.py` to build `cities.sqlite`
beside this file. This derived catalog is ignored by Git and remains separate
from `Aurvek.db`. It stores city details and a normalized name index on disk, so
requests read matching suggestions without retaining the worldwide city catalog
in application memory. There is no automatic build during requests. The builder
runs during initialization and atomically replaces the catalog only when the
complete new index is ready. It skips rebuilding when the package version,
catalog format, population threshold, and region checksum match.

The data is provided as is, without a guarantee of accuracy or completeness.
To refresh it during maintenance, update the package pin and replace this export
from the same source, then update this date and checksum, rebuild with
`python -c "from timezone_cities import build_timezone_city_catalog; build_timezone_city_catalog()"`,
and run the focused city-search tests. Only time zones present in the installed IANA database are
offered; keep the system time-zone data/current `tzdata` dependency up to date.
