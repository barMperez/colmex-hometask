from datetime import UTC, datetime, timedelta

import jwt
import pytest

from rates.api.auth import AUDIENCE, ISSUER, InvalidCredentials, InvalidToken, Principal, TokenAuthority
from rates.clock import FixedClock
from rates.settings import Settings

SECRET = "s" * 40
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def authority(clock: FixedClock, secret: str = SECRET) -> TokenAuthority:
    settings = Settings(_env_file=None, jwt_secret=secret, demo_password="pw", jwt_ttl_seconds=3600)
    return TokenAuthority(settings, clock)


def test_issue_and_verify():
    auth = authority(FixedClock(T0))
    issued = auth.issue(auth.authenticate("demo", "pw"))

    assert issued.expires_in == 3600
    claims = jwt.decode(issued.access_token, SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                        options={"verify_exp": False})
    assert claims["sub"] == "demo"
    assert claims["exp"] - claims["iat"] == 3600
    assert claims["jti"]
    assert auth.verify(issued.access_token) == Principal("demo")


def test_expiry_follows_the_injected_clock():
    clock = FixedClock(T0)
    auth = authority(clock)
    token = auth.issue(Principal("demo")).access_token

    clock.set(T0 + timedelta(seconds=3599))
    assert auth.verify(token) == Principal("demo")
    clock.set(T0 + timedelta(seconds=3601))  # issued 10:00, used 11:00:01
    with pytest.raises(InvalidToken, match="expired"):
        auth.verify(token)


@pytest.mark.parametrize(
    ("username", "password"), [("demo", "wrong"), ("other", "pw"), ("", ""), ("DEMO", "pw")]
)
def test_wrong_credentials(username, password):
    with pytest.raises(InvalidCredentials):
        authority(FixedClock(T0)).authenticate(username, password)


def forge(claims: dict, key: str = SECRET, alg: str = "HS256") -> str:
    base = {"sub": "demo", "iat": int(T0.timestamp()), "exp": int(T0.timestamp()) + 3600,
            "iss": ISSUER, "aud": AUDIENCE}
    return jwt.encode(base | claims, key, algorithm=alg)


@pytest.mark.parametrize(
    "token",
    [
        forge({}, key="t" * 40),                 # signed with another secret
        forge({"aud": "someone-else"}),          # wrong audience
        forge({"iss": "someone-else"}),          # wrong issuer
        forge({"sub": ""}),                      # empty subject
        jwt.encode({"iat": int(T0.timestamp()), "exp": int(T0.timestamp()) + 3600, "iss": ISSUER,
                    "aud": AUDIENCE}, SECRET, algorithm="HS256"),  # missing sub
        "not-a-jwt",
    ],
    ids=["wrong-secret", "wrong-audience", "wrong-issuer", "empty-sub", "missing-sub", "garbage"],
)
def test_invalid_tokens_rejected(token):
    with pytest.raises(InvalidToken):
        authority(FixedClock(T0)).verify(token)


def test_alg_none_rejected():
    header = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT"}').decode()
    payload = jwt.utils.base64url_encode(
        (f'{{"sub":"demo","iat":{int(T0.timestamp())},"exp":{int(T0.timestamp()) + 3600},'
         f'"iss":"{ISSUER}","aud":"{AUDIENCE}"}}').encode()
    ).decode()
    with pytest.raises(InvalidToken):
        authority(FixedClock(T0)).verify(f"{header}.{payload}.")


def test_iat_checked_against_injected_clock():
    clock = FixedClock(datetime(2028, 1, 4, 8, 0, tzinfo=UTC))  # far from the wall clock
    auth = authority(clock)
    assert auth.verify(auth.issue(Principal("demo")).access_token) == Principal("demo")
    with pytest.raises(InvalidToken, match="not yet valid"):
        auth.verify(forge({"iat": int(clock.now().timestamp()) + 60, "exp": int(clock.now().timestamp()) + 3600}))
