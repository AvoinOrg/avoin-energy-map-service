"""Provider fixtures only: no identity service, credentials or database writes."""
import asyncio
from base64 import b64decode
from dataclasses import FrozenInstanceError, asdict, replace
import json
import logging
import os
import ssl
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote_plus

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.api.auth import (
    AuthenticatedPrincipal, AuthConfigurationError, HTTP_TIMEOUT,
    MAX_AUTHORIZATION_BYTES, MAX_RESPONSE_BYTES, OVERALL_TIMEOUT_SECONDS,
    ZitadelIntrospector, ZitadelSettings, get_auth_settings, get_introspector, require_principal,
)

ISSUER = 'https://identity.example.invalid'
AUDIENCE = 'fixture-energy-api'
TOKEN = 'CANARY_ACCESS_TOKEN'
SECRET = 'CANARY_CLIENT_SECRET'
PROVIDER_DETAIL = 'CANARY_PROVIDER_DETAIL'
ENV = {
    'ENERGY_MAP_ZITADEL_ISSUER': ISSUER,
    'ENERGY_MAP_ZITADEL_AUDIENCES': json.dumps([AUDIENCE]),
    'ENERGY_MAP_ZITADEL_CLIENT_ID': 'fixture-api-client',
    'ENERGY_MAP_ZITADEL_CLIENT_SECRET': SECRET,
}
ERRORS = {
    401: {'code': 'invalid_access_token', 'message': 'A valid Bearer access token is required'},
    403: {'code': 'insufficient_permission', 'message': 'Required permission is missing'},
    503: {'code': 'authentication_unavailable', 'message': 'Authentication is temporarily unavailable'},
}


def settings(**kwargs):
    return ZitadelSettings(issuer=kwargs.pop('issuer', ISSUER), audiences=frozenset([AUDIENCE]),
                          client_id='fixture-api-client', client_secret=SECRET, **kwargs)


def claims(**kwargs):
    return dict(active=True, exp=2000, iss=ISSUER, sub='fixture-subject',
                aud=AUDIENCE, token_type='Bearer', **kwargs)


