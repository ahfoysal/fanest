# FaNest Complete Feature Gap Analysis

This document compares FaNest against two targets:

1. **NestJS parity**: because FaNest is intended to feel familiar to NestJS developers.
2. **FastAPI parity**: because FaNest uses FastAPI underneath and should not hide important FastAPI capabilities from Python users.

The goal is not to copy every implementation detail. The goal is to identify what prevents FaNest from becoming a complete Python backend framework with a Nest-like workflow.

## Research Sources

Primary references used:

- NestJS official docs: fundamentals, custom providers, dynamic modules, injection scopes, lifecycle events, circular dependencies, ModuleRef, discovery service, testing.
- NestJS official docs: guards, interceptors, OpenAPI/Swagger, configuration, caching, validation, serialization, file upload, versioning, task scheduling, queues, microservices, GraphQL, WebSockets, rate limiting.
- FastAPI official docs: core features, dependencies, security, CORS, middleware, background tasks, WebSockets, request files, custom responses, response models, dependency overrides.

Useful official pages:

- https://docs.nestjs.com/fundamentals/custom-providers
- https://docs.nestjs.com/fundamentals/dynamic-modules
- https://docs.nestjs.com/fundamentals/injection-scopes
- https://docs.nestjs.com/fundamentals/circular-dependency
- https://docs.nestjs.com/fundamentals/module-ref
- https://docs.nestjs.com/fundamentals/lifecycle-events
- https://docs.nestjs.com/fundamentals/discovery-service
- https://docs.nestjs.com/fundamentals/testing
- https://docs.nestjs.com/guards
- https://docs.nestjs.com/interceptors
- https://docs.nestjs.com/openapi/introduction
- https://docs.nestjs.com/techniques/configuration
- https://docs.nestjs.com/techniques/caching
- https://docs.nestjs.com/techniques/validation
- https://docs.nestjs.com/techniques/serialization
- https://docs.nestjs.com/techniques/file-upload
- https://docs.nestjs.com/techniques/versioning
- https://docs.nestjs.com/techniques/task-scheduling
- https://docs.nestjs.com/techniques/queues
- https://docs.nestjs.com/microservices/basics
- https://docs.nestjs.com/graphql/quick-start
- https://docs.nestjs.com/security/rate-limiting
- https://fastapi.tiangolo.com/
- https://fastapi.tiangolo.com/reference/dependencies/
- https://fastapi.tiangolo.com/tutorial/security/
- https://fastapi.tiangolo.com/reference/security/
- https://fastapi.tiangolo.com/tutorial/cors/
- https://fastapi.tiangolo.com/tutorial/middleware/
- https://fastapi.tiangolo.com/tutorial/background-tasks/
- https://fastapi.tiangolo.com/advanced/websockets/
- https://fastapi.tiangolo.com/tutorial/request-files/
- https://fastapi.tiangolo.com/advanced/custom-response/
- https://fastapi.tiangolo.com/tutorial/response-model/
- https://fastapi.tiangolo.com/advanced/testing-dependencies/

## Current FaNest Snapshot

FaNest currently has:

- module/controller/provider decorators
- class/value/factory/existing providers
- injection tokens and optional injection
- singleton/request/transient provider scopes
- module export validation groundwork
- app factory and `FaNestApplication`
- HTTP route decorators
- body/path/query/header/cookie/request/response/file/custom parameter binding
- guards, pipes, interceptors, filters
- built-in validation and parse pipes
- built-in HTTP exceptions
- middleware support
- CORS support
- Swagger helpers
- JWT auth, roles guard, current user decorator
- config module
- cache module
- throttler module
- health module
- scheduler with interval and cron via `croniter`
- WebSocket gateway basics
- in-memory microservice transport
- SQLAlchemy module and repository token helpers
- mapped DTO helpers
- class serializer interceptor
- testing module with provider overrides
- CLI generators

This is a strong early framework, but it is not complete yet.

## Executive Summary

FaNest is missing depth in five places that matter more than adding more decorator names:

