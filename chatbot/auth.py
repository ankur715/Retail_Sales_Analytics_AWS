"""Login (milestone 4): verify the caller's Keycloak access token and turn it into a Principal.

AUTH_MODE=off   no login; everyone is access.ANONYMOUS (all brands) -- tests, CI, the laptop,
                load and chaos tests, i.e. exactly the behaviour before this milestone
AUTH_MODE=oidc  POST /v1/chat and /api/chat need `Authorization: Bearer <access token>`:
                  - signature checked against Keycloak's published keys (JWKS, cached; RS256 only)
                  - issuer must be OIDC_ISSUER, audience must include OIDC_AUDIENCE (chatbot-api),
                    and the token must not be expired (30 s clock leeway)
                  - `groups` claim -> brands: brand-C101 -> Aurora Apparel ..., all-brands -> every brand
                Missing or bad token -> 401 with WWW-Authenticate; valid but no brand -> 403.

The chat page logs in with the authorization-code flow + PKCE as the public client
`chatbot-web` (chatbot/static/index.html) and sends the access token on each question.
Health, metrics and the page itself stay open (see docs/security/threat-model.md).
"""
import logging
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request

from chatbot import access
from etl import config

log = logging.getLogger(__name__)
ALGORITHMS = ["RS256"]
LEEWAY_S = 30


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer realm="chatbot"'})


@lru_cache(maxsize=1)
def jwks_client() -> jwt.PyJWKClient:
    url = config.OIDC_JWKS_URL or f"{config.OIDC_ISSUER}/protocol/openid-connect/certs"
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600, timeout=5)


def verify(token: str) -> dict:
    """The token's claims, or raise jwt exceptions."""
    key = jwks_client().get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=ALGORITHMS, audience=config.OIDC_AUDIENCE, issuer=config.OIDC_ISSUER,
                      leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "sub", "aud"]})


def principal_from_claims(claims: dict) -> access.Principal:
    brands = access.brands_for_groups(claims.get("groups") or [])
    if brands is not None and not brands:
        raise HTTPException(403, "Your account isn't assigned to any brand")
    return access.Principal(subject=claims["sub"], username=claims.get("preferred_username", ""), brands=brands)


def principal(request: Request) -> access.Principal:
    """FastAPI dependency for the chat routes."""
    if config.AUTH_MODE != "oidc":
        return access.ANONYMOUS
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("Sign in first: this endpoint needs a bearer token")
    try:
        claims = verify(token)
    except jwt.ExpiredSignatureError:
        raise unauthorized("Your session expired: sign in again") from None
    except (jwt.PyJWKClientError, jwt.InvalidTokenError) as e:
        log.info("rejected token: %s", e)   # never log the token itself
        raise unauthorized("Invalid token") from None
    return principal_from_claims(claims)


def page_config() -> dict:
    """What the chat page needs to start a login (no secrets: chatbot-web is a public client)."""
    if config.AUTH_MODE != "oidc":
        return {"auth": "off"}
    return {"auth": "oidc", "issuer": config.OIDC_ISSUER, "client_id": config.OIDC_WEB_CLIENT_ID}