class TrackingTransport(httpx.MockTransport):
    closes = 0

    async def aclose(self):
        self.closes += 1
        await super().aclose()


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.requests = []
        self.body = claims()
        self.reply = lambda request: httpx.Response(200, json=self.body)

        async def handle(request):
            self.requests.append(request)
            result = self.reply(request)
            return await result if asyncio.iscoroutine(result) else result

        self.transport = TrackingTransport(handle)
        self.introspector = ZitadelIntrospector(settings(), transport=self.transport, clock=lambda: self.now)
        self.app = FastAPI()
        self.executions = 0

        @self.app.get('/protected')
        async def protected(principal: AuthenticatedPrincipal = Depends(require_principal)):
            self.executions += 1
            return asdict(principal)

        self.app.dependency_overrides[get_introspector] = lambda: self.introspector
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.logs = CaptureLogs()
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        self.addCleanup(root_logger.setLevel, previous_level)
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def request(self, token=TOKEN, **kwargs):
        return self.client.get('/protected', headers={'Authorization': f'Bearer {token}'}, **kwargs)

    def assert_error(self, status, response=None):
        before = self.executions
        response = self.request() if response is None else response
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json(), {'detail': ERRORS[status]})
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers.get('www-authenticate'), 'Bearer' if status == 401 else None)
        self.assertEqual(self.executions, before)
        for canary in (TOKEN, SECRET, PROVIDER_DETAIL):
            self.assertNotIn(canary, response.text)
            self.assertNotIn(canary, '\n'.join(self.logs.messages))
        return response

    def test_opaque_and_jwt_shaped_tokens_use_authenticated_introspection(self):
        for token in (TOKEN, 'eyJhbGciOiJub25lIn0.eyJzdWIiOiJmb3JnZWQifQ.signature'):
            response = self.request(token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'issuer': ISSUER, 'subject': 'fixture-subject'})
            self.assertEqual(response.headers['cache-control'], 'no-store')
            request = self.requests[-1]
            self.assertEqual(str(request.url), ISSUER + '/oauth/v2/introspect')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(parse_qs(request.content.decode()), {'token': [token], 'token_type_hint': ['access_token']})
            self.assertEqual(b64decode(request.headers['authorization'].split(' ')[1]),
                             b'fixture-api-client:CANARY_CLIENT_SECRET')
            self.assertEqual(request.headers['content-type'], 'application/x-www-form-urlencoded')
            self.assertEqual(request.headers['accept-encoding'], 'identity')
            self.assertEqual(request.extensions['timeout'], {'connect': 1., 'pool': 1., 'write': 1., 'read': 3.})
        self.assertEqual(self.transport.closes, 2)
        for canary in (TOKEN, SECRET):
            self.assertNotIn(canary, '\n'.join(self.logs.messages))

    def test_oauth_basic_credentials_are_form_encoded_and_secret_repr_is_hidden(self):
        self.introspector = replace(self.introspector, settings=replace(settings(), client_id='client:id+', client_secret=SECRET + '+:/='))
        self.assertEqual(self.request().status_code, 200)
        value = b64decode(self.requests[-1].headers['authorization'].split(' ')[1]).decode()
        self.assertEqual(value, quote_plus('client:id+') + ':' + quote_plus(SECRET + '+:/='))
        self.assertNotIn(SECRET, repr(self.introspector))
        self.assertNotIn(SECRET, repr(self.introspector.settings))

    def test_http_client_transport_policy_and_cleanup(self):
        original = httpx.AsyncClient
        with patch('src.api.auth.httpx.AsyncClient', wraps=original) as factory:
            self.assertEqual(self.request().status_code, 200)
        config = factory.call_args.kwargs
        self.assertIs(config['trust_env'], False)
        self.assertIs(config['follow_redirects'], False)
        self.assertIs(config['verify'], True)
        self.assertEqual(config['timeout'], HTTP_TIMEOUT)
        self.assertEqual(self.introspector.deadline_seconds, OVERALL_TIMEOUT_SECONDS)
        self.assertEqual(self.transport.closes, 1)

    def test_real_lazy_settings_dependency_uses_server_credentials(self):
        self.app.dependency_overrides.clear()
        self.body = claims() | {'exp': 2**40}
        original_client = httpx.AsyncClient

        def fixture_client(**kwargs):
            kwargs['transport'] = self.transport
            return original_client(**kwargs)

        with patch.dict(os.environ, ENV, clear=True), \
             patch('src.api.auth.httpx.AsyncClient', side_effect=fixture_client):
            response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'issuer': ISSUER, 'subject': 'fixture-subject'})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.transport.closes, 1)

    def test_unsupported_issuer_url_returns_sanitized_unavailable(self):
        self.app.dependency_overrides.clear()
        original_client = httpx.AsyncClient

        def fixture_client(**kwargs):
            kwargs['transport'] = self.transport
            return original_client(**kwargs)

        # urllib accepts this IPvFuture origin, but HTTPX cannot construct its
        # request URL. Exercise real settings/dependencies without any network.
        with patch.dict(os.environ, ENV | {'ENERGY_MAP_ZITADEL_ISSUER': 'https://[v1.identity]'}, clear=True), \
             patch('src.api.auth.httpx.AsyncClient', side_effect=fixture_client), \
             TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get('/protected', headers={'Authorization': f'Bearer {TOKEN}'})

        self.assert_error(503, response)
        self.assertEqual(self.executions, 0)
        self.assertFalse(self.requests)

    def test_same_subject_relogin_and_distinct_issuer_and_subject(self):
        first = self.request('first-login').json()
        self.body.update(email='new@example.invalid', name='Changed', session_id='new', owner='forged')
        self.assertEqual(first, self.request('second-login').json())
        principal = AuthenticatedPrincipal(**first)
        self.assertEqual(principal, AuthenticatedPrincipal(ISSUER, 'fixture-subject'))
        self.body['sub'] = 'second-subject'
        self.assertNotEqual(principal, AuthenticatedPrincipal(**self.request().json()))
        second_issuer = 'https://second.example.invalid'
        self.body.update(sub='fixture-subject', iss=second_issuer)
        self.introspector = replace(self.introspector, settings=settings(issuer=second_issuer))
        self.assertNotEqual(principal, AuthenticatedPrincipal(**self.request().json()))
        self.assertEqual(set(asdict(principal)), {'issuer', 'subject'})
        self.assertNotIn(TOKEN, repr(principal))
        with self.assertRaises(FrozenInstanceError):
            principal.subject = 'forged'

    def test_subject_is_preserved_exactly(self):
        self.body['sub'] = ' subject-with-meaningful-space '
        self.assertEqual(self.request().json()['subject'], self.body['sub'])

    def test_malformed_headers_fail_before_settings_or_provider(self):
        self.app.dependency_overrides[get_introspector] = lambda: self.fail('settings accessed')
        headers = [[], [('Authorization', '')], [('Authorization', 'Bearer')],
                   [('Authorization', 'Basic abc')], [('Authorization', 'Bearer ')],
                   [('Authorization', 'Bearer  abc')], [('Authorization', 'Bearer abc def')],
                   [('Authorization', 'Bearer abc\tdef')], [('Authorization', 'Bearer abc\n')],
                   [('Authorization', 'Bearer abc\x00def')], [('Authorization', ' Bearer abc')],
                   [('Authorization', 'Bearer a,b')], [(b'Authorization', b'Bearer \xe9')],
                   [('Authorization', 'Bearer =')], [('Authorization', 'Bearer a=b')],
                   [('Authorization', 'Bearer ' + 'a' * MAX_AUTHORIZATION_BYTES)],
                   [('Authorization', 'Bearer abc'), ('authorization', 'Bearer abc')]]
        for value in headers:
            with self.subTest(headers=value):
                self.assert_error(401, self.client.get('/protected', headers=value))
        self.assertFalse(self.requests)

    def test_case_insensitive_scheme_and_header_bound(self):
        response = self.client.get('/protected', headers={'authorization': 'bEaReR abc+/~_-==.'})
        # '=' is trailing only; a dot after padding is malformed.
        self.assert_error(401, response)
        for value in ('bEaReR abc+/~_-==', 'Bearer ' + 'a' * (MAX_AUTHORIZATION_BYTES - 7)):
            self.assertEqual(self.client.get('/protected', headers={'authorization': value}).status_code, 200)

    def test_cookies_query_and_owner_headers_do_not_authenticate(self):
        response = self.client.get('/protected?access_token=forged&owner=forged',
                                   headers={'Cookie': 'session=forged', 'X-Owner': 'forged',
                                            'X-User-Id': 'forged', 'X-Email': 'forged'})
        self.assert_error(401, response)
        self.assertFalse(self.requests)
        self.assertEqual(self.request(params={'owner': 'forged', 'sub': 'forged'}).json()['subject'], 'fixture-subject')

    def test_invalid_credentials_are_challenged(self):
        for update in ({'active': False}, {'exp': 999}, {'exp': 1000}, {'iss': ISSUER + '/'},
                       {'iss': 'https://foreign.example.invalid'}, {'iss': ''}, {'sub': ''}, {'sub': ' \t'},
                       {'aud': AUDIENCE + '-suffix'}, {'aud': 'prefix-' + AUDIENCE},
                       {'aud': ['different']}, {'token_type': 'id_token'}, {'token_type': ''}, {'nbf': 1001}):
            with self.subTest(update=update):
                self.body = claims() | update
                self.assert_error(401)
        self.body = {'active': False}
        self.assert_error(401)  # inactive needs no other claims

    def test_access_token_provider_rejects_id_token_shaped_input(self):
        def provider(request):
            token = parse_qs(request.content.decode())['token'][0]
            return httpx.Response(200, json={'active': False} if token.startswith('id.') else claims())
        self.reply = provider
        self.assert_error(401, self.request('id.payload.signature'))
        self.assertEqual(len(self.requests), 1)

    def test_audience_forms_exact_membership_and_optional_timing(self):
        for aud in (AUDIENCE, [AUDIENCE], ['other', AUDIENCE]):
            self.body = claims() | {'aud': aud, 'nbf': 1000, 'iat': 99999, 'exp': 1000.5, 'token_type': 'bearer'}
            self.assertEqual(self.request().status_code, 200)
        # iat is finite-number shape checked; future iat has no extra rejection policy.

    def test_malformed_provider_claims_fail_as_unavailable(self):
        mutations = [
            {'active': 'true'}, {'active': 1}, {'active': None},
            {'exp': True}, {'exp': '2000'}, {'exp': None}, {'exp': float('inf')}, {'exp': float('nan')},
            {'iss': 1}, {'sub': None}, {'aud': None}, {'aud': []}, {'aud': ''}, {'aud': ['']},
            {'aud': ['other', 1]}, {'aud': ['other', None]}, {'aud': [AUDIENCE, ' ']},
            {'token_type': False}, {'nbf': '1'}, {'nbf': True}, {'nbf': None}, {'nbf': float('inf')},
            {'iat': None}, {'iat': '1000'}, {'iat': float('nan')},
        ]
        for update in mutations:
            with self.subTest(update=update):
                # json.dumps deliberately emits nonfinite numbers to test the parser boundary.
                self.reply = lambda request: httpx.Response(200, content=json.dumps(claims() | update))
                self.assert_error(503)
        for missing in ('active', 'exp', 'iss', 'sub', 'aud', 'token_type'):
            self.body = claims()
            del self.body[missing]
            self.reply = lambda request: httpx.Response(200, json=self.body)
            with self.subTest(missing=missing):
                self.assert_error(503)

    def test_malformed_json_envelopes_and_bounded_body(self):
        for body in (b'not-json', b'{"active":false,"active":true}', b'[]', b'null', b'"text"', b'123', b'\xff',
                     b'[' * 2000 + b']' * 2000, b'x' * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(body_length=len(body)):
                self.reply = lambda request: httpx.Response(200, content=body)
                self.assert_error(503)
        self.reply = lambda request: httpx.Response(200, json=claims(), headers={'Content-Encoding': 'unsupported'})
        self.assert_error(503)

    def test_bounded_stream_is_closed_before_reading_remaining_chunks(self):
        consumed = []
        closed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(10):
                    consumed.append(index)
                    yield b'x' * (MAX_RESPONSE_BYTES // 2)

            async def aclose(self):
                closed.append(True)

        self.reply = lambda request: httpx.Response(200, stream=Stream())
        self.assert_error(503)
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(closed, [True])
        self.assertEqual(self.transport.closes, 1)

    def test_provider_http_errors_no_redirects_or_retry(self):
        for status in (201, 204, 301, 302, 307, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                before = len(self.requests)
                self.reply = lambda request: httpx.Response(status, text=PROVIDER_DETAIL,
                                                           headers={'location': 'https://foreign.example.invalid'})
                self.assert_error(503)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.transport.closes, len(self.requests))

    def test_network_tls_and_phase_timeouts_are_sanitized(self):
        for error in (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout,
                      httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError):
            def fail(request):
                raise error(PROVIDER_DETAIL + SECRET + TOKEN, request=request) from ssl.SSLError(PROVIDER_DETAIL)
            self.reply = fail
            with self.subTest(error=error):
                self.assert_error(503)
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(self.transport.closes, 7)

    def test_overall_deadline_cancels_wait_and_closes_client(self):
        cancelled = []

        async def waiting(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        self.reply = waiting
        self.introspector = replace(self.introspector, deadline_seconds=.01)
        self.assert_error(503)
        self.assertEqual(cancelled, [True])
        self.assertEqual(self.transport.closes, 1)

    def test_overall_deadline_includes_response_consumption(self):
        closed = []

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{'
                await asyncio.Event().wait()

            async def aclose(self):
                closed.append(True)

        self.reply = lambda request: httpx.Response(200, stream=WaitingStream())
        self.introspector = replace(self.introspector, deadline_seconds=.01)
        self.assert_error(503)
        self.assertEqual(closed, [True])
        self.assertEqual(self.transport.closes, 1)

    def test_no_cache_revocation_outage_and_completion_expiry(self):
        self.assertEqual(self.request().status_code, 200)
        self.body = {'active': False}
        self.assert_error(401)
        self.body = claims()
        self.assertEqual(self.request().status_code, 200)
        self.reply = lambda request: httpx.Response(503, text=PROVIDER_DETAIL)
        self.assert_error(503)
        self.assertEqual(len(self.requests), 4)

        def expire_during_request(request):
            self.now = 2000
            return httpx.Response(200, json=claims())
        self.reply = expire_during_request
        self.assert_error(401)

    def test_default_policy_allows_ordinary_users_ignores_roles(self):
        for extra in ({}, {'scope': None}, {'roles': [], 'admin': False}, {'scope': 'unrelated'}):
            self.body = claims() | extra
            self.assertEqual(self.request().status_code, 200)

    def test_all_of_optional_scope_policy_and_token_validity_precedence(self):
        self.introspector = replace(self.introspector, settings=settings(required_scopes=frozenset(['building:write', 'building:read'])))
        for grant in ('building:read building:write', 'other building:write building:read'):
            self.body = claims(scope=grant)
            self.assertEqual(self.request().status_code, 200)
        for grant in ('', 'building:read', 'building:write-suffix', 'admin'):
            self.body = claims(scope=grant)
            self.assert_error(403)
        self.body = claims()
        self.assert_error(403)
        for grant in (None, [], 1, 'building:read\tbuilding:write', 'bad"scope', ' x', 'x  y'):
            self.body = claims(scope=grant)
            self.assert_error(503)
        self.body = claims(scope=None) | {'exp': 1000}
        self.assert_error(401)

    def test_missing_and_malformed_configuration_is_lazy_unavailable(self):
        self.app.dependency_overrides.clear()
        bad = [{}, {key: value for key, value in ENV.items() if key != 'ENERGY_MAP_ZITADEL_CLIENT_SECRET'}]
        for key, values in {
            'ENERGY_MAP_ZITADEL_ISSUER': ['', 'http://identity.example.invalid', ISSUER + '/', ISSUER + '/path',
                                          'https://user:pass@example.invalid', ISSUER + '?x=1', ISSUER + '#x',
                                          'https://identity.example.invalid:99999', 'https://bad\\host', 'https://bad host'],
            'ENERGY_MAP_ZITADEL_AUDIENCES': ['', '[]', '"aud"', '[1]', '{}', '["*"]', '[""]', '[" "]', '["a"]' * 5000],
            'ENERGY_MAP_ZITADEL_CLIENT_ID': ['', 'x\ny'],
            'ENERGY_MAP_ZITADEL_CLIENT_SECRET': ['', 'x' * 4097],
            'ENERGY_MAP_ZITADEL_REQUIRED_SCOPES': ['a\tb', 'admin  user', 'a"b', 'a' * 4097],
        }.items():
            bad.extend(ENV | {key: value} for value in values)
        for environ in bad:
            with self.subTest(keys=list(environ)), patch.dict(os.environ, environ, clear=True):
                self.assert_error(503)
                self.assert_error(401, self.client.get('/protected'))
        self.assertFalse(self.requests)

    def test_settings_loader_and_direct_constructor_validation(self):
        with patch.dict(os.environ, ENV | {'ENERGY_MAP_ZITADEL_REQUIRED_SCOPES': 'a b'}, clear=True):
            value = get_auth_settings()
        self.assertEqual(value.required_scopes, frozenset(['a', 'b']))
        self.assertEqual(value.audiences, frozenset([AUDIENCE]))
        self.assertNotIn(SECRET, repr(value))
        for update in ({'audiences': frozenset()}, {'audiences': frozenset(['a'] * 1 + ['x' * 513])},
                       {'required_scopes': frozenset(['bad scope'])}, {'issuer': ISSUER + ':0'}):
            with self.assertRaises(AuthConfigurationError) as error:
                replace(settings(), **update)
            self.assertNotIn(SECRET, str(error.exception))

    def test_openapi_is_http_bearer_without_login_flow(self):
        schema = self.app.openapi()
        self.assertEqual(schema['components']['securitySchemes']['ZitadelAccessToken']['type'], 'http')
        self.assertEqual(schema['components']['securitySchemes']['ZitadelAccessToken']['scheme'], 'bearer')
        self.assertEqual(schema['paths']['/protected']['get']['security'], [{'ZitadelAccessToken': []}])
        self.assertNotIn('tokenUrl', json.dumps(schema))


class PublicIsolationTests(unittest.TestCase):
    def test_import_and_openapi_with_unconfigured_or_broken_auth(self):
        script = """
from unittest.mock import patch
from src.api.main import app
with patch('src.api.auth.get_auth_settings', side_effect=AssertionError('public auth access')), \\
     patch('src.api.auth.httpx.AsyncClient', side_effect=AssertionError('public provider access')):
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get('/healthz', headers={'Authorization':'Bearer invalid'}).status_code == 200
        schema = client.get('/openapi.json').json()
        assert not schema.get('security')
        assert schema['components']['securitySchemes']['ZitadelAccessToken']['scheme'] == 'bearer'
        for path, operations in schema['paths'].items():
            assert 'protected' not in path
            for operation in operations.values():
                assert bool(operation.get('security')) == ('/user/buildings' in path)
"""
        for config in ({}, ENV | {'ENERGY_MAP_ZITADEL_ISSUER': 'broken'}):
            environ = {k: v for k, v in os.environ.items() if not k.startswith('ENERGY_MAP_ZITADEL_')}
            with self.subTest(configured=bool(config)):
                result = subprocess.run([sys.executable, '-c', script], env=environ | config, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_building_health_ready_area_and_tiles_never_introspect(self):
        from src.api.main import app
        from src.api.buildings import building_session, public_example
        from src.api.areas import area_session
        from src.building_contract import CanonicalBuildingV1
        from src.building_resolver import Resolution
        from unittest.mock import MagicMock
        fixture = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        session = MagicMock()
        session.__enter__.return_value = session
        session.execute.return_value.scalar_one.return_value = True
        session.execute.return_value.scalar.return_value = b'building-tile'
        session.execute.return_value.mappings.return_value.first.return_value = {
            'name_fi': 'fixture', 'name_sv': None, 'name_en': None, 'region_code': '01',
            'building_count': 0, 'valid_official_class_count': 0, 'certificate_coverage': None, 'metrics': {}}
        release = {'release_id': '00000000-0000-0000-0000-000000000001', 'boundary_set_id': 'fixture',
                   'vintage': 'fixture', 'as_of': '2026-10-23', 'metric_version': 1}
        previous = app.dependency_overrides.copy()
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(previous)))
        app.dependency_overrides[building_session] = lambda: session
        app.dependency_overrides[area_session] = lambda: session
        for config in ({}, ENV, ENV | {'ENERGY_MAP_ZITADEL_ISSUER': 'broken'}):
            with patch.dict(os.environ, config, clear=True), \
                 patch('src.api.auth.get_auth_settings', side_effect=AssertionError('public auth access')), \
                 patch('src.api.auth.ZitadelIntrospector.authenticate', side_effect=AssertionError('public introspection')), \
                 patch('src.api.buildings.resolve_building', return_value=Resolution('available', fixture)), \
                 patch('src.api.main.SessionLocal', return_value=session), \
                 patch('src.api.areas.selected_release', return_value=release), \
                 TestClient(app) as client:
                for path in ('/healthz', '/readyz', '/energy-data/buildings/fixture-key',
                             '/energy-data/tiles/0/0/0.pbf', '/energy-data/tiles/11/0/0.pbf',
                             '/energy-data/areas/region/01'):
                    response = client.get(path, headers={'Authorization': 'Bearer invalid'})
                    self.assertEqual(response.status_code, 200, path)
                    self.assertNotEqual(response.headers.get('cache-control'), 'no-store')
                session.execute.return_value.scalar_one.return_value = b'area-tile'
                response = client.get('/energy-data/areas/region/tiles/0/0/0.pbf', headers={'Authorization': 'invalid'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'area-tile')
                self.assertEqual(response.headers['cache-control'], 'no-cache')
                session.execute.return_value.scalar_one.return_value = True
        for call in session.execute.call_args_list:
            self.assertTrue(str(call.args[0]).lstrip().startswith(('SELECT', 'WITH')))
        session.add.assert_not_called()
        session.flush.assert_not_called()
        session.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