1. **True module container architecture**
   FaNest validates exports, but still has a mostly global runtime container. Nest has module-aware dependency lookup, `ModuleRef`, global modules, dynamic modules, and lazy loading.

2. **Async provider lifecycle**
   Real frameworks need async factories, startup initialization, graceful shutdown, and resource cleanup for DB clients, Redis, queues, brokers, telemetry, and config.

3. **Production adapters and package depth**
   Packages exist, but many are shallow: Swagger, SQLAlchemy, cache, microservices, auth, config, testing, and CLI all need deeper behavior.

4. **FastAPI feature passthrough**
   FaNest should expose or wrap FastAPI’s strongest features: dependency overrides, response models, background tasks, security schemes, streaming/file responses, static files, forms, templating, and OpenAPI controls.

5. **Developer experience**
   CLI auto-wiring, project scaffolding, generated tests, docs, examples, and issue templates are still early.

## Priority Legend

- **P0**: Framework blocker. Without this, FaNest feels incomplete or unsafe.
- **P1**: Important for serious apps.
- **P2**: Useful package depth.
- **P3**: Nice to have or later ecosystem feature.

Status:

- **Done**: implemented and tested.
- **Partial**: exists, but shallow or missing key behavior.
- **Missing**: not implemented.

## 1. Core Framework / DI / Modules

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Modules | Yes | No | Partial | P0 | Decorator exists; runtime module container is still not fully module-scoped. |
| Controllers | Yes | Path ops | Done | P0 | Class controllers work. |
| Providers/services | Yes | Dependencies | Done | P0 | Constructor DI works. |
| Custom providers | Yes | Dependencies | Done | P0 | `use_value`, `use_class`, `use_factory`, `use_existing`. |
| Injection tokens | Yes | Callable deps | Done | P0 | `token()` and `Inject()`. |
| Optional injection | Yes | Optional deps | Done | P1 | Implemented. |
| Singleton scope | Yes | Dependency cache | Done | P0 | Default. |
| Request scope | Yes | Request deps | Done | P0 | Uses `ContextVar`. |
| Transient scope | Yes | `use_cache=False` | Done | P1 | Implemented. |
| Module exports enforcement | Yes | No | Partial | P0 | Validation exists; actual runtime lookup still global. |
| Module-local containers | Yes | No | Missing | P0 | Needed for true Nest-like boundaries. |
| Global modules | Yes | App deps | Missing | P1 | Needed for config/auth/shared infra. |
| Dynamic modules | Yes | Factories | Partial | P0 | `for_root` patterns exist, but no generic dynamic module type. |
| Async providers | Yes | Async deps | Missing | P0 | Needed for Redis, DB, queues, config, brokers. |
| `forRootAsync` / `registerAsync` | Yes | Async deps | Missing | P0 | Needed for config-driven package setup. |
| `forwardRef` | Yes | No | Missing | P1 | Needed for circular module/provider refs. |
| `ModuleRef` | Yes | App container access | Missing | P1 | Needed for dynamic resolution. |
| Discovery service | Yes | No | Missing | P2 | Useful for plugins, decorators, scheduled jobs, event handlers. |
| Lazy modules | Yes | Routers imported lazily | Missing | P3 | Later optimization. |
| Lifecycle hooks | Yes | Lifespan | Partial | P1 | Providers get startup/shutdown; modules/controllers/gateways not complete. |
| Shutdown hooks | Yes | Lifespan | Partial | P1 | Needs explicit cleanup hooks and signal behavior. |

### Core Fix Plan

1. Build `ModuleCompiler` and `ModuleContainer`.
2. Resolve providers by module context, not only global token.
3. Add `GlobalModule` / `is_global`.
4. Add async provider support.
5. Add `ModuleRef.get()` and `ModuleRef.resolve()`.
6. Add `forward_ref()`.

