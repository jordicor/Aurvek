"""Shared UI messages for Python, Jinja and the browser.

Catalogs are trusted application resources, never user templates. Language is
bound to a request or an explicit Translator; there is no process-wide locale.
"""

import json
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from babel import Locale
from babel.numbers import format_currency as babel_format_currency, format_decimal
from markupsafe import Markup, escape


LANGUAGES = MappingProxyType({
    "en": "en-US", "es": "es-ES", "ja": "ja-JP", "fr": "fr-FR",
    "pt": "pt-PT", "it": "it-IT", "de": "de-DE",
})
CATALOG_ROOT = Path(__file__).resolve().parent / "locales"
_PLURALS = frozenset({"zero", "one", "two", "few", "many", "other"})
_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}|[{}]")
_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")
_TAG = re.compile(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*\Z")
_log = logging.getLogger(__name__)


def normalize_language(value: str | None) -> str | None:
    """Normalize a supported BCP 47 language/region; reject arbitrary input."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip().replace("_", "-")
    if not _TAG.fullmatch(value):
        return None
    language = value.split("-", 1)[0].lower()
    return language if language in LANGUAGES else None


def negotiate_language(cookie: str | None, accept_language: str = "") -> str:
    """Respect an explicit cookie, then weighted HTTP language preferences."""
    language = normalize_language(cookie)
    if language:
        return language
    ranges = []
    for position, item in enumerate(accept_language[:4096].split(",")[:32]):
        parts = [part.strip() for part in item.split(";")]
        tag = parts[0].lower()
        if tag != "*" and not _TAG.fullmatch(tag):
            continue
        quality = 1.0
        if len(parts) > 1:
            if len(parts) != 2 or not re.fullmatch(r"q=(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", parts[1]):
                continue
            quality = float(parts[1][2:])
        ranges.append((tag, quality, position))
    choices = []
    for order, (language, region) in enumerate(LANGUAGES.items()):
        matches = []
        for tag, quality, position in ranges:
            if tag == "*":
                specificity = 0
            elif tag == language or tag == region.lower():
                specificity = tag.count("-") + 1
            elif tag.split("-", 1)[0] == language:
                # Aurvek has one regional convention per language; e.g. pt-BR
                # may select Portuguese without changing that convention.
                specificity = 1
            else:
                continue
            matches.append((specificity, quality, -position))
        if matches:
            _, quality, position = max(matches)
            if quality > 0:
                choices.append((quality, position, -order, language))
    return max(choices)[3] if choices else "en"


def message_parameters(pattern: str) -> frozenset[str]:
    parameters = set()
    for match in _TOKEN.finditer(pattern):
        if match.group(1):
            parameters.add(match.group(1))
        elif match.group(0) not in ("{{", "}}"):
            raise ValueError("Invalid message placeholder")
    return frozenset(parameters)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate catalog key: {key}")
        result[key] = value
    return result


def _validate_message(value) -> frozenset[str]:
    if isinstance(value, str) and value.strip():
        return message_parameters(value)
    if not isinstance(value, dict) or "other" not in value or not set(value) <= _PLURALS:
        raise ValueError("Expected text or cardinal plural variants including other")
    contracts = []
    for pattern in value.values():
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("Empty or non-text plural variant")
        contracts.append(message_parameters(pattern))
    if any(contract != contracts[0] for contract in contracts):
        raise ValueError("Plural variants must use the same parameters")
    return contracts[0] | {"count"}


def read_catalogs(root: Path) -> Mapping:
    """Validate catalog structure and translated contracts before use."""
    catalogs = {}
    for path in sorted(root.glob("*/*.json")):
        language, domain = path.parent.name, path.stem
        if language not in LANGUAGES or not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
            raise ValueError(f"Invalid catalog path: {path}")
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise ValueError(f"Expected catalog object: {path}")
        for key, value in data.items():
            if not _KEY.fullmatch(key):
                raise ValueError(f"Invalid message key: {domain}.{key}")
            try:
                _validate_message(value)
            except ValueError as exc:
                raise ValueError(f"{language}/{domain}.{key}: {exc}") from exc
        catalogs.setdefault(language, {})[domain] = data
    english = catalogs.get("en", {})
    if not english or not isinstance(english.get("common", {}).get("error.generic"), str):
        raise ValueError("English common.error.generic is required")
    if message_parameters(english["common"]["error.generic"]):
        raise ValueError("The generic error must not require parameters")
    for language, domains in catalogs.items():
        for domain, messages in domains.items():
            for key, value in messages.items():
                original = english.get(domain, {}).get(key)
                if original is None:
                    raise ValueError(f"No English source for {language}/{domain}.{key}")
                if isinstance(value, str) != isinstance(original, str):
                    raise ValueError(f"Message type differs: {language}/{domain}.{key}")
                if _validate_message(value) != _validate_message(original):
                    raise ValueError(f"Parameters differ: {language}/{domain}.{key}")
    return MappingProxyType({
        language: MappingProxyType({
            domain: MappingProxyType({
                key: MappingProxyType(value) if isinstance(value, dict) else value
                for key, value in messages.items()
            }) for domain, messages in domains.items()
        }) for language, domains in catalogs.items()
    })


@lru_cache(maxsize=1)
def get_catalogs() -> Mapping:
    return read_catalogs(CATALOG_ROOT)


@lru_cache(maxsize=len(LANGUAGES))
def _locale(language: str) -> Locale:
    return Locale.parse(LANGUAGES[language], sep="-")


def _parameter_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return str(int(value)) if value == int(value) else str(value)
    raise ValueError("Message parameters must be text or finite numbers")


@dataclass(frozen=True)
class Translator:
    language: str = "en"

    def __post_init__(self):
        object.__setattr__(self, "language", normalize_language(self.language) or "en")

    def _pattern(self, key: str, params: Mapping) -> str:
        """Select one complete message and validate its parameter contract."""
        catalogs = get_catalogs()
        domain, separator, name = key.partition(".")
        if not separator:
            raise ValueError("A message key must include its domain")
        language = self.language
        value = catalogs.get(language, {}).get(domain, {}).get(name)
        if value is None:
            language = "en"
            value = catalogs["en"].get(domain, {}).get(name)
        if value is None:
            raise ValueError("Unknown message key")
        if not isinstance(value, str):
            count = params.get("count")
            if isinstance(count, bool) or not isinstance(count, (int, float)) or not math.isfinite(count):
                raise ValueError("Plural messages require a finite numeric count")
            category = _locale(language).plural_form(count)
            value = value.get(category, value["other"])
            required = message_parameters(value) | {"count"}
        else:
            required = message_parameters(value)
        if set(params) != required:
            raise ValueError("Message parameters do not match the catalog")
        return value

    def render(self, key: str, **params) -> str:
        """Strict rendering for validation; t() provides the safe UI boundary."""
        value = self._pattern(key, params)
        text = {name: _parameter_text(value) for name, value in params.items()}
        return _TOKEN.sub(
            lambda match: text[match.group(1)] if match.group(1) else match.group(0)[0],
            value,
        )

    def html(self, key: str, **params) -> Markup:
        """Compose auth sentences with links from trusted, autoescaped Jinja macros."""
        try:
            pattern = str(escape(self._pattern(key, params)))
            text = {
                name: str(value) if isinstance(value, Markup) else str(escape(str(_parameter_text(value))))
                for name, value in params.items()
            }
            return Markup(_TOKEN.sub(
                lambda match: text[match.group(1)] if match.group(1) else match.group(0)[0],
                pattern,
            ))
        except (ValueError, TypeError, KeyError):
            _log.error("Invalid HTML UI message key=%s language=%s", key, self.language)
            return escape(self.t("common.error.generic"))

    def t(self, key: str, **params) -> str:
        try:
            return self.render(key, **params)
        except (ValueError, TypeError, KeyError):
            # Never include parameters, rendered text or exception details here.
            _log.error("Invalid UI message key=%s language=%s", key, self.language)
            catalogs = get_catalogs()
            return catalogs.get(self.language, {}).get("common", {}).get(
                "error.generic", catalogs["en"]["common"]["error.generic"]
            )

    def browser_payload(self, domains=()) -> dict:
        catalogs = get_catalogs()
        selected_domains = tuple(dict.fromkeys(("common", *domains)))
        if any(domain not in catalogs["en"] for domain in selected_domains):
            raise ValueError("Unknown browser catalog domain")
        resources = {}
        for language in dict.fromkeys(("en", self.language)):
            resources[language] = {
                domain: {
                    key: dict(value) if isinstance(value, Mapping) else value
                    for key, value in catalogs.get(language, {}).get(domain, {}).items()
                } for domain in selected_domains
            }
        return {
            "version": 1, "language": self.language,
            "locales": {language: LANGUAGES[language] for language in resources},
            "resources": resources,
        }

    def format_number(self, value, minimum_fraction_digits=0, maximum_fraction_digits=3) -> str:
        """Format presentation only; callers retain numeric values for inputs/calculations."""
        if not 0 <= minimum_fraction_digits <= maximum_fraction_digits <= 20:
            raise ValueError("Invalid display precision")
        pattern = "#,##0"
        if maximum_fraction_digits:
            pattern += "." + "0" * minimum_fraction_digits + "#" * (maximum_fraction_digits - minimum_fraction_digits)
        return format_decimal(value, format=pattern, locale=_locale(self.language))

    def format_currency(self, value, currency="USD", fraction_digits=2) -> str:
        """Keep the caller's currency and precision, including small usage costs."""
        if not isinstance(fraction_digits, int) or not 0 <= fraction_digits <= 20:
            raise ValueError("Invalid display precision")
        locale = _locale(self.language)
        digits = "#,##0" + ("." + "0" * fraction_digits if fraction_digits else "")
        pattern = re.sub(r"[#,0]+(?:\.[#,0]+)?", digits, locale.currency_formats["standard"].pattern)
        return babel_format_currency(value, currency, format=pattern, locale=locale, currency_digits=False)


def get_translator(request, current_user=None) -> Translator:
    """Reuse the loaded identity and request state; no authentication or DB IO."""
    existing = getattr(request.state, "i18n", None)
    if current_user is not None and getattr(current_user, "ui_language", None) is not None:
        language = normalize_language(current_user.ui_language) or "en"
    elif existing is not None:
        return existing
    else:
        language = negotiate_language(request.cookies.get("ui_language"), request.headers.get("accept-language", ""))
    if existing is None or existing.language != language:
        request.state.i18n = Translator(language)
    return request.state.i18n


def template_context(request) -> dict:
    translator = get_translator(request)
    return {"t": translator.t, "t_html": translator.html, "ui_language": translator.language,
            "i18n_payload": translator.browser_payload,
            "format_number": translator.format_number, "format_currency": translator.format_currency}
