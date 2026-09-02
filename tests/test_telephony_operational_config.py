from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_nginx_routes_voice_callbacks_and_preserves_private_audio_gets():
    config = _read("nginx/aurvek-main.conf")

    assert "location = /webhooks/twilio/voice/inbound" in config
    assert "location = /webhooks/twilio/voice/inbound-status" in config
    assert config.count("limit_except POST { deny all; }") == 4
    assert "(twiml|status|stream-status|amd|recording)" in config
    assert "connect-action/[^/]+/[0-9]+" in config
    assert "(private-audio|call-audio)" in config
    assert "limit_except GET { deny all; }" in config


def test_nginx_has_exact_long_lived_twilio_websocket_before_generic_ws():
    config = _read("nginx/aurvek-main.conf")
    exact = config.index("location = /ws/twilio/media-stream")
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
