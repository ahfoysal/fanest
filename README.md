# FaNest

A progressive Python framework for building structured, scalable, and maintainable backend applications.

FaNest brings the NestJS way of thinking to Python: modules, controllers, services, decorators, dependency injection, guards, pipes, interceptors, filters, gateways, scheduled jobs, and package-style integrations. FastAPI does the HTTP work underneath; FaNest gives the application an architecture.

It is built for developers who like the NestJS workflow but want to work in Python.

## Description

Python has excellent web libraries, but larger applications still need a repeatable structure. FaNest provides that structure without hiding the Python ecosystem.

The framework combines:

- class-based modules, controllers, services, and gateways
- constructor-based dependency injection
- decorator-driven routing and WebSocket messages
- request pipeline layers: guards, pipes, interceptors, and filters
- FastAPI, Pydantic, SQLAlchemy, and pytest-friendly defaults
- a CLI shaped around everyday backend work

FaNest is inspired by NestJS, but it is not a wrapper around NestJS and is not affiliated with the NestJS project.

## Philosophy

The goal is not to make Python pretend to be TypeScript. The goal is to keep the workflow familiar for NestJS developers while choosing Python-native tools where they fit better.

FaNest should feel predictable in a growing codebase:

- modules describe boundaries
- providers hold business logic
- controllers stay thin
- decorators make framework behavior visible
- tests can override dependencies without patching imports
- packages can plug into the same module system

Small APIs should stay small. Bigger APIs should not become a pile of unrelated route functions.

## Getting Started

```bash
uv sync --extra dev
uv run uvicorn examples.basic.main:app --reload
```

Open:

```txt
http://127.0.0.1:8000/docs
```

## A Small Application

```python
from pydantic import BaseModel

from fanest import Body, Controller, FaNestFactory, Get, Injectable, Module, Post


class CreateUserDto(BaseModel):
    name: str


@Injectable()
class UsersService:
    def __init__(self):
        self.users = []

    def find_all(self):
        return self.users

    def create(self, dto: CreateUserDto):
        user = {"id": len(self.users) + 1, "name": dto.name}
        self.users.append(user)
        return user


@Controller("users")
class UsersController:
    def __init__(self, users_service: UsersService):
        self.users_service = users_service

    @Get("/")
    async def find_all(self):
        return self.users_service.find_all()

    @Post("/")
    async def create(self, dto: CreateUserDto = Body()):
        return self.users_service.create(dto)


@Module(controllers=[UsersController], providers=[UsersService])
class AppModule:
    pass


app = FaNestFactory.create(AppModule)
```

## All Uses Example

The fuller example lives in [examples/all_uses/main.py](/Users/foysal/fanest/examples/all_uses/main.py).

It shows:

- REST controllers
- Pydantic DTOs
- constructor injection
- custom provider tokens
- config module
- JWT auth
- role guards
- cache interceptor
- throttling guard
- Swagger document setup
- health endpoint
- SQLAlchemy module wiring
- interval jobs
- cron jobs
- WebSocket gateway
- global prefix, CORS, and global pipes

Run it:

```bash
uv run uvicorn examples.all_uses.main:app --reload
```

Useful paths:

```txt
GET  /api/users
POST /api/users
POST /api/users/login
GET  /api/admin/me
GET  /api/health
GET  /api/docs
WS   /api/chat
```

## CLI

```bash
fanest new blog-api
fanest start main:app --reload

fanest generate resource users
fanest generate module users
fanest generate controller users
fanest generate service users
fanest generate guard auth
fanest generate pipe validation
fanest generate interceptor logging
fanest generate filter http_error
fanest generate gateway chat
```

## Core Features

FaNest currently includes:

- `@Module`
- `@Controller`
- `@Injectable`
- `@Get`, `@Post`, `@Put`, `@Patch`, `@Delete`
- `Body`, `Param`, `Query`, `Header`, `Req`
- `UseGuards`
- `UsePipes`
- `UseInterceptors`
- `UseFilters`
- `WebSocketGateway`
- `SubscribeMessage`
- `Interval`
- `Cron`
- lifecycle hooks: `on_module_init`, `on_application_shutdown`

## Dependency Injection

FaNest supports class providers and Nest-style provider definitions:

```python
from fanest import Inject, Injectable, Module, token, use_factory, use_value

CONFIG = token("CONFIG")
MESSAGE = token("MESSAGE")


@Injectable()
class MessageService:
    def __init__(self, message: str = Inject(MESSAGE)):
        self.message = message


@Module(
    providers=[
        MessageService,
        use_value(CONFIG, {"message": "hello"}),
        use_factory(MESSAGE, lambda config: config["message"], inject=[CONFIG]),
    ],
)
class MessageModule:
    pass
```

Supported provider types:

- class providers
- value providers
- factory providers
- existing provider aliases
- injection tokens
- optional injection
- provider overrides in tests

## Packages

The repository already contains first-party package starts:

```txt
fanest.core              modules, DI, scanner, app factory
fanest.common            decorators, exceptions, pipes
fanest.platform_fastapi  FastAPI adapter
fanest.config            ConfigModule and ConfigService
fanest.swagger           decorators, DocumentBuilder, SwaggerModule
fanest.auth              JWT service, auth guard, roles guard
fanest.sqlalchemy        async SQLAlchemy module and repositories
fanest.cache             cache service and cache interceptor
fanest.throttler         throttling module and guard
fanest.schedule          interval and cron jobs
fanest.health            health endpoint module
fanest.testing           TestingModule and provider overrides
```

## Built-In Pipes And Exceptions

Pipes:

- `ValidationPipe`
- `ParseIntPipe`
- `ParseBoolPipe`
- `DefaultValuePipe`

Exceptions:

- `BadRequestException`
- `UnauthorizedException`
- `ForbiddenException`
- `NotFoundException`
- `ConflictException`
- `InternalServerErrorException`

## Swagger

```python
from fanest.swagger import DocumentBuilder, SwaggerModule

config = (
    DocumentBuilder()
    .set_title("Blog API")
    .set_description("A FaNest application")
    .set_version("1.0.0")
    .add_bearer_auth()
    .build()
)

document = SwaggerModule.create_document(app, config)
SwaggerModule.setup("/docs", app, document)
```

## Testing

```python
from fanest.testing import TestingModule

app = (
    TestingModule.create(AppModule)
    .override_provider(UsersService, MockUsersService())
    .compile()
)
```

Run the test suite:

```bash
uv run pytest
uv run ruff check .
```

## NestJS Parity

FaNest is aiming at the full NestJS surface area. The current implementation covers the core application model and many common packages, but some large systems still need deeper work.

Current:

- modules, controllers, providers
- DI with custom providers
- REST decorators
- request binding
- guards, pipes, interceptors, filters
- Swagger helpers
- JWT auth and roles
- cache and throttling
- WebSocket gateways
- cron and interval jobs
- SQLAlchemy package start
- health checks
- testing utilities
- CLI generators

Still to deepen:

- request-scoped and transient providers
- `forwardRef` circular dependency handling
- richer module export enforcement
- middleware consumer API
- file upload helpers
- response serialization decorators
- full mapped types package
- GraphQL module
- microservice transports
- queues
- mailer
- advanced SQLAlchemy migrations/templates
- MongoDB package
- CLI auto-registration into modules
- workspace/monorepo mode

The plan is to keep closing that gap package by package, without losing the Python feel.

## Repository Status

This is an early framework build, but it is runnable and tested.

```bash
uv run pytest
# 15 passed
```

## License

MIT
