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
- mapped DTO helpers
- constructor injection
- custom provider tokens
- middleware
- file uploads
- file validation pipes
- response headers, SSE, and streaming files
- rendered templates and static assets
- session cookies and security headers
- custom parameter decorators
- config module
- config validation helpers
- async module configuration
- i18n translations and locale extraction
- JWT auth
- Passport-style auth strategies
- role guards
- cache interceptor
- cache stores
- throttling guard
- Swagger document setup, security schemes, and TypeScript client generation
- typed exception filters
- Reflector and discovery services
- health indicators
- metrics counters
- worker task handlers
- GraphQL resolvers
- health endpoint
- SQLAlchemy module wiring
- Mongo-style document collections
- Mongoose-style module aliases
- interval jobs
- cron jobs
- timeout jobs and scheduler registry
- queue processors
- Bull-style queue aliases
- mailer service
- CQRS command/query/event buses
- event emitter wildcard, once, and off helpers
- named microservice transports
- WebSocket gateway with rooms, broadcasting, guards, pipes, filters, and Socket.IO-style emitters
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
fanest workspace acme-platform
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
fanest generate dto users
fanest generate middleware request_id
fanest generate decorator current_user
fanest generate library common
fanest generate resource users --dry-run
fanest generate module users --module app_module.py
```

## Core Features

FaNest currently includes:

- `@Module`
- `@Controller`
- `@Injectable`
- `@Get`, `@Post`, `@Put`, `@Patch`, `@Delete`, `@Options`, `@Head`, `@All`
- `Body`, `Param`, `Query`, `Header`, `Cookie`, `Req`, `Res`, `Ip`, `Session`
- `UploadedFile`, `UploadedFiles`, `BackgroundTasks`, custom param decorators
- `HttpCode`, `Redirect`, `SetHeader`, `SetMetadata`, `Version`, `ResponseModel`
- `Sse` and `StreamableFile`
- `UseGuards`
- `UsePipes`
- `UseInterceptors`
- `UseFilters`
- `WebSocketGateway`
- `SubscribeMessage`
- `Interval`
- `Cron`
- `Timeout`
- `Global`
- `MessagePattern`
- `EventPattern`
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
- async factory providers
- existing provider aliases
- injection tokens
- optional injection
- singleton, request, and transient scopes
- `forward_ref`
- global modules
- provider overrides in tests

## Packages

The repository already contains first-party package starts:

```txt
fanest.core              modules, DI, scanner, app factory
fanest.common            decorators, exceptions, pipes
fanest.platform_fastapi  FastAPI adapter
fanest.session           signed cookie sessions
fanest.security          helmet-style security headers
fanest.i18n              translations and I18nLang helper
fanest.config            ConfigModule and ConfigService
fanest.swagger           decorators, DocumentBuilder, SwaggerModule
fanest.auth              JWT service, passport strategies, auth guard, roles guard
fanest.sqlalchemy        async SQLAlchemy module and repositories
fanest.mongodb           Mongo/Mongoose-style document service and collections
fanest.cache             cache service, interceptor, and store adapters
fanest.throttler         throttling module and guard
fanest.schedule          interval, cron, timeout jobs, scheduler registry
fanest.websockets        connection manager, rooms, broadcasting
fanest.serve_static      static asset module
fanest.queues            QueueModule/BullModule, processors, jobs
fanest.mailer            mail service with outbox and SMTP handoff
fanest.cqrs              command, query, and event buses
fanest.events            event emitter and OnEvent decorators
fanest.graphql           resolvers, queries, mutations, GraphQL endpoint
fanest.microservices     message/event patterns and named transports
fanest.mapped_types      PartialType, PickType, OmitType, IntersectionType
fanest.health            health endpoint module
fanest.metrics           counters and metrics endpoint
fanest.workers           task handler registry
fanest.discovery/core    Reflector and DiscoveryService
fanest.testing           TestingModule and provider overrides
```

## Built-In Pipes And Exceptions

Pipes:

- `ValidationPipe`
- `ParseIntPipe`
- `ParseBoolPipe`
- `ParseFloatPipe`
- `ParseUUIDPipe`
- `ParseEnumPipe`
- `ParseArrayPipe`
- `DefaultValuePipe`
- `ParseFilePipe`
- `MaxFileSizeValidator`
- `FileTypeValidator`

Exceptions:

- `BadRequestException`
- `UnauthorizedException`
- `ForbiddenException`
- `NotFoundException`
- `ConflictException`
- `InternalServerErrorException`
- `UnprocessableEntityException`
- `TooManyRequestsException`
- `ServiceUnavailableException`

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

Swagger decorators include `ApiTags`, `ApiOperation`, `ApiParam`, `ApiQuery`, `ApiHeader`,
`ApiBody`, `ApiResponse`, `ApiConsumes`, `ApiProduces`, `ApiBearerAuth`, `ApiBasicAuth`,
`ApiCookieAuth`, `ApiSecurity`, response shortcuts such as `ApiOkResponse`,
`ApiCreatedResponse`, `ApiNotFoundResponse`, `ApiExcludeEndpoint`, and `ApiProperty`.
`SwaggerModule.generate_typescript_client(document)` can emit a small fetch client from the
generated OpenAPI document.

## Mapped Types

```python
from fanest import PartialType, PickType

