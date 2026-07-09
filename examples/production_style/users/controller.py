from typing import Any, cast

from fanest import (
    Body,
    Controller,
    Get,
    Param,
    ParseBoolPipe,
    ParseIntPipe,
    Patch,
    Post,
    Query,
    Serialize,
    SetHeader,
    StreamableFile,
    UseFilters,
    UseGuards,
    UseInterceptors,
    UsePipes,
    ValidationPipe,
)
from fanest.auth import CurrentUser, JwtAuthGuard, Roles, RolesGuard

from examples.production_style.common import ApiProblemFilter, TimingInterceptor
from examples.production_style.users.dto import CreateUserDto, LoginDto, UpdateUserDto
from examples.production_style.users.service import UsersService


@Controller("auth")
@UseFilters(ApiProblemFilter)
class AuthController:
    def __init__(self, users: UsersService):
        self.users = users

    @Post("login")
    @UsePipes(ValidationPipe())
    async def login(self, dto: LoginDto = cast(Any, Body())):
        return self.users.login(dto)


@Controller("users")
@UseFilters(ApiProblemFilter)
class UsersController:
    def __init__(self, users: UsersService):
        self.users = users

    @Get("/")
    @UseInterceptors(TimingInterceptor)
    async def list_users(
        self,
        search: str | None = cast(Any, Query(default=None)),
        include_disabled: bool = cast(Any, Query("includeDisabled", ParseBoolPipe(), default=False)),
    ):
        return self.users.list_users(search, include_disabled)

    @Get("{user_id}")
    @Serialize(exclude_none=True)
    async def get_user(self, user_id: int = cast(Any, Param("user_id", ParseIntPipe()))):
        return self.users.get_user(user_id)

    @Post("/")
    @UsePipes(ValidationPipe(whitelist=True))
    async def create_user(self, dto: CreateUserDto = cast(Any, Body())):
        return await self.users.create_user(dto)

    @SetHeader("x-report-format", "csv")
    @Get("exports/report.csv")
    async def report(self):
        rows = ["id,email,name,roles"]
        rows.extend(
            f"{user.id},{user.email},{user.name},{'|'.join(user.roles)}"
            for user in self.users.list_users(include_disabled=True)
        )
        return StreamableFile(
            "\n".join(rows).encode(),
            content_type="text/csv",
            filename="users.csv",
        )


@Controller("admin/users")
@UseGuards(JwtAuthGuard, RolesGuard)
@UseFilters(ApiProblemFilter)
class AdminUsersController:
    def __init__(self, users: UsersService):
        self.users = users

    @Get("me")
    async def me(self, user: dict = cast(Any, CurrentUser())):
        return {"user": user}

    @Roles("admin")
    @Get("stats")
    async def stats(self):
        return self.users.stats()

    @Roles("admin")
    @Patch("{user_id}")
    @UsePipes(ValidationPipe(whitelist=True))
    async def update_user(
        self,
        dto: UpdateUserDto = cast(Any, Body()),
        user_id: int = cast(Any, Param("user_id", ParseIntPipe())),
    ):
        return await self.users.update_user(user_id, dto)
