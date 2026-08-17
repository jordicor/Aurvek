from starlette.requests import Request

from integrations.elevenlabs.sdk_routes import SDK_CACHE_CONTROL, _sdk_response


def _request(if_none_match: str = "") -> Request:
    headers = []
    if if_none_match:
        headers.append((b"if-none-match", if_none_match.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/sdk/elevenlabs-client.js",
            "raw_path": b"/sdk/elevenlabs-client.js",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        }
    )


def test_sdk_response_sets_cache_headers_and_strong_etag():
    response = _sdk_response(
        _request(),
        b"console.log('sdk');",
        "application/javascript",
    )

    assert response.status_code == 200
    assert response.body == b"console.log('sdk');"
    assert response.headers["cache-control"] == SDK_CACHE_CONTROL
    assert response.headers["etag"].startswith('"')
    assert response.headers["etag"].endswith('"')


def test_sdk_response_honours_if_none_match_without_body():
    initial = _sdk_response(_request(), b"sdk", "application/javascript")
    etag = initial.headers["etag"]

    response = _sdk_response(
        _request(f'"unrelated", W/{etag}'),
        b"sdk",
        "application/javascript",
    )

    assert response.status_code == 304
    assert response.body == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == SDK_CACHE_CONTROL