UpdateUserDto = PartialType(CreateUserDto)
PublicUserDto = PickType(UserDto, ["id", "name"])
```

## Middleware

```python
class RequestIdMiddleware:
    async def use(self, request, call_next):
        response = await call_next(request)
        response.headers["x-request-id"] = "local"
        return response


@Module(controllers=[UsersController], middlewares=[RequestIdMiddleware])
class AppModule:
    pass
```

Modules can also expose a Nest-style `configure(consumer)` method for route-scoped
middleware with exclusions:

```python
class AppModule:
    def configure(self, consumer):
        consumer.apply(RequestIdMiddleware).exclude("/health").for_routes("/users*")
```

## Microservices

```python
from fanest.microservices import MessagePattern, MicroserviceServer


class MathService:
    @MessagePattern("math.double")
    async def double(self, data, context):
        return data * 2


server = MicroserviceServer(AppModule).compile()
client = server.client()
result = await client.send("math.double", 21)
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
- DI with custom, async, scoped, optional, and aliased providers
- global modules and exported module boundaries
- REST decorators
- request binding
- versioned routes, status codes, redirects, response headers, SSE, streaming files
- rendered templates and static asset module
- signed sessions and security headers
- middleware
- route-scoped middleware with exclusions
- file upload binding
- file validation
- custom param decorators
- mapped DTO helpers
- response serialization
- guards, pipes, interceptors, filters
- `@Catch` typed exception filters
- Reflector and DiscoveryService
- Swagger helpers and security schemes
- JWT auth and roles
- Passport-style strategy guards
- cache and throttling
- WebSocket gateways, Socket.IO-style room emitters, guards, pipes, and filters
- cron, interval, timeout jobs, and scheduler registry
- in-memory queue processors
- queue retries and delayed jobs
- BullModule and InjectQueue aliases
- mailer package with templates
- CQRS package
- event emitter wildcard/once/off helpers
- microservice message/event patterns and named transports
- lightweight GraphQL module
- SQLAlchemy package start
- migration template helper
- Mongo-style package start
- MongooseModule and InjectModel aliases
- i18n package
- cache store adapters
- health checks
- health indicators
- metrics module
- worker task handlers
- testing utilities
- CLI generators
- workspace and library CLI commands

Still to deepen:

- Redis-backed queue transport
- advanced SMTP provider adapters
- full migration runner
- CLI auto-registration into modules
- workspace/monorepo mode

The plan is to keep closing that gap package by package, without losing the Python feel.

## Repository Status

This is an early framework build, but it is runnable and tested.

```bash
uv run pytest
# 85 passed
```

## License

MIT
