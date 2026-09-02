"""Focused regressions for messaging voice-note retention admin switches."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("platform", "route_path", "template_path"),
    (
        (
            "whatsapp",
            "integrations/whatsapp/admin_routes.py",
            "templates/admin_whatsapp.html",
        ),
        (
            "telegram",
            "integrations/telegram/admin_routes.py",
            "templates/admin_telegram.html",
        ),
    ),
)
def test_voice_note_retention_admin_switch_is_explicitly_opt_in(
    platform: str,
    route_path: str,
    template_path: str,
) -> None:
    route = (ROOT / route_path).read_text(encoding="utf-8")
    template = (ROOT / template_path).read_text(encoding="utf-8")
    key = f"{platform}_retain_voice_notes"

    assert "retain_voice_notes = False" in route
    assert f'form.get("{key}") == "1"' in route
    assert f"VALUES ('{key}', ?" in route
    assert f'id="{key}" name="{key}" value="1"' in template
    assert f"{{% if {key} %}}checked{{% endif %}}" in template
    assert "Disabled by default for privacy." in template
    assert "only to voice notes received after saving" in template
    assert "ensure_csrf_token" in route
    assert "validate_mutation_request" in route
    assert 'supplied_token=form.get("csrf_token")' in route
    assert template.count('name="csrf_token"') == 2
