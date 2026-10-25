# Zitadel access-token authentication

F001.2 supplies a reusable resource-server dependency for later owner routes.
It adds no product route, login flow, saved state or database access. The completed
[building contract](building-contract.md) is unchanged. Core owns Better Auth
sessions, cookies, login callbacks and token refresh.

## Server configuration and provisioning

Configure only the API process, using dedicated Energy API credentials. Values
below are placeholders; `.env.example` is not a production configuration.

```dotenv
ENERGY_MAP_ZITADEL_ISSUER=https://identity.example.invalid
ENERGY_MAP_ZITADEL_AUDIENCES='["<energy-api-audience>"]'
ENERGY_MAP_ZITADEL_CLIENT_ID=<dedicated-energy-api-introspection-client-id>
ENERGY_MAP_ZITADEL_CLIENT_SECRET=<dedicated-energy-api-introspection-client-secret>
ENERGY_MAP_ZITADEL_REQUIRED_SCOPES=
```

| Setting | Validation and policy |
| --- | --- |
| `ENERGY_MAP_ZITADEL_ISSUER` | Exact HTTPS origin, at most 2,048 ASCII characters, no trailing slash, path, credentials, query or fragment. No claim-based discovery or normalization. Configure the provider's exact issuer using this convention. |
| `ENERGY_MAP_ZITADEL_AUDIENCES` | JSON array of 1–32 nonempty exact audience strings, each at most 512 printable ASCII characters without whitespace or `*`; total JSON at most 16 KiB. Any exact member match suffices. No prefix, substring or wildcard acceptance. |
| `ENERGY_MAP_ZITADEL_CLIENT_ID` | Dedicated introspection API client ID, 1–2,048 printable ASCII characters without whitespace. Not the frontend OAuth client ID. |
| `ENERGY_MAP_ZITADEL_CLIENT_SECRET` | Dedicated server secret, 1–4,096 printable ASCII characters without whitespace; excluded from settings repr. Never send it to browsers. |
| `ENERGY_MAP_ZITADEL_REQUIRED_SCOPES` | Optional all-of scope policy; empty/unset imposes no additional permission. Up to 128 space-separated OAuth scope tokens, total at most 4,096 characters; quotes, backslashes, tabs, empty tokens and non-ASCII are invalid. No default role/admin requirement. |

Settings are read and validated lazily for protected requests. Missing/malformed
settings disable protected access with sanitized 503. Import/startup/OpenAPI and
public routes do not validate them or call Zitadel. `/healthz` remains process
liveness and `/readyz` remains area-data readiness; neither proves identity
readiness. No startup migration or live-provider health probe is added. Public
source reads and future valid share reads must omit this dependency, including
when the caller supplies an invalid Authorization header.

