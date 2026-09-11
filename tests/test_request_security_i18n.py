"""Localized mutation denials retain the existing token and origin decisions."""
import json
import pytest
from starlette.requests import Request
from i18n import LANGUAGES, Translator
import request_security as security


@pytest.mark.parametrize('language', LANGUAGES)
def test_mutation_rejections_follow_request_locale_and_preserve_decisions(monkeypatch, language):
    monkeypatch.setattr(security, 'PRIMARY_APP_DOMAIN', '')
    tr = Translator(language)
    scope = {'type': 'http', 'method': 'POST', 'scheme': 'https', 'path': '/',
             'server': ('fixture.test', 443), 'session': {security.CSRF_SESSION_KEY: 'fixture-token'},
             'state': {'i18n': tr}, 'headers': [(b'host', b'fixture.test'), (b'origin', b'https://fixture.test')]}
    response = security.validate_mutation_request(Request(scope))
    assert response.status_code == 403 and json.loads(response.body) == {'status': 'forbidden', 'message': tr.t('common.security.csrf_invalid')}
    scope['headers'] += [(b'x-gptsub-csrf', b'fixture-token'), (b'sec-fetch-site', b'cross-site')]
    response = security.validate_mutation_request(Request(scope))
    assert response.status_code == 403 and json.loads(response.body)['message'] == tr.t('common.security.cross_site')
    scope['headers'] = [(key,value) for key,value in scope['headers'] if key not in (b'origin',b'sec-fetch-site')]
    scope['headers'].append((b'origin', b'https://different.test'))
    response = security.validate_mutation_request(Request(scope))
    assert response.status_code == 403 and json.loads(response.body)['message'] == tr.t('common.security.origin_mismatch')
    scope['headers'][-1] = (b'origin', b'https://fixture.test')
    assert security.validate_mutation_request(Request(scope)) is None
