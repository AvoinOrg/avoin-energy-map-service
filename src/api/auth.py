"""Lazy, fail-closed Zitadel access-token boundary; no database or login flow.

See authentication.md for the callable, provider, error and deployment contracts.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import os
import re
import time
from typing import Annotated, Callable
from urllib.parse import quote_plus, urlsplit

from fastapi import Depends, HTTPException, Request, Response, Security
from fastapi.security import HTTPBearer
import httpx

MAX_AUTHORIZATION_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT = httpx.Timeout(connect=1.0, pool=1.0, write=1.0, read=3.0)
OVERALL_TIMEOUT_SECONDS = 5.0
_BEARER = re.compile(r'Bearer ([A-Za-z0-9._~+/\-]+=*)', re.IGNORECASE | re.ASCII)
_SCOPE = re.compile(r'[\x21\x23-\x5b\x5d-\x7e]+', re.ASCII)

_ERRORS = {
    401: ('invalid_access_token', 'A valid Bearer access token is required'),
    403: ('insufficient_permission', 'Required permission is missing'),
    503: ('authentication_unavailable', 'Authentication is temporarily unavailable'),
}


def _failure(status: int) -> HTTPException:
    code, message = _ERRORS[status]
    headers = {'Cache-Control': 'no-store'}
    if status == 401:
        headers['WWW-Authenticate'] = 'Bearer'
    return HTTPException(status, {'code': code, 'message': message}, headers=headers)


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    issuer: str
    subject: str


class AuthConfigurationError(ValueError):
    def __init__(self):
        super().__init__('Invalid authentication configuration')


def _setting_text(value: str, maximum: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and value.isascii() and all(33 <= ord(c) <= 126 for c in value))


def _scopes(value: str) -> frozenset[str]:
    # OAuth scope-token grammar; only ASCII space separates scopes.
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError
    if value == '':
        return frozenset()
    parts = value.split(' ')
    if len(parts) > 128 or any(not _SCOPE.fullmatch(part) for part in parts):
        raise ValueError
    return frozenset(parts)


@dataclass(frozen=True, slots=True)
class ZitadelSettings:
    issuer: str
    audiences: frozenset[str]
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    required_scopes: frozenset[str] = frozenset()

    def __post_init__(self):
        try:
            if not _setting_text(self.issuer, 2048):
                raise ValueError
            parsed = urlsplit(self.issuer)
            # A canonical HTTPS origin, without even a trailing slash. Never
            # normalize a claim into a trust value or follow an untrusted URL.
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                    or self.issuer != f'https://{parsed.netloc}' or parsed.port == 0
                    or '%' in parsed.netloc or '\\' in parsed.netloc):
                raise ValueError
            if (not isinstance(self.audiences, frozenset) or not 1 <= len(self.audiences) <= 32
                    or any(not _setting_text(aud, 512) or '*' in aud for aud in self.audiences)):
                raise ValueError
            if (not _setting_text(self.client_id, 2048)
                    or not _setting_text(self.client_secret, 4096)):
                raise ValueError
            if (not isinstance(self.required_scopes, frozenset) or len(self.required_scopes) > 128
                    or any(not isinstance(s, str) or not _SCOPE.fullmatch(s) for s in self.required_scopes)
                    or len(' '.join(self.required_scopes)) > 4096):
                raise ValueError
        except (ValueError, TypeError):
            raise AuthConfigurationError() from None

    @property
    def introspection_url(self) -> str:
        return f'{self.issuer}/oauth/v2/introspect'


def get_auth_settings() -> ZitadelSettings:
    """Read only server environment, on protected requests; never at startup."""
    try:
        raw_audiences = os.environ['ENERGY_MAP_ZITADEL_AUDIENCES']
        if len(raw_audiences) > 16 * 1024:
            raise ValueError
        audiences = json.loads(raw_audiences)
        if (not isinstance(audiences, list) or not 1 <= len(audiences) <= 32
                or any(not isinstance(aud, str) for aud in audiences)):
            raise ValueError
        return ZitadelSettings(
            issuer=os.environ['ENERGY_MAP_ZITADEL_ISSUER'],
            audiences=frozenset(audiences),
            client_id=os.environ['ENERGY_MAP_ZITADEL_CLIENT_ID'],
            client_secret=os.environ['ENERGY_MAP_ZITADEL_CLIENT_SECRET'],
            required_scopes=_scopes(os.getenv('ENERGY_MAP_ZITADEL_REQUIRED_SCOPES', '')),
        )
    except (KeyError, ValueError, TypeError, RecursionError):
        raise AuthConfigurationError() from None


class _StrictBearer(HTTPBearer):
    async def __call__(self, request: Request) -> str:
        headers = request.headers.getlist('authorization')
        if len(headers) != 1 or len(headers[0]) > MAX_AUTHORIZATION_BYTES:
            raise _failure(401)
        match = _BEARER.fullmatch(headers[0])
        if match is None:
            raise _failure(401)
        return match.group(1)


_bearer = _StrictBearer(scheme_name='ZitadelAccessToken', auto_error=False,
                        description='Provider access token (opaque or JWT), verified by introspection')


class _ProviderContractError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ProviderContractError
        result[key] = value
    return result


def _epoch(value) -> int | float:
    if type(value) not in (int, float):
        raise _ProviderContractError
    try:
        if not math.isfinite(value):
            raise _ProviderContractError
    except OverflowError:
        raise _ProviderContractError from None
    return value


def _principal(claims, settings: ZitadelSettings, now: float) -> AuthenticatedPrincipal:
    if not isinstance(claims, dict) or type(claims.get('active')) is not bool:
        raise _ProviderContractError
    if claims['active'] is False:
        raise _failure(401)
    exp = _epoch(claims.get('exp'))
    issuer, subject, audience, token_type = (claims.get(k) for k in ('iss', 'sub', 'aud', 'token_type'))
    if not all(isinstance(v, str) for v in (issuer, subject, token_type)):
        raise _ProviderContractError
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list):
        audiences = audience
    else:
        raise _ProviderContractError
    if not audiences or any(not isinstance(v, str) or not v.strip() for v in audiences):
        raise _ProviderContractError
    nbf = _epoch(claims['nbf']) if 'nbf' in claims else None
    # iat is shape-checked if present; it is not an additional clock-skew policy.
    if 'iat' in claims:
        _epoch(claims['iat'])
    if (exp <= now or (nbf is not None and nbf > now) or issuer != settings.issuer
            or not settings.audiences.intersection(audiences) or not subject.strip()
            or token_type.lower() != 'bearer'):
        raise _failure(401)
    if settings.required_scopes:
        try:
            scopes = _scopes(claims.get('scope', ''))
        except ValueError:
            raise _ProviderContractError from None
        if not settings.required_scopes.issubset(scopes):
            raise _failure(403)
    return AuthenticatedPrincipal(issuer=issuer, subject=subject)


@dataclass(frozen=True, slots=True, repr=False)
class ZitadelIntrospector:
    settings: ZitadelSettings
    transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.time
    deadline_seconds: float = OVERALL_TIMEOUT_SECONDS

    async def authenticate(self, token: str) -> AuthenticatedPrincipal:
        """No cache/retries/stale fallback. Test transport uses this same path."""
        try:
            async with asyncio.timeout(self.deadline_seconds):
                async with httpx.AsyncClient(
                    auth=httpx.BasicAuth(quote_plus(self.settings.client_id), quote_plus(self.settings.client_secret)),
                    timeout=HTTP_TIMEOUT, follow_redirects=False, trust_env=False,
                    verify=True, transport=self.transport,
                    headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                ) as client:
                    async with client.stream('POST', self.settings.introspection_url,
                                             data={'token': token, 'token_type_hint': 'access_token'}) as response:
                        if response.status_code != 200:
                            raise _ProviderContractError
                        # Refuse compressed content to bound memory before decoding.
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise _ProviderContractError
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise _ProviderContractError
                            body.extend(chunk)
                        claims = json.loads(body, object_pairs_hook=_unique_object)
                # Check expiry only after response consumption and client cleanup.
                return _principal(claims, self.settings, self.clock())
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError, RecursionError):
            # InvalidURL is separate from HTTPError; unsupported configured URLs
            # must use the same unavailable contract as other provider failures.
            # Do not log exception text, request/response reprs or provider bodies.
            raise _failure(503) from None


def get_introspector() -> ZitadelIntrospector:
    """Override this collaborator in tests; defaults stay lazy and server-only."""
    try:
        return ZitadelIntrospector(get_auth_settings())
    except AuthConfigurationError:
        raise _failure(503) from None


async def require_principal(
    response: Response,
    token: Annotated[str, Security(_bearer)],
    introspector: Annotated[ZitadelIntrospector, Depends(get_introspector)],
) -> AuthenticatedPrincipal:
    principal = await introspector.authenticate(token)
    response.headers['Cache-Control'] = 'no-store'
    return principal