The official [introspection guide](https://zitadel.com/docs/guides/integrate/token-introspection)
supports both opaque and JWT access tokens with Basic client authentication.
The read-only Hiilikartta integration uses Basic too. We select Basic for deployment
compatibility; [private-key JWT](https://zitadel.com/docs/guides/integrate/token-introspection/private-key-jwt)
is the provider's recommended alternative and is not implemented here.

An operator must provision a dedicated **API** application with Basic enabled and
its own secret, following the official [Basic guide](https://zitadel.com/docs/guides/integrate/token-introspection/basic-auth).
Establish the Energy API's project/application audience relationship and configure
only approved resource audiences. Core currently requests
`urn:zitadel:iam:org:project:id:<configured-project-id>:aud`; inspecting that code
does not prove Energy API registration or actual token audiences. Do not infer
an audience from the frontend client ID or silently reuse core/Hiilikartta secrets.
The returned access token's `client_id` may identify its frontend; it is not required
to equal the introspection client ID. Rotate credentials in the server's private
configuration through the established deployment process; invalid credentials
fail protected requests closed. This feature changes no provider/core settings.

**External integration remains unverified:** an authorized later smoke test must
confirm Basic application provisioning, core's audience scope, actual opaque/JWT
access-token claims (including issuer, audience, subject and token type), normal
user access, and rejection of another resource's token. Provider fixtures do not
establish any of these deployment facts. No real credentials/token were inspected.

## Callable contract for later children

Import from `src.api.auth`:

- `AuthenticatedPrincipal`: frozen value with only `issuer: str` and `subject: str`.
- `require_principal`: reusable FastAPI dependency returning that principal.
- `get_introspector`: overrideable dependency returning `ZitadelIntrospector`.
- `get_auth_settings() -> ZitadelSettings`: lazy environment loader; invalid settings
  raise sanitized `AuthConfigurationError`, translated by `get_introspector` to 503.
- `ZitadelIntrospector(settings, transport=None, clock=time.time, deadline_seconds=5.0)`:
  `await authenticate(token)` uses the same HTTP/claim path with an optional test
  transport and UTC epoch clock. Transport/deadline overrides are test seams,
  not environment options or insecure production modes.

Later-handler signature (no demonstration endpoint is installed):

```python
from typing import Annotated
from fastapi import Depends
from src.api.auth import AuthenticatedPrincipal, require_principal

async def owner_handler(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
):
    owner_issuer, owner_subject = principal.issuer, principal.subject
    # Scope every private lookup/mutation/list/share-management query by BOTH.
    # Authenticate before opening the mutation transaction.
```

A changed login/access token with the same `(issuer, subject)` is the same owner.
Different subjects or issuers are different owners. Preserve the exact subject;
never trim or replace it with email, name, session ID, request owner fields or
headers. Principal identity is authentication, not resource authorization. F001.3
owns owner isolation, narrow writer, list/CRUD, transaction ordering, 404 privacy,
ETags/preconditions and complete endpoint tests. F001.4 owns shares and their
anonymous read path. Neither cookies nor invalid credentials grant anonymous
fallback on owner routes.

`require_principal` applies `Cache-Control: no-store` to ordinary successful
FastAPI responses. Authentication failures also carry it. Later owner routers
must enforce no-store on **all** their responses, including returned Response
objects and validation, authorization and precondition errors; this dependency
cannot cover those later paths by itself. Do not apply global auth middleware or
change public cache headers.

## Bearer, introspection and claim policy

Exactly one `Authorization` header is required: case-insensitive `Bearer`, one
ASCII space, then an RFC 6750 token (`A–Z a–z 0–9 - . _ ~ + /`, optional trailing
`=`). The whole header is capped at 16 KiB. Duplicate headers, empty values,
controls, internal whitespace, comma-joined credentials and unsupported schemes
return 401 before settings/provider access. Tokens are never taken from query
parameters, cookies or owner headers. Opaque and JWT-shaped inputs follow the
same introspection path; no JWT decoding, ID-token or local-signature fallback.

Each request makes one authenticated form POST to the server-configured issuer's
`/oauth/v2/introspect` with `token` and `token_type_hint=access_token`. Basic client
ID/secret components are form-URL-encoded before Basic encoding. Normal HTTPS
certificate verification stays enabled; environment proxies/credentials are
ignored (`trust_env=False`). No redirects or automatic retries. Connect, pool and
write timeouts are 1 second each; read timeout is 3 seconds. A 5-second overall
async deadline includes client use, response consumption and cleanup. The client
and response are deterministically closed on success, error and cancellation.

The response must be HTTP 200 and a JSON object with unique keys. At most 64 KiB
are consumed before parsing; compressed responses are refused (the request asks
for identity encoding). Provider 401/403 concerns the API client's credentials,
not the user's authentication: all non-200 statuses, malformed JSON, oversized
bodies and provider/transport failures become 503. There is no provider-body or
exception-text reflection or logging.

| Claim | Required handling |
| --- | --- |
| `active` | Literal boolean; false is 401 and requires no other claims. Missing/wrong type is 503. |
| `exp` | Required finite JSON number, excluding boolean/numeric string. Checked after introspection consumption/client cleanup: must be strictly greater than the current UTC epoch. Equality/expiry is 401, no grace. Missing/malformed is 503. |
| `iss` | Required string, exact equality to the configured issuer; a well-formed mismatch (including empty) is 401. Missing/wrong type is 503. |
| `aud` | Required nonempty string or nonempty array of nonblank strings. At least one exact configured match; wrong resource is 401. Missing/wrong type/empty/mixed array is 503. |
| `sub` | Required string; empty/whitespace-only is 401, missing/wrong type is 503. Nonempty identity is preserved exactly. |
| `token_type` | Required string, case-insensitive `Bearer`; other/empty values are 401, missing/wrong type is 503. |
| `nbf` | If present, finite JSON number excluding bool/string; future use is 401, malformed is 503. Equality is allowed. |
| `iat` | If present, finite JSON number excluding bool/string; malformed is 503. A future finite iat is not an additional rejection rule; exp/nbf govern timing. |
| `scope` | Ignored without configured policy. When required, missing/empty/insufficient exact grants are 403. Malformed type, scope-token syntax or exceeding the same 128-token/4,096-character bounds is 503. All configured scopes are required, after token validity checks. |

Role/admin claims and extra identity fields grant no permission. No cache (positive
or negative), retries, or stale-on-error fallback exists. Every protected request
rechecks the provider: provider-reported revocation takes effect on the next
request; an outage never reuses earlier success. This adds network latency and
makes protected access depend on provider availability. Future caching would need
separate expiry-capped short lifetimes and deterministic revocation-window tests.

## HTTP errors and frontend handoff

All authentication errors have JSON `detail` and `Cache-Control: no-store`:

| HTTP | Exact detail | Additional header |
| --- | --- | --- |
| 401 | `{"code":"invalid_access_token","message":"A valid Bearer access token is required"}` | `WWW-Authenticate: Bearer` |
| 403 | `{"code":"insufficient_permission","message":"Required permission is missing"}` | None |
| 503 | `{"code":"authentication_unavailable","message":"Authentication is temporarily unavailable"}` | None |

For example an expired token returns:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Cache-Control: no-store
Content-Type: application/json

{"detail":{"code":"invalid_access_token","message":"A valid Bearer access token is required"}}
```

Use core `getAuthAccessToken()` (`src/common/auth/client.tsx`) or its server-side
session helper and forward its **provider access token** in the Authorization
header on each future owner request. Core refresh/session errors stay in core;
its user/session IDs, ID tokens and UI mock tokens are not API credentials.
Refresh/re-authenticate via core for 401; surface insufficient permission for 403
and temporary protected-service unavailability for 503. Do not loop retries or
fall back to an anonymous write.

Direct browser calls require the intended origin in `ENERGY_MAP_API_ORIGINS` and
Authorization in preflight. Existing API CORS permits request headers; this child
does not broaden origins. A same-origin core proxy must forward the provider token
and preserve status/cache/precondition headers, keeping core cookies at core.
Later owner features must document conditional headers and expose ETag. CORS is
not an authorization boundary. Never put access tokens in URLs, analytics, logs
or error reports. Keep Authorization/Basic headers, credentials, request/response
bodies and HTTP debug traces out of application/proxy/APM logs; do not log HTTPX
request reprs or provider exceptions.

Tests in `tests/test_auth.py` mount a test-only protected route, HTTPX mock
transports and clocks. They exercise validity/errors, no-cache revocation,
deadlines/response bounds, sanitized failures and public-route isolation without
database/state writes. They establish no real-provider, frontend, deployment or
later owner/share authorization acceptance. Runtime setup/recovery belongs to the
feature-scoped `runtime_doctor`, not manual service/environment management.
