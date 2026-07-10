from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Post, UseGuards, use_class
from fanest.auth import ABILITY_FACTORY, Ability, AbilityBuilder, CheckPolicies, PoliciesGuard


class Article:
    def __init__(self, locked: bool = False, owner: str | None = None) -> None:
        self.locked = locked
        self.owner = owner


def test_ability_builder_casl_semantics():
    ability = (
        AbilityBuilder()
        .can("read", Article)
        .can("update", Article, when=lambda a: a.owner == "alice")
        .cannot("read", Article, when=lambda a: a.locked)
        .build()
    )
    assert ability.can("read", Article(locked=False)) is True
    assert ability.can("read", Article(locked=True)) is False
    assert ability.can("update", Article(owner="alice")) is True
    assert ability.can("update", Article(owner="bob")) is False
    assert ability.cannot("delete", Article()) is True

    admin = AbilityBuilder().can("manage", "all").build()
    assert admin.can("read", Article) is True
    assert admin.can("delete", "Anything") is True
    assert isinstance(admin, Ability)


@Injectable()
class AbilityFactory:
    def create_for_user(self, user):
        builder = AbilityBuilder()
        if user and user.get("role") == "admin":
            builder.can("manage", "all")
        elif user:
            builder.can("read", Article)
        return builder.build()


class SetUserGuard:
    async def can_activate(self, context):
        role = context.request.headers.get("x-role")
        context.request.state.user = {"role": role} if role else None
        return True


@Controller("articles")
class ArticlesController:
    @Get("/")
    @UseGuards(SetUserGuard, PoliciesGuard)
    @CheckPolicies(lambda ability: ability.can("read", Article))
    async def read(self):
        return {"ok": True}

    @Post("/")
    @UseGuards(SetUserGuard, PoliciesGuard)
    @CheckPolicies(lambda ability: ability.can("manage", "all"))
    async def create(self):
        return {"created": True}


@Module(controllers=[ArticlesController], providers=[AbilityFactory, use_class(ABILITY_FACTORY, AbilityFactory)])
class PolicyModule:
    pass


def test_policies_guard_allows_and_denies_by_ability():
    with TestClient(FaNestFactory.create(PolicyModule), raise_server_exceptions=False) as client:
        assert client.get("/articles/", headers={"x-role": "admin"}).status_code == 200
        assert client.get("/articles/", headers={"x-role": "user"}).status_code == 200
        assert client.get("/articles/").status_code == 403  # no user -> empty ability
        assert client.post("/articles/", headers={"x-role": "user"}).status_code == 403
        assert client.post("/articles/", headers={"x-role": "admin"}).status_code == 201


def test_check_policies_noop_when_no_policies():
    @Controller("open")
    class OpenController:
        @Get("/")
        @UseGuards(PoliciesGuard)
        async def index(self):
            return {"open": True}

    @Module(controllers=[OpenController])
    class OpenModule:
        pass

    with TestClient(FaNestFactory.create(OpenModule)) as client:
        assert client.get("/open/").status_code == 200
