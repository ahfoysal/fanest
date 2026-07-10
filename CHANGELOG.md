# Changelog

All notable changes to FaNest are documented here. This project follows
[Semantic Versioning](https://semver.org/) (currently in the `0.3.0` beta line).

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
