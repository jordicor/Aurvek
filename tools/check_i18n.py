"""Validate the published catalog domains and literal translation references."""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from i18n import LANGUAGES, read_catalogs


def check_catalogs(languages):
    catalogs = read_catalogs(ROOT / "locales")
    english = catalogs["en"]
    problems = []
    for language in languages:
        if language not in LANGUAGES:
            problems.append(f"Unsupported language: {language}")
            continue
        for domain, messages in english.items():
            translated = catalogs.get(language, {}).get(domain, {})
            for key in sorted(set(messages) - set(translated)):
                problems.append(f"Missing translation: {language}/{domain}.{key}")
    # Check references across owned application code, including the F4 bridge
    # and F5/F6 domains. Authored/generated user assets are not application code.
    consumers = list(ROOT.glob("*.py"))
    consumers += list((ROOT / "templates").rglob("*.html"))
    consumers += list((ROOT / "data/static/js").rglob("*.js"))
    for directory in ("chat", "ai_runtime", "marketplace", "billing", "integrations", "legal", "tools/prompt_pipeline"):
        consumers += list((ROOT / directory).rglob("*.py"))
    consumers += [ROOT / path for path in ("tools/tts.py", "tools/download_pdf.py", "tools/download_mp3.py")]
    consumers = list(dict.fromkeys(consumers))
    # Only a complete literal argument is a literal key; dynamic code families
    # below are checked against their owning services instead of a partial key.
    reference = re.compile(r"\b(?:t|t_html|passwordT|voiceAdminT)\(\s*['\"]([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)['\"]\s*[,)]")
    profile_reference = re.compile(r"\b(?:profileText|apiCredentialText|profileSettingsText)\(\s*['\"]([a-z][a-z0-9_.]*)['\"]\s*[,)]")
    chat_reference = re.compile(r"\bchat_text\(\s*\w+\s*,\s*['\"]([a-z][a-z0-9_]*)['\"]\s*[,)]")
    for path in consumers:
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".js" and "AurvekI18n" not in content:
            continue  # Third-party bundles do not use the Aurvek catalog API.
        references = [(match, match.group(1)) for match in reference.finditer(content)]
        local_t_domains = {
            "settings.js": "profile", "fullsize-viewer.js": "chat_ui",
            "nekoglass-controls.js": "navigation",
            "embed-adapter.js": "embed",
        }
        if path.name in local_t_domains:
            references = [(match, local_t_domains[path.name] + "." + key if content[match.start() - 1:match.start()] != "." else key)
                          for match, key in references]
        references += [(match, "profile." + match.group(1)) for match in profile_reference.finditer(content)]
        references += [(match, "chat_errors." + match.group(1)) for match in chat_reference.finditer(content)]
        aliases = {
            "categoryText": "admin_categories", "modelText": "admin_models",
            "serviceText": "admin_services", "voiceText": "admin_voices",
            "subscriptionText": "admin_subscription", "rankingText": "admin_ranking", "geoText": "admin_geo",
            "peT": "prompt_editor", "folderText": "chat_widgets.folders", "aiText": "ai_config",
            "accountSettingsText": "account", "commonSettingsText": "common",
        }
        if path.name in {"home.js", "explore.js"}:
            aliases.update(mt="marketplace", mh="marketplace")
        if path.name == "application-panel.js":
            aliases["label"] = "application"
        for alias, prefix in aliases.items():
            references += [(match, prefix + "." + match.group(1)) for match in re.finditer(
                rf"\b{alias}\(\s*['\"]([a-z][a-z0-9_.]*)['\"]\s*[,)]", content)]
        references += [(match, match.group(1)) for match in re.finditer(
            r"\bdata-i18n-[a-z-]+=['\"]([a-z][a-z0-9_.]*)['\"]", content)]
        if path.name == "llm_catalog.py":
            references += [(match, "admin_models." + match.group(1)) for match in re.finditer(
                r"\bmessage_key\s*=\s*['\"]([a-z][a-z0-9_.]*)['\"]", content)]
        widget_prefixes = {
            "voice-call.js": "voice_call", "phone-call.js": "phone",
            "phone-history.js": "phone_history", "voice-note-retranscription.js": "retranscription",
        }
        if path.name in widget_prefixes:
            references += [(match, "chat_widgets." + widget_prefixes[path.name] + "." + match.group(1))
                           for match in re.finditer(r"\btr\(\s*['\"]([a-z][a-z0-9_.]*)['\"]\s*[,)]", content)]
        for match, key in references:
            domain, _, name = key.partition(".")
            if name not in english.get(domain, {}):
                line = content[:match.start()].count("\n") + 1
                problems.append(f"Unknown key: {path.relative_to(ROOT)}:{line}: {key}")
    phone_source = (ROOT / "phone_verification.py").read_text(encoding="utf-8")
    phone_codes = set(re.findall(r'^\s*code = "([a-z_]+)"', phone_source, re.MULTILINE))
    deletion_source = (ROOT / "user_deletion.py").read_text(encoding="utf-8")
    deletion_codes = set(re.findall(
        r'result\.status = "blocked"\s+result\.reason_codes = \["([a-z_]+)"\]', deletion_source))
    deletion_codes.update(re.findall(r'reasons\.append\("([a-z_]+)"\)', deletion_source))
    for prefix, codes in (("phone", phone_codes), ("deletion", deletion_codes),
                          ("memory", {"health_suspected", "health_degraded", "health_unavailable"}),
                          ("credentials", {"mode_system_only", "mode_own_only", "mode_both_prefer_own", "mode_both_prefer_system"})):
        for code in sorted(codes):
            if f"{prefix}.{code}" not in english.get("account", {}):
                problems.append(f"Missing dynamic code translation: account.{prefix}.{code}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", nargs="+", default=list(LANGUAGES), choices=list(LANGUAGES))
    args = parser.parse_args()
    try:
        problems = check_catalogs(args.languages)
    except ValueError as exc:
        problems = [str(exc)]
    for problem in problems:
        print(problem.encode("ascii", errors="backslashreplace").decode("ascii"))
    if not problems:
        print("Catalogs and migrated translation references are valid.")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
