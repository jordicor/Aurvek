from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_nginx_routes_voice_callbacks_and_preserves_private_audio_gets():
    config = _read("nginx/aurvek-main.conf")

    assert "location = /webhooks/twilio/voice/inbound" in config
    assert "location = /webhooks/twilio/voice/inbound-status" in config
    voice_callbacks = re.findall(
        r"^    location ([^\n]+) \{\n(.*?)^    \}", config, re.M | re.S
    )
    voice_posts = [body for route, body in voice_callbacks
                   if "/webhooks/twilio/voice/" in route and "private-audio" not in route]
    assert len(voice_posts) == 4
    assert all("limit_except POST { deny all; }" in body for body in voice_posts)
    assert "(twiml|status|stream-status|amd|recording)" in config
    assert "connect-action/[^/]+/[0-9]+" in config
    assert "(private-audio|private-inbound-unavailable-audio|call-audio)" in config
    assert "limit_except GET { deny all; }" in config


def test_nginx_has_scoped_long_lived_twilio_websocket_before_generic_ws():
    config = _read("nginx/aurvek-main.conf")
    exact = config.index('location ~ "^/ws/twilio/media-stream(/[A-Za-z0-9_-]{32,128})?$"')
    generic = config.index("location /ws {")
    twilio_block = config[exact:generic]

    assert exact < generic
    assert "proxy_http_version 1.1;" in twilio_block
    assert "proxy_set_header Upgrade $http_upgrade;" in twilio_block
    assert 'proxy_set_header Connection "upgrade";' in twilio_block
    assert "proxy_send_timeout 18000s;" in twilio_block
    assert "proxy_read_timeout 18000s;" in twilio_block
    assert "proxy_buffering off;" in twilio_block
    assert "proxy_request_buffering off;" in twilio_block
    assert "proxy_read_timeout 300s;" in config[generic:]


def test_public_environment_contract_reuses_existing_twilio_credentials():
    example = _read(".env.example")

    assert "PRIMARY_APP_DOMAIN=" in example
    assert "WhatsApp, SMS and Voice" in example
    assert "TWILIO_SID=" in example
    assert "TWILIO_AUTH=" in example
    assert "TWILIO_VOICE_AUTH" not in example
    assert "TWILIO_VOICE_TOKEN" not in example


def test_application_management_ui_reaches_backend_without_opening_other_app_paths():
    config = _read("nginx/aurvek-main.conf")
    rule = next(line.strip() for line in config.splitlines()
                if line.strip().startswith('location ~ ') and 'applications/manage' in line)
    pattern = re.compile(rule.removeprefix('location ~ ').removesuffix(' {'))
    for path in ('/applications/manage', '/applications/manage/katari/twilio', '/admin/telephony'):
        assert pattern.match(path)
    for path in ('/applications/manage-other', '/applications/private', '/api/applications/v1/accounts/session'):
        assert not pattern.match(path)
    start = config.index(rule)
    assert 'include {{SNIPPETS_PATH}}/fastapi-proxy.conf;' in config[start:config.index('\n    }', start)]
    assert 'location ^~ /api/applications/v1/ { return 404; }' in config
