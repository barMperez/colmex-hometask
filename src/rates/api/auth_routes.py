"""``POST /api/v1/auth/token``: OAuth2 password form -> bearer token for the demo user."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from rates.api.auth import TOKEN_URL, InvalidCredentials, TokenAuthority, get_token_authority
from rates.api.problems import problem_openapi
from rates.api.schemas import TokenOut

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post(
    TOKEN_URL,
    response_model=TokenOut,
    summary="Get a bearer token for the demo user",
    description=(
        "OAuth2 password form (`username`, `password`). Returns a bearer token for "
        "`GET /api/v1/exchange-rates`. **Demo only:** a single configured user, no refresh "
        "tokens, no revocation."
    ),
    responses={
        400: problem_openapi("INVALID_REQUEST"),
        401: problem_openapi("INVALID_CREDENTIALS"),
    },
)
async def issue_token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    authority: Annotated[TokenAuthority, Depends(get_token_authority)],
) -> TokenOut:
    try:
        principal = authority.authenticate(form.username, form.password)
    except InvalidCredentials:
        log.warning("auth.login_failed username=%r", form.username[:64])
        raise
    issued = authority.issue(principal)
    log.info("auth.token_issued user=%s expires_in=%d", principal.username, issued.expires_in)
    return TokenOut(access_token=issued.access_token, expires_in=issued.expires_in)
