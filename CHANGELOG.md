# Changelog

All notable changes to FaNest are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-07-10

First release actually published to PyPI (the v0.4.0 tag's publish step was
blocked by a red test gate; see the test fix below). Includes an Inertia
adapter stabilization pass.

### Fixed
- **CI/release gate:** event tests used `asyncio.run(emitter.emit(...))`;
  `emit()` returns a fire-and-forget awaitable, not a coroutine, which
  `asyncio.run()` rejects on Python 3.10–3.13. Switched to the `emit_async()`
  coroutine variant (test-only; the shipped package was unaffected). This
  unblocks the Release workflow's publish step.
- **Inertia adapter — adversarial stabilization pass:**
  - `set_root_view()` no longer mutates the singleton service's shared config
    (per-request state instead) — a cross-request leak; proven isolated under
    300 concurrent requests.
  - `X-Inertia` JSON responses tolerate non-native props (datetime/UUID/Decimal/
    set/Enum) via `default=str`, matching the HTML path (previously 500'd on SPA
    navigation).
  - stale-version 409 percent-encodes the URL (non-latin-1 slugs no longer crash
    Starlette header encoding); deep-copied dict-form shared props; bounded
    method-override body scan + streamed large uploads (DoS); TypeError-safe CSRF
    compare (clean 419); resilient Vite manifest/hot-file reads; SSR falls back
    to CSR on malformed results; `Vary: X-Inertia` merged not dropped; path-
    traversal rejected in `ensure_pages_exist`; CR/LF stripped from redirects.

[0.4.1]: https://github.com/ahfoysal/fanest/releases/tag/v0.4.1

## [0.4.0] - 2026-07-10

First stable `0.4.0` release, promoted from the `0.3.0b6` beta after live-service
certification. Everything in `0.3.0b1`–`0.3.0b6` is included.

### Verified
- **Live-service certification** run against real **PostgreSQL, Redis and
  MongoDB** (in addition to the in-memory/fake backends): SQLAlchemy, cache,
  session, throttler, queue, and Mongo paths all pass. Full suite: 677 passed.
- **Wheel build + install** certified in a clean temp venv.

### Fixed
- **session:** `RedisSessionStore` (and `MemorySessionStore`) now implement
  `clear()`, matching the cache-store interface and the `SessionStore` protocol —
  a gap surfaced by live Redis certification.

[0.4.0]: https://github.com/ahfoysal/fanest/releases/tag/v0.4.0

## [0.3.0b6] - 2026-07-10

Combined release: Inertia v2 beta hardening plus the remaining round-2 bug
fixes. Full suite green (661 passed, 30 skipped) with ruff and pyright clean.

### Changed
- **Inertia adapter split into a package** (`fanest.inertia`): the monolithic
  module is now `context` / `service` / `middleware` / `rendering` / `props` /
  `ssr` / `vite` / `errors` submodules, with env-gated error pages and safer
  defaults (beta hardening).

### Added
- **Inertia infinite-scroll props** (`scrollProps`) — Inertia v2 parity.

### Fixed
- **SQLAlchemy (3):** transactions are tracked per `SqlAlchemyService` (a
  ContextVar registry keyed by service), so a transaction on one database no
  longer leaks its session into another connection; named connections via
  `for_root(name=...)` / `for_feature(connection=...)` coexist with distinct DI
  tokens, and two unnamed `for_root` imports fail loudly — including when
  `SqlAlchemyService` is injected directly; `MigrationManager` numbers new
  migrations from the highest existing prefix + 1 (deleting one no longer
  re-issues a duplicate sequence).
- **Observability (9):** `HttpHealthIndicator` runs its blocking `urllib` call
  off the event loop and reports an expected non-2xx status as healthy;
  `HealthModule.register_async` resolves the factory once, not twice;
  `render_prometheus` groups each metric's `# HELP`/`# TYPE` immediately before
  its own samples and validates metric/label names as ASCII; the logger applies
  new stream/structured/handler options on re-registration and maps
  `level="verbose"` to DEBUG; `MailerService.send()` with an async transport
  fails fast pointing to `send_async`; `HttpModule` forwards `client_options`
  to `httpx.AsyncClient`.
- **CLI (1):** `fanest new` rejects project names with a trailing separator
  (invalid PEP 508 distribution name).

[0.3.0b6]: https://github.com/ahfoysal/fanest/releases/tag/v0.3.0b6

## [0.3.0b5] - 2026-07-10

Combined release consolidating the Inertia v2 adapter, a large cross-subsystem
bug audit, and several parity features onto one integration line. All work below
is on `main` at this tag; the full suite is green (640 passed, 30 skipped) with
`ruff` and `pyright` clean.

### Added — parity features

