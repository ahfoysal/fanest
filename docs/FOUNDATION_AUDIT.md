# FaNest Foundation Audit

This file tracks what still prevents FaNest from feeling like a complete framework rather than a collection of useful primitives.

## Core Blockers

### 1. Module Encapsulation

Current state: providers from every imported module are registered into one application-wide container.

Why this blocks completeness: Nest modules have boundaries. A module can use its own providers and providers exported by imported modules. Private providers should not leak everywhere.

Work needed:

- preserve module ownership during scanning
- compile each module with local providers, controllers, gateways, and imported exports
- resolve dependencies from local module scope first
- only expose imported module exports
- support global modules intentionally, not accidentally

### 2. Async Dependency Injection

Current state: factory providers are synchronous.

Why this blocks completeness: database clients, Redis clients, queues, and secrets often need async setup.

Work needed:

- async factory providers
- startup-time provider initialization
- async lifecycle-aware shutdown
- clean errors when async providers are used in sync contexts

### 3. Application Abstraction

Current state: `FaNestFactory.create()` returns a FastAPI app directly.

Why this blocks completeness: Nest has an application object that owns global pipes, middleware, lifecycle, microservices, shutdown hooks, and adapters.

Work needed:

- `FaNestApplication`
- `use_global_pipes`
- `use_global_guards`
- `use_global_interceptors`
- `use_global_filters`
- `enable_cors`
- `set_global_prefix`
- `connect_microservice`
- `listen`

### 4. Adapter Boundary

Current state: the framework is strongly tied to FastAPI internals.

Why this blocks completeness: a framework should have a stable internal HTTP contract, even if FastAPI is the default adapter.

Work needed:

- HTTP adapter interface
- FastAPI adapter implementation
- route metadata independent of FastAPI defaults
- response handling contract

### 5. CLI Project Intelligence

Current state: generators create files but do not update module imports.

Why this blocks completeness: Nest CLI feels productive because it wires generated files into the project.

Work needed:

- parse module files
- insert providers/controllers/gateways
- generate tests
- generate DTOs/entities
- support workspace/library mode

### 6. Package Depth

Current state: many packages exist but several are still shallow.

Work needed:

- Swagger decorators for body/query/params/properties/security
- SQLAlchemy transactions, migrations, and repository helpers
- GraphQL module
- microservice transports beyond memory
- queue package
- mailer package
- MongoDB package
- static assets and file upload helpers

## Immediate Fix Order

1. Module encapsulation groundwork
2. Application abstraction
3. Async providers
4. CLI auto-wiring
5. SQLAlchemy transactions/migrations
6. Swagger completeness
7. GraphQL and microservices transports