## 2. Application / Platform Layer

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Application object | Yes | FastAPI app | Partial | P0 | `FaNestApplication` exists but thin. |
| `listen()` | Yes | Uvicorn/server | Partial | P1 | Exists, but basic. |
| Global prefix | Yes | Router prefix | Done | P1 | Works. |
| Global pipes/guards/interceptors/filters | Yes | Dependencies/middleware | Done | P1 | Works. |
| Enable CORS | Yes | Middleware | Done | P1 | Works. |
| Versioning | Yes | Manual/router | Missing | P1 | URI/header/media-type/custom versioning needed. |
| HTTP adapter interface | Yes | Starlette/FastAPI | Missing | P1 | FaNest is tightly coupled to FastAPI adapter. |
| Platform agnosticism | Yes | No | Missing | P2 | Later: Starlette/FastAPI are enough now. |
| Static assets | Yes | StaticFiles | Missing | P2 | FastAPI supports via Starlette. |
| Templates / MVC views | Yes | Jinja2 possible | Missing | P3 | Not urgent for API-first framework. |
| Compression | Yes | Middleware | Missing | P2 | GZip middleware wrapper. |
| Sessions | Yes | Middleware | Missing | P2 | Starlette SessionMiddleware wrapper. |
| Cookies | Yes | Response/cookie | Partial | P1 | Cookie param exists; response cookie helpers missing. |

### Platform Fix Plan

1. Add route versioning.
2. Add static assets module.
3. Add compression/session modules.
4. Add response helper decorators for cookies/headers.
5. Introduce internal `HttpAdapter` protocol.

## 3. HTTP Routing / Request Pipeline

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Method decorators | Yes | Yes | Done | P0 | GET/POST/PUT/PATCH/DELETE. |
| Status code decorator | Yes | Route option | Done | P1 | `HttpCode`. |
| Redirect decorator | Yes | RedirectResponse | Done | P1 | Basic support. |
| Header decorator | Yes | Response headers | Partial | P1 | Request `Header()` exists; response header decorator missing. |
| Param/query/body binding | Yes | Yes | Done | P0 | Works. |
| Cookie binding | Yes | Yes | Done | P1 | Works. |
| File upload binding | Yes | Yes | Partial | P1 | `UploadedFile`, `UploadedFiles`; no validation/storage interceptors. |
| Form binding | Yes-ish | Yes | Missing | P1 | Need `Form()` param decorator. |
| Request/response injection | Yes | Yes | Done | P1 | `Req`, `Res`. |
| Custom param decorators | Yes | No direct | Done | P1 | `create_param_decorator`. |
| Middleware | Yes | Yes | Partial | P0 | Module middleware works; no route consumer/exclusions. |
| Guards | Yes | Security deps | Done | P0 | Works; no `@Public` yet. |
| Pipes | Yes | Validation/type hints | Partial | P0 | Many pipes exist; options shallow. |
| Interceptors | Yes | Middleware/deps | Done | P0 | Basic chain works. |
| Exception filters | Yes | Exception handlers | Partial | P0 | Works; filter chain behavior could be richer. |
| Execution context | Yes | Request context | Partial | P1 | Needs host switching for HTTP/WS/RPC. |
| SSE | Yes | StreamingResponse | Missing | P1 | Important FastAPI/Nest feature. |
| Streaming responses | Yes | StreamingResponse | Missing | P1 | Needed for file/stream APIs. |
| File responses | Yes | FileResponse | Missing | P1 | Needed for downloads. |
| Background tasks | No direct equivalent | Yes | Missing | P1 | Important FastAPI feature. |
| Response model filtering | Swagger/DTO | Yes | Missing | P1 | FastAPI response_model capability not wrapped. |

### HTTP Fix Plan

1. Add `Form()`, `BackgroundTasks()`, `Sse()`, `StreamableFile`.
2. Add response decorators: `SetHeader`, `SetCookie`, `ClearCookie`.
3. Add route versioning.
4. Add middleware consumer API: `for_routes`, `exclude`.
5. Add `@Public()` and global-auth skip.

