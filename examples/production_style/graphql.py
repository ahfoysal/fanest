from typing import Any, cast

from fanest import ParseIntPipe
from fanest.config import ConfigService
from fanest.graphql import Args, Mutation, Query, Resolver, UseGuards, UseInterceptors

from examples.production_style.common import GraphQLTimingInterceptor
from examples.production_style.users.dto import CreateUserDto
from examples.production_style.users.service import UsersService


class GraphQLApiKeyGuard:
    def can_activate(self, context):
        return context.kwargs.get("api_key") == "local-dev-key"


@Resolver()
class UsersResolver:
    def __init__(self, users: UsersService, config: ConfigService):
        self.users = users
        self.config = config

    @Query("users")
    @UseInterceptors(GraphQLTimingInterceptor)
    async def users_query(self, search: str | None = cast(Any, Args("search", default=None))):
        return [user.model_dump() for user in self.users.list_users(search)]

    @Query("user")
    async def user_query(self, user_id: int = cast(Any, Args("id", ParseIntPipe()))):
        return self.users.get_user(user_id).model_dump()

    @Mutation("createUser")
    async def create_user(
        self,
        email: str = cast(Any, Args("email")),
        name: str = cast(Any, Args("name")),
        password: str = cast(Any, Args("password")),
    ):
        user = await self.users.create_user(
            CreateUserDto(email=email, name=name, password=password, roles=["user"])
        )
        return user.model_dump()

    @Query("adminStats")
    @UseGuards(GraphQLApiKeyGuard)
    async def admin_stats(self, api_key: str = cast(Any, Args("apiKey"))):
        return self.users.stats()
