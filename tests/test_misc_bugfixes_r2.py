"""Round-2 regressions for i18n, config, throttler, session, and policies."""

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Session, use_class
from fanest.auth.policies import AbilityBuilder
from fanest.config.module import _parse_env_value
from fanest.core.enhancers import APP_GUARD
from fanest.i18n.module import I18nOptions, I18nService
from fanest.session import SessionModule
from fanest.throttler import Throttle, ThrottlerGuard, ThrottlerModule


# --------------------------------------------------------------------------- #
# i18n
# --------------------------------------------------------------------------- #
def test_i18n_interpolation_handles_backslashes_and_group_refs():
    service = I18nService(
        I18nOptions(translations={"en": {"saved": "Saved to {path}", "echo": "{v}"}}, fallback_language="en")
    )
    assert service.translate("saved", lang="en", args={"path": r"C:\Users\test"}) == r"Saved to C:\Users\test"
    assert service.translate("echo", lang="en", args={"v": r"a\1b"}) == r"a\1b"
    assert service.translate("echo", lang="en", args={"v": r"\g<0>"}) == r"\g<0>"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_env_value_strips_inline_comment_after_quotes():
    assert _parse_env_value('"s3cret" # comment') == "s3cret"
    assert _parse_env_value("'abc123' # note") == "abc123"
    assert _parse_env_value("plain # note") == "plain"
    assert _parse_env_value('"a # b"') == "a # b"  # hash inside quotes is literal


# --------------------------------------------------------------------------- #
# throttler
# --------------------------------------------------------------------------- #
def _throttle_app(controller):
    @Module(
        imports=[ThrottlerModule.for_root(limit=1000, ttl=60)],
        controllers=[controller],
        providers=[use_class(APP_GUARD, ThrottlerGuard)],
    )
    class M:
        pass

    return TestClient(FaNestFactory.create(M), raise_server_exceptions=False)


def test_class_level_throttle_applies_to_handlers():
    from fanest.throttler import Throttle as _Throttle

    @_Throttle(limit=1, ttl=60)
    @Controller("a")
    class A:
        @Get("/")
        async def read(self):
            return {"ok": True}

    client = _throttle_app(A)
    codes = [client.get("/a").status_code for _ in range(3)]
    assert 429 in codes


def test_method_skip_throttle_false_overrides_controller_skip():
    from fanest.throttler import SkipThrottle

    @SkipThrottle()
    @Controller("b")
    class B:
        @SkipThrottle(False)
        @Throttle(limit=1, ttl=60)
        @Get("/")
        async def read(self):
            return {"ok": True}

    client = _throttle_app(B)
    codes = [client.get("/b").status_code for _ in range(2)]
    assert 429 in codes


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #
def _session_app(rolling):
    @Controller("s")
    class S:
        @Get("/set")
        async def setter(self, session: dict = Session()):
            session["u"] = 1
            return {"ok": True}

        @Get("/read")
        async def reader(self, session: dict = Session()):
            return {"u": session.get("u")}

    @Module(imports=[SessionModule.for_root(secret_key="k" * 20, rolling=rolling)], controllers=[S])
    class M:
        pass

    return TestClient(FaNestFactory.create(M))


def test_malformed_neighbor_cookie_does_not_500():
    client = _session_app(True)
    response = client.get("/s/read", headers={"cookie": "weird{name=1; other=2"})
    assert response.status_code == 200


def test_rolling_false_does_not_reissue_cookie_on_unmodified_request():
    client = _session_app(False)
    client.get("/s/set")
    read = client.get("/s/read")
    assert "set-cookie" not in {key.lower() for key in read.headers}

    rolling_client = _session_app(True)
    rolling_client.get("/s/set")
    rolling_read = rolling_client.get("/s/read")
    assert "set-cookie" in {key.lower() for key in rolling_read.headers}


# --------------------------------------------------------------------------- #
# policies
# --------------------------------------------------------------------------- #
def test_conditional_rule_grants_at_type_level():
    class Article:
        def __init__(self, owner):
            self.owner = owner

    ability = AbilityBuilder().can(
        "update", Article, when=lambda article: article.owner == "alice"
    ).build()
    # Type-level check: the permission is potentially available.
    assert ability.can("update", Article) is True
    assert ability.can("update", "Article") is True
    # Instance-level check still evaluates the condition.
    assert ability.can("update", Article("alice")) is True
    assert ability.can("update", Article("bob")) is False