## 4. Validation / Pipes / Serialization / DTOs

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Validation pipe | Yes | Pydantic | Done | P0 | Uses Pydantic. |
| Transform payloads | Yes | Pydantic | Partial | P1 | Basic transform; options shallow. |
| Whitelist/forbid extra | Yes | Pydantic config | Missing | P1 | Should be ValidationPipe options. |
| ParseInt/Bool/Float/UUID/Enum/Array | Yes | Type hints | Done | P1 | Implemented. |
| DTO mapped types | Yes | Pydantic create_model | Partial | P1 | Helpers exist; metadata/schema depth shallow. |
| Serialization interceptor | Yes | Pydantic dump | Partial | P1 | Basic include/exclude. |
| `@Expose` / `@Exclude` | Yes | Pydantic config/serializers | Missing | P2 | Needed for field-level serialization. |
| Response models | Swagger/DTO | Yes | Missing | P1 | Need route option wrapper. |
| Custom validators | class-validator | Pydantic | Native via Pydantic | P1 | Users can use Pydantic. |

### DTO Fix Plan

1. Add `ValidationPipe` options: whitelist, forbid_extra, transform.
2. Add `ResponseModel()` route decorator.
3. Improve mapped types to preserve field metadata.
4. Add serializer field include/exclude conventions.

## 5. Swagger / OpenAPI

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Swagger module setup | Yes | Built-in docs | Done | P1 | `SwaggerModule.setup`. |
| Document builder | Yes | OpenAPI dict | Done | P1 | Basic. |
| Tags | Yes | Route tags | Done | P1 | Works. |
| Operation summary/description | Yes | Route metadata | Done | P1 | Works. |
| Response decorator | Yes | Route responses | Partial | P1 | Basic. |
| Bearer auth | Yes | Security schemes | Partial | P1 | Operation security now wired; scheme needs more options. |
| Params/query/body decorators | Yes | OpenAPI | Partial | P1 | Basic param/query; body schema shallow. |
| `ApiProperty` | Yes | Pydantic schema | Missing | P1 | Need field metadata helper or Pydantic guidance. |
| `ApiHideProperty` | Yes | Pydantic schema | Missing | P2 | Useful. |
| Multiple documents | Yes | Multiple schemas | Partial | P2 | Possible manually, no include filters. |
| Swagger UI options | Yes | FastAPI docs options | Missing | P2 | Needed for polish. |

### Swagger Fix Plan

1. Add `ApiProperty`, `ApiHideProperty`, `ApiExtraModels`.
2. Add `ApiBody` schema support.
3. Add security scheme options.
4. Add document include/exclude route filters.

## 6. Auth / Security

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| JWT service | Package | Manual/security | Done | P1 | Instance-configured now. |
| JWT guard | Passport strategy | Security dep | Done | P1 | Works. |
| Roles guard | Common pattern | Manual | Done | P1 | Works. |
| `@Public()` | Common pattern | Optional deps | Missing | P0 | Needed for global auth. |
| Refresh tokens | App pattern | Manual | Missing | P1 | Needed for auth module depth. |
| Passport strategies | Yes | Security classes | Missing | P1 | Local/basic/API-key/OAuth2 strategies. |
| OAuth2 password flow | Passport/FastAPI tools | Yes | Missing | P1 | FastAPI has security utilities; FaNest should wrap. |
| API key auth | Yes | Yes | Missing | P1 | Header/query/cookie API keys. |
| Basic auth | Yes | Yes | Missing | P2 | Useful. |
| CSRF | Middleware/package | Manual | Missing | P2 | Web apps. |
| Helmet/security headers | Helmet | Middleware | Missing | P2 | Security headers module. |
| Rate limiting | Throttler | Middleware | Partial | P1 | Basic in-memory throttler. |

### Auth Fix Plan

1. Add `@Public()`.
2. Add strategy abstraction: `AuthStrategy`, `AuthGuard(strategy)`.
3. Add API key, basic auth, OAuth2 password helpers.
4. Add refresh token helper service.
5. Add security headers module.

## 7. Config

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| `.env` loading | Yes | Pydantic settings | Done | P1 | Basic parser. |
| Pydantic schema validation | Joi/class | Pydantic | Missing | P0 | Needed for production confidence. |
| Typed get/coercion | Yes | Pydantic | Missing | P1 | Currently strings. |
| Config namespaces | Yes | Settings models | Missing | P1 | `register_as` equivalent. |
| Global config module | Yes | App-level | Missing | P1 | Useful after global modules. |
| `forRootAsync` | Yes | Async deps | Missing | P0 | Needs async provider support. |
| Multiple env files | Yes | Pydantic settings | Missing | P2 | Useful. |

