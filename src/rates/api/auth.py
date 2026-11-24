"""Demo authentication: HS256 JWT bearer tokens for one configured user.

Expiry is checked against the injected ``Clock`` (not the wall clock inside PyJWT),
so it follows the same time source as eligibility and is testable.
"""

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from rates.clock import Clock
from rates.settings import Settings

log = logging.getLogger(__name__)

TOKEN_URL = "/api/v1/auth/token"
ISSUER = AUDIENCE = "rates-api"
ALGORITHM = "HS256"


class AuthError(Exception):
    """Base class for 401 outcomes; ``code`` is the problem-details code."""

    code = "INVALID_TOKEN"


class AuthenticationRequired(AuthError):
    code = "AUTHENTICATION_REQUIRED"


class InvalidToken(AuthError):
    code = "INVALID_TOKEN"


class InvalidCredentials(AuthError):
    code = "INVALID_CREDENTIALS"


@dataclass(frozen=True)
class Principal:
    username: str


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_in: int


class TokenAuthority:
    """Checks the demo credentials, issues tokens and verifies them."""

    def __init__(self, settings: Settings, clock: Clock) -> None:
        if settings.jwt_secret is None or settings.demo_password is None:
            raise RuntimeError("TokenAuthority needs JWT_SECRET and DEMO_PASSWORD")
        self._secret = settings.jwt_secret.get_secret_value()
        self._ttl = settings.jwt_ttl_seconds
        self._username = settings.demo_username
        self._password = settings.demo_password.get_secret_value()
        self._clock = clock

    def authenticate(self, username: str, password: str) -> Principal:
        # Both comparisons always run: the response does not reveal which one failed.
        user_ok = hmac.compare_digest(username.encode(), self._username.encode())
        password_ok = hmac.compare_digest(password.encode(), self._password.encode())
        if not (user_ok and password_ok):
            raise InvalidCredentials("Invalid username or password")
        return Principal(self._username)

    def issue(self, principal: Principal) -> IssuedToken:
        now = self._clock.now()
        claims = {
            "sub": principal.username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._ttl)).timestamp()),
            "iss": ISSUER,
            "aud": AUDIENCE,
            "jti": uuid4().hex,
        }
        return IssuedToken(jwt.encode(claims, self._secret, algorithm=ALGORITHM), self._ttl)

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                # Time claims are checked below against the injected clock, not the wall clock.
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["exp", "iat", "sub"],
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"Invalid token: {exc}") from exc
        now = self._clock.now()
        try:
            issued_at = datetime.fromtimestamp(int(claims["iat"]), tz=now.tzinfo)
            expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=now.tzinfo)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InvalidToken("Invalid token: bad time claims") from exc
        if issued_at > now:
            raise InvalidToken("Token is not yet valid")
        if now >= expires_at:
            raise InvalidToken("Token has expired")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise InvalidToken("Invalid token: empty subject")
        return Principal(claims["sub"])


oauth2_scheme = OAuth2PasswordBearer(tokenUrl=TOKEN_URL, auto_error=False)


def get_token_authority(request: Request) -> TokenAuthority:
    return request.app.state.token_authority


async def current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    authority: Annotated[TokenAuthority, Depends(get_token_authority)],
) -> Principal:
    """Security dependency: sets ``request.state.principal`` or raises a 401 ``AuthError``."""
    if not token:
        raise AuthenticationRequired("A bearer token is required")
    principal = authority.verify(token)
    request.state.principal = principal
    return principal