- **Inertia.js v2 adapter** (`fanest.inertia`): first-class, opt-in Laravel-Inertia
  parity — page objects, partial reloads (`only`/`except`, dot-notation), asset
  versioning (409 + `X-Inertia-Location`), 303 redirects for PUT/PATCH/DELETE,
  external `location` redirects, deferred/merge/prepend/always/lazy props, history
  encryption, Vite dev/manifest asset injection, and optional Node SSR. Session
  integration: `with_errors()`, flash, `back()`, empty-response redirect-back,
  error bags, and reflash on version-mismatch reloads.
- **`REQUEST` / `INQUIRER` injection tokens**: request-scoped provider resolving to
  the current request (scope bubbles to consumers, no cross-request bleed under
  concurrency) and a transient provider resolving to the consuming class.
- **`RouterModule.register`**: hierarchical route prefixes with children, composing
  with the global prefix and URI versioning, scoped per application.
- **OAuth2 security scopes**: `@Scopes`, `ScopesGuard`, `CurrentSecurityScopes`,
  automatic OpenAPI security wiring, wired into `AuthModule` global guards.
- **`FaNestApplication.enable_shutdown_hooks()`**: signal-chained graceful shutdown;
  hooks receive the triggering signal name; original handlers restored after lifespan.
- **Standalone application context** (`create_application_context`,
  `FaNestApplicationContext`): non-HTTP DI + lifecycle (NestJS `createApplicationContext`).
- **Policy-based (CASL-style) authorization**: `AbilityBuilder`, `PoliciesGuard`,
  `@CheckPolicies`.
- **`GraphQLModule.for_root/for_schema/for_federation`** accept `providers=`.
- **Multi-instance-safe SQLAlchemy schema bootstrap** via advisory lock.

### Fixed

**HTTP adapter (7):** POST handlers default to `201 Created` (NestJS; `@HttpCode`
overrides; GraphQL endpoint pinned to 200); host-scoped `@Controller(host=...)` only
serves the matching Host (else 404) with parameterized sub-domain binding;
header/media-type/custom versioning with `default_version` 404s an unmatched request
version; `@ApiExcludeController` keeps serving routes and only omits them from the
OpenAPI schema; upload routes no longer crash when a DI class interceptor is stacked
with a `FilesInterceptor`.

**Swagger / OpenAPI (6):** `create_document` deep-copies the cached schema (no
cross-document corruption); `add_security` honours `requirements=`; TypeScript client
sanitizes operationIds into valid identifiers; `@ApiHideProperty` fields stripped from
every component schema (no leaked `hidden` marker); `$ref` parameters no longer deduped
away; `dict` → `object`+`additionalProperties`, `IntEnum` → `integer` schema inference.

**Core DI / pipes / events / CQRS / serialization / mapped types / MongoDB (21):**
controllers default to singleton scope with lifecycle hooks and eager instantiation
(`scope="request"` opt-in); shutdown hook order corrected
(`onModuleDestroy → beforeApplicationShutdown → onApplicationShutdown`); sync `create()`
normalizes dict/callable dynamic-module imports; request/transient-scoped global
enhancers resolve per request; scanner resolves `ForwardRef` providers and tolerates
uninspectable callable attributes; `ParseIntPipe`/`ParseArrayPipe`/`ValidationPipe`
edge cases; stacked `@OnEvent`, `*`/`**` wildcard semantics, and once/on ordering;
Command/Query bus errors propagate; saga dedup; class-level and nested `@Exclude`;
mapped types transplant `@model_validator`; in-memory Mongo array queries, `$each`,
`$pull` conditions, dotted-array traversal, mixed-type sort, and `MotorCollection`
update-return.

**Microservices & queues (6):** TCP frame limit >64 KiB; NATS `send()` raises remote
errors; broker single-process warning only when unconfigured; a raising `@EventPattern`
handler no longer kills the listener loop; queue ids scoped per queue; `clean()` residue
(attempts, double-count, failed/dead-letter phantoms).

**Scheduler (4):** no `@property` side effects at bootstrap; invalid cron/timezone fails
fast at registration; `@Interval`/`@Cron` skip missed ticks after a stall (no burst);
unique job names for same-named providers.

**GraphQL parser/executor (5):** comment-aware brace scanning; block strings
(`"""..."""`); `__typename`/fragment conditions use the `@ObjectType` schema name;
nested enums serialize by name; same `@ResolveField` name across different object types.

**Other subsystems (7):** i18n interpolation inserts values literally (no backslash /
group-reference crashes); `.env` inline-comment stripping after quoted values; throttler
class-level `@Throttle` and `@SkipThrottle(False)` override; session malformed-cookie
resilience and `rolling=False`; policies conditional grants at type level;
cache controller-level `@CacheTTL`/`@CacheKey`; websocket `leave_namespace` also leaves
its rooms.

### Notes

- The earlier `0.3.0b1`–`b4` audit (issues #36–#92) is included in this line.
- `src/fanest/_version.py` was stale (`0.3.0b1`); all version sources are now aligned to `0.3.0b5`.

[0.3.0b5]: https://github.com/ahfoysal/fanest/releases/tag/v0.3.0b5
