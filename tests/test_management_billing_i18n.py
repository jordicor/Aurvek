"""Billing presentation contracts using fixture data only."""
import csv
import io
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from i18n import LANGUAGES, Translator


class User:
    id = 7
    ui_language = 'es'

    @property
    async def is_admin(self):
        return True

    @property
    async def is_user(self):
        return True


def request(data=None, query=b''):
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(data).encode()}
    return Request({'type': 'http', 'method': 'PUT', 'path': '/', 'headers': [], 'query_string': query}, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['NaN', 'Infinity', None, True, {}, -1, 101])
async def test_pricing_rejects_bad_values_before_any_write(monkeypatch, value):
    import app

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid pricing must not reach the database')
    monkeypatch.setattr(app, 'get_db_connection', forbidden)
    response = await app.update_pricing_config(request({'pricing_margin_paid': 12, 'pricing_commission': value}), User())
    assert response.status_code == 400
    assert json.loads(response.body)['success'] is False
    assert 'pricing_commission' in json.loads(response.body)['message']


@pytest.mark.asyncio
async def test_pricing_preserves_value_and_returns_localized_success(monkeypatch):
    import app
    conn = AsyncMock()
    cursor = AsyncMock()
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def database(**kwargs):
        yield conn
    monkeypatch.setattr(app, 'get_db_connection', database)
    response = await app.update_pricing_config(request({'pricing_commission': '12.345'}), User())
    assert json.loads(response.body)['message'] == Translator('es').render('management_billing_errors.pricing.updated')
    assert cursor.execute.call_args.args[1] == ('12.345', 'pricing_commission')
    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_team_csv_reuses_queries_preserves_numbers_and_escapes_formulas(monkeypatch, language):
    import app
    user = User()
    user.ui_language = language
    cursor = AsyncMock()
    conn = AsyncMock()
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def database(**kwargs):
        assert kwargs == {'readonly': True}
        yield conn
    monkeypatch.setattr(app, 'get_db_connection', database)

    def prepare():
        cursor.reset_mock()
        cursor.fetchone.side_effect = [(10.125,), (1.23,)]
        cursor.fetchall.side_effect = [[(4, '=SUM(1,2)', '\t@bad', 20, 'block', 2.123456)], [('Original prompt', 1, 2, 1000)], [('=SUM(1,2)', 'Original prompt', 1000, '2026-09-01')]]

    prepare()
    normal = await app.get_user_team_billing(request(), user)
    payload = json.loads(normal.body)
    query_count = cursor.execute.await_count
    assert payload['users'][0]['status'] == 'Active'
    assert payload['by_prompt'][0]['cost'] == 0.015
    prepare()
    exported = await app.get_user_team_billing(request(query=b'format=csv'), user)
    assert cursor.execute.await_count == query_count == 5
    assert exported.headers['content-type'].startswith('text/csv')
    rows = list(csv.reader(io.StringIO(exported.body.decode())))
    assert rows[0][0] == Translator(language).render('management_billing_errors.csv.username')
    assert rows[1] == ["'=SUM(1,2)", "'\t@bad", '2.123456', '20.0', Translator(language).render('management_billing_errors.status.active'), 'USD']


@pytest.mark.asyncio
async def test_team_failure_is_safe_and_localized(monkeypatch):
    import app

    def fail(**kwargs):
        raise RuntimeError('secret-database-details')
    monkeypatch.setattr(app, 'get_db_connection', fail)
    response = await app.get_user_team_billing(request(), User())
    assert response.status_code == 500
    assert json.loads(response.body) == {'error': Translator('es').render('management_billing_errors.team.failed')}


@pytest.mark.asyncio
async def test_storage_validation_keeps_machine_fields(monkeypatch):
    import app
    response = await app.update_storage_quota_user(9, request({'quota_bytes': -1}), User())
    assert response.status_code == 400
    assert json.loads(response.body) == {'success': False, 'message': Translator('es').render('management_billing_errors.storage.quota_invalid')}


@pytest.mark.asyncio
async def test_admin_csv_keeps_technical_type_and_raw_amount(monkeypatch):
    import app
    conn = AsyncMock()
    cursor = AsyncMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = [('2026-09-01', ' =bad', 'llm', 2, 31, 40, 0.125, 0.000012345)]

    @asynccontextmanager
    async def database(**kwargs):
        yield conn
    monkeypatch.setattr(app, 'get_db_connection', database)
    response = await app.export_admin_usage_csv(request(), days=7, type='llm', current_user=User())
    rows = list(csv.reader(io.StringIO(response.body.decode())))
    assert rows[0][-1] == 'Coste (USD)'
    assert response.headers['content-disposition'] == "attachment; filename*=UTF-8''exportacion_uso_7dias.csv"
    assert rows[1] == ['2026-09-01', "' =bad", 'llm', '2', '31', '40', '0.125', '1.2345e-05']
    assert conn.execute.call_args.args[1] == ['-7 days', 'llm']
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_denial_uses_loaded_identity_without_database(monkeypatch):
    import app

    class NonAdmin(User):
        ui_language = 'ja'

        @property
        async def is_admin(self):
            return False

    def forbidden(*args, **kwargs):
        pytest.fail('Permission denial must not query the database')
    monkeypatch.setattr(app, 'get_db_connection', forbidden)
    response = await app.get_storage_quota_config(NonAdmin())
    assert response.status_code == 403
    assert json.loads(response.body)['message'] == Translator('ja').render('management_billing_errors.admin_required')