### Config Fix Plan

1. Add Pydantic settings schema support.
2. Add typed `get(key, cast=...)`.
3. Add namespaces.
4. Add global module support.

## 8. Database / ORM

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| SQLAlchemy module | TypeORM/Prisma analog | Common | Partial | P0 | Engine/session/repository helpers exist. |
| `for_feature` | Yes | Manual | Partial | P1 | Repository token support exists. |
| Transactions | Yes/manual | Manual | Missing | P0 | Needs `@Transactional` or unit-of-work. |
| Request-scoped sessions | Yes/manual | Dependencies | Missing | P0 | Critical for DB apps. |
| Alembic integration | TypeORM migrations | Alembic | Missing | P1 | CLI and config helpers. |
| Pagination helpers | Common | Manual | Missing | P2 | Useful. |
| MongoDB package | Mongoose | Motor/Beanie | Missing | P1 | Expected from Nest parity. |
| Prisma-like package | Prisma | Prisma Python/SQLAlchemy | Missing | P3 | Optional. |

### DB Fix Plan

1. Add request-scoped session provider.
2. Add transaction decorator/context manager.
3. Add repository base methods with pagination.
4. Add Alembic scaffold commands.
5. Add Mongo/Beanie module later.

## 9. Cache

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Cache module | Yes | Manual | Partial | P1 | In-memory only. |
| Cache interceptor | Yes | Manual | Done | P1 | Basic. |
| TTL decorator | Yes | Manual | Done | P1 | `CacheTTL`. |
| Cache key decorator | Yes | Manual | Missing | P1 | Need `CacheKey`. |
| Cache evict decorator | Common | Manual | Missing | P1 | Needed for invalidation. |
| Redis adapter | Yes via stores | Common | Missing | P1 | Needed for production. |
| LRU/size limit | Store-dependent | Manual | Missing | P2 | Prevent memory growth. |

### Cache Fix Plan

1. Add `CacheKey`, `CacheEvict`.
2. Add store interface.
3. Add Redis store.
4. Add max-size/LRU memory store.

## 10. Scheduling / Jobs / Queues

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Cron | Yes | Manual | Done | P1 | Now uses `croniter`. |
| Interval | Yes | Manual | Done | P1 | Works. |
| Timeout | Yes | Manual | Missing | P2 | Add `Timeout`. |
| Dynamic job registry | Yes | Manual | Missing | P1 | Need add/remove/list jobs. |
| Distributed locking | No built-in | Manual | Missing | P2 | Production cron. |
| Queues | Bull/BullMQ | Celery/RQ/Arq | Missing | P1 | Big missing package. |
| Background tasks | No exact | Yes | Missing | P1 | FastAPI feature; add wrapper. |

### Jobs Fix Plan

1. Add `Timeout`.
2. Add scheduler registry.
3. Add background task param/decorator.
4. Add queue package with Arq/RQ/Celery adapter.

## 11. Microservices / Events

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| MessagePattern | Yes | No | Done | P1 | In-memory only. |
| EventPattern | Yes | No | Done | P1 | In-memory only. |
| ClientProxy | Yes | No | Partial | P1 | Basic. |
| Redis transport | Yes | External | Missing | P1 | Needed. |
| NATS transport | Yes | External | Missing | P1 | Needed. |
| RabbitMQ transport | Yes | External | Missing | P1 | Needed. |
| Kafka transport | Yes | External | Missing | P1 | Needed. |
| gRPC transport | Yes | External | Missing | P1 | Needed. |
| Timeouts/retries | Yes | Client config | Missing | P1 | Needed. |
| Hybrid HTTP + microservice app | Yes | Manual | Missing | P1 | `connect_microservice`. |

### Microservice Fix Plan

