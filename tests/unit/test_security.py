import pytest
from pydantic import SecretStr

from gateway.core.security import StaticKeyAuthenticator, parse_bearer_token


def test_authenticates_any_configured_key() -> None:
    authenticator = StaticKeyAuthenticator([SecretStr("first-key"), SecretStr("second-key")])

    first = authenticator.authenticate("first-key")
    second = authenticator.authenticate("second-key")

    assert first is not None
    assert second is not None
    assert first.id != second.id


@pytest.mark.parametrize("candidate", ["", "first-ke", "first-key ", "FIRST-KEY", "other"])
def test_rejects_non_matching_credentials(candidate: str) -> None:
    authenticator = StaticKeyAuthenticator([SecretStr("first-key")])

    assert authenticator.authenticate(candidate) is None


def test_principal_id_does_not_reveal_the_key() -> None:
    principal = StaticKeyAuthenticator([SecretStr("first-key")]).authenticate("first-key")

    assert principal is not None
    assert "first-key" not in principal.id
    assert principal.id.startswith("key-")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("Bearer   abc  ", "abc"),
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic abc", None),
        ("abc", None),
    ],
)
def test_parse_bearer_token(header: str | None, expected: str | None) -> None:
    assert parse_bearer_token(header) == expected