1. Connect microservices to `FaNestApplication`.
2. Add timeout/retry options.
3. Add Redis transport first.
4. Add NATS/Rabbit/Kafka/gRPC later.

## 12. WebSockets

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Gateway decorator | Yes | Manual | Done | P1 | Works. |
| SubscribeMessage | Yes | Manual | Done | P1 | Works. |
| Connect/disconnect hooks | Yes | Manual | Partial | P1 | Supported but lightly tested. |
| Guards/pipes/interceptors for gateways | Yes | Manual | Missing | P1 | Needed for parity. |
| Rooms/namespaces | Socket.IO | Manual | Missing | P1 | Important. |
| Broadcast manager | Yes | Manual | Missing | P1 | Important. |
| Binary/text frames | Yes | Yes | Missing | P2 | JSON only now. |
| WebSocket DI context | Yes | Manual | Partial | P1 | Needs execution context. |

### WebSocket Fix Plan

1. Add connection manager.
2. Add rooms/broadcasts.
3. Add gateway guards/pipes/interceptors.
4. Add binary/text payload support.

## 13. GraphQL

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| GraphQL module | Yes | Strawberry/Ariadne | Missing | P1 | Big missing package. |
| Resolver decorators | Yes | Strawberry style | Missing | P1 | `Query`, `Mutation`, `Resolver`. |
| Subscriptions | Yes | Supported by libs | Missing | P2 | Later. |
| Guards/interceptors for resolvers | Yes | Manual | Missing | P1 | Needs execution context. |
| Schema generation | Yes | Lib-dependent | Missing | P1 | Choose Strawberry first. |
| DataLoader support | Common | External | Missing | P2 | Important for production GraphQL. |

### GraphQL Fix Plan

1. Add Strawberry-based `GraphQLModule`.
2. Add resolver registration.
3. Add guards/pipes via execution context.
4. Add DataLoader helpers.

## 14. Testing

| Capability | NestJS | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| Testing module | Yes | TestClient | Partial | P1 | Compile works. |
| Override provider | Yes | Dependency overrides | Done | P1 | Works. |
| `get()` provider | Yes | app state | Missing | P1 | Needed. |
| `resolve()` scoped provider | Yes | Dependency context | Missing | P1 | Needed. |
| E2E test app helpers | Yes | TestClient | Missing | P2 | Useful. |
| Mock helper | No built-in? | unittest.mock | Missing | P2 | Nice. |
| Dependency override bridge | No | Yes | Missing | P2 | Should expose FastAPI overrides if needed. |

### Testing Fix Plan

1. Add `TestingModule.get(token)`.
2. Add `TestingModule.resolve(token)`.
3. Add `create_testing_client`.
4. Add mock helpers.

## 15. CLI / Project Generation

| Capability | NestJS CLI | FastAPI | FaNest | Priority | Notes |
|---|---:|---:|---:|---:|---|
| New project | Yes | No | Partial | P0 | Needs full scaffold. |
| Generate module/controller/service | Yes | No | Done | P1 | Basic. |
| Generate resource | Yes | No | Done | P1 | Basic. |
| Generate guard/pipe/interceptor/filter/gateway | Yes | No | Done | P1 | Basic. |
| Generate middleware | Yes | No | Missing | P1 | Need. |
| Generate DTO/entity | Yes | No | Missing | P1 | Need. |
| Auto-register generated artifacts | Yes | No | Missing | P0 | Major DX gap. |
| Dry run | Yes | No | Missing | P2 | Useful. |
| No spec flag | Yes | No | Missing | P2 | Useful. |
| Workspace/monorepo | Yes | No | Missing | P2 | Later. |
| Build command | Yes | No | Missing | P2 | Python packaging/build helper. |
| Info command | Yes | No | Missing | P2 | Useful. |

### CLI Fix Plan

1. Scaffold complete `pyproject.toml`, tests, app module.
2. Add module auto-wiring.
3. Add DTO/entity/middleware/decorator generators.
4. Add dry-run.

## 16. FastAPI Feature Gap

FaNest should not only chase Nest. It should also expose the best FastAPI features.

| FastAPI Feature | FaNest Status | Priority | Notes |
|---|---:|---:|---|
| Type-hint validation | Done | P0 | Through FastAPI/Pydantic. |
| Dependency injection `Depends` | Missing bridge | P1 | FaNest DI is separate; bridge needed. |
| Security utilities | Missing | P1 | OAuth2/APIKey/HTTPBasic wrappers. |
| Response models | Missing | P1 | Add route decorator. |
| Background tasks | Missing | P1 | Add param decorator or service. |
| Middleware | Partial | P1 | Module middleware exists. |
| CORS | Done | P1 | Works. |
| Static files | Missing | P2 | Starlette feature wrapper. |
| Templates | Missing | P3 | Lower priority. |
| Forms | Missing | P1 | Add `Form`. |
| File uploads | Partial | P1 | Basic only. |
| Custom responses | Partial | P1 | Redirect only; need HTML/File/Streaming. |
| StreamingResponse | Missing | P1 | Needed. |
| WebSockets | Partial | P1 | Gateway abstraction basic. |
| Dependency overrides for testing | Partial | P1 | Provider overrides; no FastAPI override bridge. |
| OpenAPI customization | Partial | P1 | Swagger module needs depth. |
| Lifespan | Done | P1 | Used internally. |
| Mounted sub-apps | Missing | P2 | Useful for admin/static/sub apps. |

## 17. Package Hygiene

Current dependencies are heavy for a core framework:

- SQLAlchemy
- PyJWT
- python-multipart
- aiosqlite
- croniter

For a polished package, split optional extras:

```toml
fanest[auth]
fanest[sqlalchemy]
fanest[files]
fanest[schedule]
fanest[all]
```

Priority: **P1** before publishing.

## 18. Recommended Build Order

### Phase A: Make The Foundation Real

1. Module-local runtime container.
2. Async providers and `for_root_async`.
3. `ModuleRef`.
4. `forward_ref`.
5. Global modules.

### Phase B: Complete HTTP/FastAPI Surface

1. `@Public`.
2. Form and background task binding.
3. Response model decorator.
4. Streaming/File/HTML responses.
5. Versioning.
6. Middleware consumer API.

### Phase C: Deepen Common Packages

1. Pydantic config module.
2. SQLAlchemy transactions/request sessions.
3. Cache store interface + Redis.
4. Swagger properties/body/security.
5. TestingModule `get`/`resolve`.

### Phase D: Big Ecosystem Packages

1. GraphQL module.
2. Queue module.
3. Redis microservice transport.
4. NATS/RabbitMQ/Kafka/gRPC transports.
5. MongoDB/Beanie module.

### Phase E: Developer Experience

1. CLI full scaffold.
2. CLI auto-wiring.
3. Docs site.
4. More examples.
5. Benchmarks.
6. Release automation.

## 19. Immediate GitHub Issues To Create

1. Implement module-local runtime container.
2. Add async providers and `for_root_async`.
3. Add `@Public()` and global auth skip.
4. Add Pydantic config schema validation.
5. Add `TestingModule.get()` and `.resolve()`.
6. Add `ResponseModel`, `Form`, and background task decorators.
7. Add SQLAlchemy request-scoped sessions and transaction decorator.
8. Add `CacheKey`, `CacheEvict`, and Redis cache store.
9. Add Swagger `ApiProperty`, `ApiBody` schemas, and security options.
10. Add CLI auto-registration.
11. Add WebSocket rooms/broadcasting.
12. Add GraphQL module.
13. Add queue module.
14. Split optional dependencies.

## Final Assessment

FaNest is no longer just a toy. It has a meaningful framework skeleton, a working request pipeline, usable DI, scoped providers, Swagger/auth/cache/scheduler/WebSocket/microservice starts, and a real example app.

But it is not complete yet. The biggest gap is not missing decorators. The biggest gap is **runtime architecture depth**:

- true module-local dependency resolution
- async provider lifecycle
- adapter abstraction
- package-level production behavior
- CLI intelligence

Once those are fixed, FaNest can move from "portfolio framework" to "serious experimental framework."
