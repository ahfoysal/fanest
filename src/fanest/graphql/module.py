import asyncio
import enum
import inspect
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from fanest import (
    Body,
    ConnectedSocket,
    Controller,
    HttpCode,
    Injectable,
    MessageBody,
    Module,
    Post,
    SubscribeMessage,
    WebSocketGateway,
    use_factory,
)
from fanest.core.metadata import ExecutionContext

T = TypeVar("T")

_SUBSCRIPTION_FILTERED = object()


@dataclass(frozen=True)
class GraphQLField:
    response_key: str
    handler_name: str
    args: dict[str, Any]
    selection: list["GraphQLField"] | None = None
    location: tuple[int, int] | None = None
    type_condition: str | None = None


class GraphQLParseError(ValueError):
    pass


class GraphQLUnsupportedFeatureError(NotImplementedError):
    pass


_UNSUPPORTED_FEDERATION_DIRECTIVES = {
    "extends": "@extends",
    "external": "@external",
    "provides": "@provides",
    "requires": "@requires",
}


@dataclass(frozen=True)
class GraphQLArg:
    name: str | None = None
    default: Any = ...
    pipes: tuple[Any, ...] = ()


@dataclass(frozen=True)
class GraphQLObjectMetadata:
    name: str
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    federation: dict[str, Any] = field(default_factory=dict)
    directives: tuple[str, ...] = ()
    field_directives: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphQLValidationIssue:
    message: str
    path: list[str] | None = None
    location: tuple[int, int] | None = None


@dataclass(frozen=True)
class GraphQLDirectiveMetadata:
    name: str
    locations: tuple[str, ...] = ()
    repeatable: bool = False


def _format_validation_issue(schema: "GraphQLSchema", issue: GraphQLValidationIssue) -> dict[str, Any]:
    return schema._format_error(issue.message, path=issue.path, location=issue.location)


class GraphQLDataLoader:
    def __init__(self, batch_load_fn: Callable[[list[Any]], Any]):
        self.batch_load_fn = batch_load_fn
        self._cache: dict[Any, Any] = {}
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._scheduled = False

    async def load(self, key: Any) -> Any:
        if key in self._cache:
            return self._cache[key]
        if key in self._pending:
            return await self._pending[key]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[key] = future
        if not self._scheduled:
            self._scheduled = True
            loop.call_soon(asyncio.create_task, self.dispatch())
        return await future

    async def load_many(self, keys: list[Any]) -> list[Any]:
        return list(await asyncio.gather(*(self.load(key) for key in keys)))

    async def dispatch(self) -> None:
        pending = self._pending
        self._pending = {}
        self._scheduled = False
        keys = list(pending)
        try:
            values = self.batch_load_fn(keys)
            if inspect.isawaitable(values):
                values = await values
            values_by_key = dict(zip(keys, values, strict=False))
            for key, future in pending.items():
                value = values_by_key.get(key)
                self._cache[key] = value
                if not future.done():
                    future.set_result(value)
        except Exception as exc:
            for future in pending.values():
                if not future.done():
                    future.set_exception(exc)

    def clear(self, key: Any | None = None) -> "GraphQLDataLoader":
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)
        return self

    def prime(self, key: Any, value: Any) -> "GraphQLDataLoader":
        self._cache.setdefault(key, value)
        return self


def _append_metadata(target: Any, key: str, values: tuple[Any, ...]) -> None:
    existing = list(getattr(target, key, []))
    existing.extend(values)
    setattr(target, key, existing)


def _copy_enhancer_metadata(source: Any, target: Any) -> None:
    for key in ("__fanest_guards__", "__fanest_pipes__", "__fanest_interceptors__"):
        values = getattr(source, key, None)
        if values is not None:
            setattr(target, key, list(values))


def _set_federation_metadata(target: Any, values: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
    metadata.update(values)
    setattr(target, "__fanest_graphql_federation__", metadata)
    existing_type = getattr(target, "__fanest_graphql_type__", None)
    if existing_type is not None:
        setattr(
            target,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=existing_type.name,
                kind=existing_type.kind,
                fields=existing_type.fields,
                federation=metadata,
                directives=existing_type.directives,
                field_directives=existing_type.field_directives,
            ),
        )
    return metadata


def _append_graphql_directive_usage(target: Any, directive: str) -> None:
    usages: list[str] = list(getattr(target, "__fanest_graphql_directives__", ()))
    usages.append(directive.strip())
    setattr(target, "__fanest_graphql_directives__", tuple(usages))
    field_metadata = getattr(target, "__fanest_graphql_field__", None)
    if field_metadata is not None:
        setattr(
            target,
            "__fanest_graphql_field__",
            {**field_metadata, "directives": tuple(usages)},
        )
    existing_type = getattr(target, "__fanest_graphql_type__", None)
    if existing_type is not None:
        setattr(
            target,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=existing_type.name,
                kind=existing_type.kind,
                fields=existing_type.fields,
                federation=existing_type.federation,
                directives=tuple(usages),
                field_directives=existing_type.field_directives,
            ),
        )


def Resolver(cls=None):
    # Accept both `@Resolver` and `@Resolver()` so the wrong form does not raise.
    if cls is None:
        return Resolver
    return Injectable()(cls)


def ObjectType(name: str | None = None):
    def decorator(cls: type[T]) -> type[T]:
        existing = getattr(cls, "__fanest_graphql_type__", None)
        federation = dict(getattr(existing, "federation", {}))
        federation.update(getattr(cls, "__fanest_graphql_federation__", {}))
        setattr(
            cls,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=name or cls.__name__,
                kind="object",
                federation=federation,
                directives=tuple(getattr(cls, "__fanest_graphql_directives__", ())),
            ),
        )
        return cls

    return decorator


def InputType(name: str | None = None):
    def decorator(cls: type[T]) -> type[T]:
        setattr(
            cls,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=name or cls.__name__,
                kind="input",
                directives=tuple(getattr(cls, "__fanest_graphql_directives__", ())),
            ),
        )
        return cls

    return decorator


def Args(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return GraphQLArg(name=name, default=default, pipes=pipes)


def Field(type_: Any = None, *, name: str | None = None):
    def decorator(target):
        field_name = name or getattr(target, "__name__", None)
        setattr(
            target,
            "__fanest_graphql_field__",
            {
                "name": field_name,
                "type": type_,
                "directives": tuple(getattr(target, "__fanest_graphql_directives__", ())),
            },
        )
        return target

    if callable(type_) and not isinstance(type_, type):
        target = type_
        type_hint = getattr(target, "__annotations__", {}).get("return")
        setattr(
            target,
            "__fanest_graphql_field__",
            {
                "name": getattr(target, "__name__", None),
                "type": type_hint,
                "directives": tuple(getattr(target, "__fanest_graphql_directives__", ())),
            },
        )
        return target
    return decorator


def InterfaceType(name: str | None = None):
    def decorator(cls: type[T]) -> type[T]:
        setattr(
            cls,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=name or cls.__name__,
                kind="interface",
                directives=tuple(getattr(cls, "__fanest_graphql_directives__", ())),
            ),
        )
        return cls

    return decorator


def UnionType(name: str | None = None, *, types: tuple[type, ...] | list[type] = ()):
    def decorator(cls: type[T]) -> type[T]:
        union_types = []
        for item in types:
            metadata = getattr(item, "__fanest_graphql_type__", None)
            union_types.append(metadata.name if metadata is not None else item.__name__)
        setattr(
            cls,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=name or cls.__name__,
                kind="union",
                fields={"types": tuple(union_types)},
                directives=tuple(getattr(cls, "__fanest_graphql_directives__", ())),
            ),
        )
        return cls

    return decorator


def EnumType(enum_cls: type[T] | None = None, *, name: str | None = None):
    def decorator(cls: type[T]) -> type[T]:
        if issubclass(cls, enum.Enum):
            values = tuple(item.name for item in cls)
        else:
            values = tuple(key for key, value in vars(cls).items() if key.isupper() and not callable(value))
        setattr(
            cls,
            "__fanest_graphql_type__",
            GraphQLObjectMetadata(
                name=name or cls.__name__,
                kind="enum",
                fields={value: value for value in values},
                directives=tuple(getattr(cls, "__fanest_graphql_directives__", ())),
            ),
        )
        return cls

    if enum_cls is None:
        return decorator
    return decorator(enum_cls)


def Scalar(name: str | None = None, *, serialize: Callable[[Any], Any] | None = None, parse_value: Callable[[Any], Any] | None = None):
    def decorator(target: T) -> T:
        setattr(
            target,
            "__fanest_graphql_scalar__",
            {
                "name": name or getattr(target, "__name__", str(target)),
                "serialize": serialize or getattr(target, "serialize", None),
                "parse_value": parse_value or getattr(target, "parse_value", None),
            },
        )
        return target

    return decorator


def Directive(name: str, *, locations: tuple[str, ...] | list[str] = (), repeatable: bool = False):
    def decorator(target: T) -> T:
        directive_name = name.strip()
        setattr(
            target,
            "__fanest_graphql_directive__",
            GraphQLDirectiveMetadata(name=directive_name.lstrip("@").split("(", 1)[0], locations=tuple(locations), repeatable=repeatable),
        )
        _append_graphql_directive_usage(target, directive_name if directive_name.startswith("@") else f"@{directive_name}")
        return target

    return decorator


def UseFieldMiddleware(*middleware: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_graphql_field_middleware__", middleware)
        return target

    return decorator


def ResolveField(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "field", "name": name or handler.__name__})
        return handler

    return decorator


def ResolveReference(handler):
    setattr(handler, "__fanest_graphql_resolve_reference__", True)
    return handler


def Query(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "query", "name": name or handler.__name__})
        return handler

    return decorator


def Mutation(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "mutation", "name": name or handler.__name__})
        return handler

    return decorator


def Subscription(
    name: str | None = None,
    *,
    filter: Callable[..., Any] | None = None,
    resolve: Callable[..., Any] | None = None,
):
    def decorator(handler):
        setattr(
            handler,
            "__fanest_graphql__",
            {
                "kind": "subscription",
                "name": name or handler.__name__,
                "filter": filter,
                "resolve": resolve,
            },
        )
        return handler

    return decorator


def UseGuards(*guards: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_guards__", guards)
        return target

    return decorator


def UsePipes(*pipes: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_pipes__", pipes)
        return target

    return decorator


def UseInterceptors(*interceptors: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_interceptors__", interceptors)
        return target

    return decorator


def Key(fields: str):
    def decorator(target: T) -> T:
        metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
        metadata.setdefault("keys", []).append(fields)
        setattr(target, "__fanest_graphql_federation__", metadata)
        existing_type = getattr(target, "__fanest_graphql_type__", None)
        if existing_type is not None:
            setattr(
                target,
                "__fanest_graphql_type__",
                GraphQLObjectMetadata(
                    name=existing_type.name,
                    kind=existing_type.kind,
                    fields=existing_type.fields,
                    federation=metadata,
                ),
            )
        return target

    return decorator


def Extends(target: T) -> T:
    _set_federation_metadata(target, {"extends": True})
    return target


def External(target: T) -> T:
    _set_federation_metadata(target, {"external": True})
    return target


def Provides(fields: str):
    def decorator(target: T) -> T:
        _set_federation_metadata(target, {"provides": fields})
        return target

    return decorator


def Requires(fields: str):
    def decorator(target: T) -> T:
        _set_federation_metadata(target, {"requires": fields})
        return target

    return decorator


class GraphQLSDLParser:
    _type_pattern = re.compile(r"\b(type|input|interface)\s+([_A-Za-z][_0-9A-Za-z]*)[^{]*\{([^}]*)\}", re.DOTALL)
    _enum_pattern = re.compile(r"\benum\s+([_A-Za-z][_0-9A-Za-z]*)[^{]*\{([^}]*)\}", re.DOTALL)
    _scalar_pattern = re.compile(r"\bscalar\s+([_A-Za-z][_0-9A-Za-z]*)")
    _union_pattern = re.compile(r"\bunion\s+([_A-Za-z][_0-9A-Za-z]*)\s*=\s*([^\n#]+)")
    _field_pattern = re.compile(r"^([_A-Za-z][_0-9A-Za-z]*)\s*(?:\([^)]*\))?\s*:\s*([^@#\n]+)")
    _key_pattern = re.compile(r'@key\s*\(\s*fields\s*:\s*"([^"]+)"\s*\)')
    _directive_usage_pattern = re.compile(r"@[_A-Za-z][_0-9A-Za-z]*(?:\s*\([^)]*\))?")

    @classmethod
    def parse(cls, sdl: str) -> dict[str, GraphQLObjectMetadata]:
        types: dict[str, GraphQLObjectMetadata] = {}
        for match in cls._type_pattern.finditer(sdl):
            kind, name, body = match.groups()
            fields: dict[str, str] = {}
            field_directives: dict[str, tuple[str, ...]] = {}
            for raw_line in body.splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                field_match = cls._field_pattern.match(line)
                if field_match is None:
                    continue
                field_name, field_type = field_match.groups()
                fields[field_name] = field_type.strip()
                directives = cls._directive_usages(line[field_match.end() :])
                if directives:
                    field_directives[field_name] = directives
            federation: dict[str, Any] = {}
            key_match = cls._key_pattern.search(match.group(0))
            if key_match is not None:
                federation["keys"] = [key_match.group(1)]
            header = match.group(0).split("{", 1)[0]
            type_directives = cls._directive_usages(header)
            types[name] = GraphQLObjectMetadata(
                name=name,
                kind={"type": "object", "input": "input", "interface": "interface"}[kind],
                fields=fields,
                federation=federation,
                directives=type_directives,
                field_directives=field_directives,
            )
        for match in cls._enum_pattern.finditer(sdl):
            name, body = match.groups()
            values = [
                line.split("#", 1)[0].strip().split()[0]
                for line in body.splitlines()
                if line.split("#", 1)[0].strip()
            ]
            types[name] = GraphQLObjectMetadata(
                name=name,
                kind="enum",
                fields={value: value for value in values},
            )
        for match in cls._union_pattern.finditer(sdl):
            name, raw_types = match.groups()
            union_types = tuple(part.strip() for part in raw_types.split("|") if part.strip())
            types[name] = GraphQLObjectMetadata(
                name=name,
                kind="union",
                fields={"types": union_types},
            )
        for match in cls._scalar_pattern.finditer(sdl):
            name = match.group(1)
            types.setdefault(name, GraphQLObjectMetadata(name=name, kind="scalar"))
        return types

    @classmethod
    def _directive_usages(cls, source: str) -> tuple[str, ...]:
        return tuple(" ".join(match.group(0).split()) for match in cls._directive_usage_pattern.finditer(source))


@Injectable()
class GraphQLSchema:
    _builtin_scalars = {"String", "Int", "Float", "Boolean", "ID"}

    def __init__(
        self,
        *,
        federation: bool = False,
        federation_sdl: str | None = None,
        max_complexity: int | None = None,
        extensions: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        field_middleware: list[Any] | None = None,
    ):
        self.queries: dict[str, Any] = {}
        self.mutations: dict[str, Any] = {}
        self.subscriptions: dict[str, Any] = {}
        self.field_resolvers: dict[tuple[str, str], Any] = {}
        self.types: dict[str, GraphQLObjectMetadata] = {}
        self.scalars: dict[str, dict[str, Any]] = {}
        self.directives: dict[str, GraphQLDirectiveMetadata] = {
            "include": GraphQLDirectiveMetadata("include", ("FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT")),
            "skip": GraphQLDirectiveMetadata("skip", ("FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT")),
        }
        self._field_registrations: dict[tuple[str, ...], Any] = {}
        self._sdl = federation_sdl or ""
        self.federation = federation
        self.max_complexity = max_complexity
        self.extensions = dict(extensions or {})
        self.plugins = list(plugins or [])
        self.field_middleware = list(field_middleware or [])
        self._entity_resolvers: dict[str, Any] = {}
        if federation:
            self.queries["_service"] = self._service
            self.queries["_entities"] = self._entities

    def register_sdl(self, sdl: str) -> None:
        self._sdl = f"{self._sdl}\n{sdl}".strip()
        self.types.update(GraphQLSDLParser.parse(sdl))

    def register_model(self, model: type) -> None:
        metadata = getattr(model, "__fanest_graphql_type__", None)
        if metadata is None:
            raise GraphQLUnsupportedFeatureError(
                f"GraphQL model sharing requires @ObjectType, @InputType, @InterfaceType, @UnionType, or @EnumType: {model.__name__}"
            )
        fields = dict(metadata.fields)
        field_directives = dict(metadata.field_directives)
        model_fields = getattr(model, "model_fields", None)
        if isinstance(model_fields, dict):
            for field_name, model_field in model_fields.items():
                fields.setdefault(field_name, getattr(model_field, "annotation", None))
        for _, member in inspect.getmembers(model, predicate=callable):
            field_metadata = getattr(member, "__fanest_graphql_field__", None)
            if field_metadata is not None:
                field_name = field_metadata["name"] or member.__name__
                fields[field_name] = field_metadata.get("type")
                if field_metadata.get("directives"):
                    field_directives[field_name] = tuple(field_metadata["directives"])
        if fields != metadata.fields or field_directives != metadata.field_directives:
            metadata = GraphQLObjectMetadata(
                name=metadata.name,
                kind=metadata.kind,
                fields=fields,
                federation=metadata.federation,
                directives=metadata.directives,
                field_directives=field_directives,
            )
        self.types[metadata.name] = metadata

    def register_scalar(self, scalar: Any, *, name: str | None = None) -> None:
        metadata = getattr(scalar, "__fanest_graphql_scalar__", None)
        scalar_name = name or (metadata or {}).get("name") or getattr(scalar, "__name__", str(scalar))
        if not hasattr(self, "scalars"):
            self.scalars = {}
        self.scalars[str(scalar_name)] = {
            "target": scalar,
            "serialize": (metadata or {}).get("serialize") or getattr(scalar, "serialize", None),
            "parse_value": (metadata or {}).get("parse_value") or getattr(scalar, "parse_value", None),
        }
        self.types[str(scalar_name)] = GraphQLObjectMetadata(name=str(scalar_name), kind="scalar")

    def register_directive(self, directive: Any) -> None:
        metadata = getattr(directive, "__fanest_graphql_directive__", None)
        if metadata is None:
            if isinstance(directive, str):
                metadata = GraphQLDirectiveMetadata(directive.lstrip("@"))
            else:
                raise GraphQLUnsupportedFeatureError(
                    f"GraphQL directives must use @Directive metadata: {directive!r}"
                )
        self.directives[metadata.name] = metadata

    def register_resolver(self, resolver: Any) -> None:
        type_metadata = getattr(resolver.__class__, "__fanest_graphql_type__", None)
        if type_metadata is not None:
            self._ensure_supported_federation_metadata(type_metadata.federation, owner=type_metadata.name)
            self.types[type_metadata.name] = type_metadata
        for _, handler in inspect.getmembers(resolver, predicate=callable):
            metadata = getattr(handler, "__fanest_graphql__", None)
            field_metadata = getattr(handler, "__fanest_graphql_field__", None)
            self._ensure_supported_federation_metadata(
                getattr(handler, "__fanest_graphql_federation__", {}),
                owner=getattr(handler, "__name__", "GraphQL field"),
            )
            if field_metadata is not None:
                type_name = type_metadata.name if type_metadata is not None else resolver.__class__.__name__
                current = self.types.get(type_name) or GraphQLObjectMetadata(name=type_name, kind="object")
                registered_field_name = field_metadata["name"] or handler.__name__
                current.fields[registered_field_name] = field_metadata.get("type")
                directives: list[str] = list(field_metadata.get("directives") or ())
                federation = getattr(handler, "__fanest_graphql_federation__", {})
                if federation.get("external"):
                    directives.append("@external")
                if isinstance(federation.get("provides"), str):
                    directives.append(f'@provides(fields: "{federation["provides"]}")')
                if isinstance(federation.get("requires"), str):
                    directives.append(f'@requires(fields: "{federation["requires"]}")')
                if directives:
                    current.field_directives[registered_field_name] = tuple(directives)
                self.types[type_name] = current
            if metadata is None:
                continue
            kind = metadata["kind"]
            name = metadata["name"]
            registration_key = getattr(handler, "__fanest_registration_key__", id(handler))
            owner_type = type_metadata.name if type_metadata is not None else resolver.__class__.__name__
            try:
                handler.__fanest_graphql_owner_type__ = owner_type
            except (AttributeError, TypeError):
                pass
            field_key: tuple[str, ...] = (kind, owner_type, name) if kind == "field" else (kind, name)
            previous_registration = self._field_registrations.get(field_key)
            if previous_registration is not None and previous_registration != registration_key:
                raise ValueError(f"Duplicate GraphQL {kind} field registered: {name}")
            self._field_registrations[field_key] = registration_key
            if kind == "query":
                self.queries[name] = handler
            elif kind == "mutation":
                self.mutations[name] = handler
            elif kind == "field":
                self.field_resolvers[(owner_type, name)] = handler
            else:
                self.subscriptions[name] = handler
        if type_metadata is not None:
            reference_handler = self._reference_handler(resolver)
            if reference_handler is not None:
                self._entity_resolvers[type_metadata.name] = reference_handler

    def _ensure_supported_federation_metadata(self, metadata: dict[str, Any], *, owner: str) -> None:
        return

    async def _service(self) -> dict[str, str]:
        return {"sdl": self.federation_sdl()}

    async def _entities(self, representations: list[dict[str, Any]]) -> list[Any]:
        entities = []
        for representation in representations:
            typename = representation.get("__typename")
            resolver = self._entity_resolvers.get(str(typename))
            if resolver is None:
                entities.append(None)
                continue
            result = self._call_reference_resolver(resolver, representation)
            if inspect.isawaitable(result):
                result = await result
            entities.append(result)
        return entities

    def _call_reference_resolver(self, resolver: Any, representation: dict[str, Any]) -> Any:
        signature = getattr(resolver, "__fanest_target_signature__", inspect.signature(resolver))
        parameters = [parameter for parameter in signature.parameters.values() if parameter.name != "self"]
        if not parameters:
            return resolver()
        first = parameters[0]
        if first.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            if getattr(resolver, "__fanest_registration_key__", None) is not None:
                return resolver(**{first.name: representation})
            try:
                return resolver(representation)
            except TypeError:
                return resolver(**{first.name: representation})
        return resolver(**{first.name: representation})

    def _reference_handler(self, resolver: Any) -> Any | None:
        for name in ("resolve_reference", "__resolve_reference__"):
            handler = getattr(resolver, name, None)
            if handler is not None:
                return handler
        for _, handler in inspect.getmembers(resolver, predicate=callable):
            if getattr(handler, "__fanest_graphql_resolve_reference__", False):
                return handler
        return None

    def federation_sdl(self) -> str:
        if self._sdl:
            base_sdl = self.sdl()
        else:
            base_sdl = self.sdl()
        lines = [
            base_sdl,
            'scalar _Any',
            'type _Service { sdl: String }',
            'union _Entity = ' + (' | '.join(sorted(self._entity_resolvers)) or '_EmptyEntity'),
        ]
        if not self._entity_resolvers:
            lines.append('type _EmptyEntity { _empty: String }')
        lines.append("extend type Query {")
        lines.append("  _service: _Service")
        lines.append("  _entities(representations: [_Any!]!): [_Entity]!")
        lines.append("}")
        return "\n".join(line for line in lines if line).strip()

    def sdl(self) -> str:
        lines: list[str] = []
        declared_types = set()
        if self._sdl:
            lines.append(self._sdl)
            declared_types = set(GraphQLSDLParser.parse(self._sdl))
        for directive in self.directives.values():
            if directive.name in {"include", "skip"}:
                continue
            locations = " | ".join(directive.locations) if directive.locations else "FIELD"
            repeatable = " repeatable" if directive.repeatable else ""
            lines.append(f"directive @{directive.name}{repeatable} on {locations}")
        for graph_type in sorted(self.types.values(), key=lambda item: item.name):
            if graph_type.name in declared_types:
                continue
            if graph_type.kind == "scalar":
                if graph_type.name not in self._builtin_scalars:
                    lines.append(f"scalar {graph_type.name}")
                continue
            if graph_type.kind == "enum":
                lines.append(f"enum {graph_type.name} {{")
                for value in graph_type.fields:
                    lines.append(f"  {value}")
                lines.append("}")
                continue
            if graph_type.kind == "union":
                union_types = graph_type.fields.get("types", ())
                lines.append(f"union {graph_type.name} = {' | '.join(map(str, union_types))}")
                continue
            keyword = {
                "object": "type",
                "input": "input",
                "interface": "interface",
            }.get(graph_type.kind, "type")
            lines.append(f"{keyword} {graph_type.name}{self._type_directives(graph_type)} {{")
            for field_name, field_type in graph_type.fields.items():
                lines.append(
                    f"  {field_name}: {self._sdl_type_ref(field_type)}"
                    f"{self._field_directives(graph_type, field_name)}"
                )
            lines.append("}")
        self._append_root_sdl(lines, "Query", self.queries)
        self._append_root_sdl(lines, "Mutation", self.mutations)
        self._append_root_sdl(lines, "Subscription", self.subscriptions)
        return "\n".join(line for line in lines if line).strip()

    def _append_root_sdl(self, lines: list[str], name: str, handlers: dict[str, Any]) -> None:
        public_handlers = {key: value for key, value in handlers.items() if not key.startswith("_")}
        if not public_handlers:
            return
        lines.append(f"type {name} {{")
        for field_name, handler in sorted(public_handlers.items()):
            lines.append(f"  {field_name}: {self._handler_return_type(handler)}")
        lines.append("}")

    def _handler_return_type(self, handler: Any) -> str:
        signature = getattr(handler, "__fanest_target_signature__", inspect.signature(handler))
        return_annotation = signature.return_annotation
        if return_annotation is inspect.Signature.empty:
            return "String"
        return self._sdl_type_ref(return_annotation)

    def _sdl_type_ref(self, field_type: Any) -> str:
        if isinstance(field_type, str):
            return field_type.strip()
        if isinstance(field_type, type):
            metadata = getattr(field_type, "__fanest_graphql_type__", None)
            if metadata is not None:
                return metadata.name
            scalar_metadata = getattr(field_type, "__fanest_graphql_scalar__", None)
            if scalar_metadata is not None:
                return str(scalar_metadata["name"])
            return {
                str: "String",
                int: "Int",
                float: "Float",
                bool: "Boolean",
            }.get(field_type, field_type.__name__)
        return "String"

    def _federation_directive(self, graph_type: GraphQLObjectMetadata) -> str:
        directives: list[str] = []
        if graph_type.federation.get("extends"):
            directives.append("@extends")
        keys = graph_type.federation.get("keys")
        if isinstance(keys, list):
            directives.extend(f'@key(fields: "{key}")' for key in keys)
        return "".join(f" {directive}" for directive in directives)

    def _type_directives(self, graph_type: GraphQLObjectMetadata) -> str:
        usages = "".join(f" {directive}" for directive in graph_type.directives if directive)
        return f"{usages}{self._federation_directive(graph_type)}"

    def _field_directives(self, graph_type: GraphQLObjectMetadata, field_name: str) -> str:
        directives = list(graph_type.field_directives.get(field_name, ()))
        federation = graph_type.federation
        if federation.get("external") and field_name in self._sdl_fields(graph_type):
            directives.append("@external")
        if isinstance(federation.get("provides"), str):
            directives.append(f'@provides(fields: "{federation["provides"]}")')
        if isinstance(federation.get("requires"), str):
            directives.append(f'@requires(fields: "{federation["requires"]}")')
        return "".join(f" {directive}" for directive in directives)

    def _sdl_fields(self, graph_type: GraphQLObjectMetadata) -> list[str]:
        fields = list(graph_type.fields)
        keys = graph_type.federation.get("keys")
        if isinstance(keys, list):
            for key in keys:
                if isinstance(key, str):
                    for field_name in key.split():
                        if field_name not in fields:
                            fields.append(field_name)
        return fields or ["id"]

    async def execute(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            await self._run_plugin_hook("before_execute", document, variables or {}, operation_name)
            operation_start = self._operation_start(document, operation_name)
            variables = {
                **self._variable_defaults(document, operation_start),
                **(variables or {}),
            }
            operation = self._operation_type(document, operation_start)
            fields = self._operation_fields(document, variables, operation_start)
        except GraphQLParseError as exc:
            return await self._finalize_response({"errors": [self._format_error(str(exc))]})
        blocking_issues = [
            issue
            for issue in self.validate_fields(operation, fields)
            if issue.message.startswith("Field conflict")
        ]
        if blocking_issues:
            return await self._finalize_response(
                {"errors": [_format_validation_issue(self, issue) for issue in blocking_issues]}
            )
        complexity_issue = self._complexity_issue(fields)
        if complexity_issue is not None:
            return await self._finalize_response({"errors": [self._format_validation_issue(complexity_issue)]})
        if not fields:
            if self._selection_is_empty(document, operation_start):
                return await self._finalize_response(
                    {"errors": [self._format_error("GraphQL document must contain at least one field")]}
                )
            return await self._finalize_response({"data": {}})
        handlers = {
            "query": self.queries,
            "mutation": self.mutations,
            "subscription": self.subscriptions,
        }[operation]
        data: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        for graphql_field in fields:
            if self._is_root_meta_field(graphql_field.handler_name):
                meta_value = self._root_meta_field(
                    operation,
                    graphql_field.handler_name,
                    graphql_field.args,
                )
                data[graphql_field.response_key] = await self._shape_result(
                    meta_value,
                    graphql_field,
                    [graphql_field.response_key],
                )
                continue
            handler = handlers.get(graphql_field.handler_name)
            if handler is None:
                data[graphql_field.response_key] = None
                error = self._format_error(
                    f"Unknown {operation} field: {graphql_field.handler_name}",
                    path=[graphql_field.response_key],
                    location=graphql_field.location,
                )
                await self._run_plugin_hook("on_error", error, graphql_field, operation)
                errors.append(error)
                continue
            try:
                await self._run_plugin_hook("before_resolve", graphql_field, operation)
                result = await self._execute_handler(
                    handler,
                    variables,
                    graphql_field.args,
                    graphql_field,
                    operation=operation,
                )
                for hook_result in await self._run_plugin_hook(
                    "after_resolve",
                    result,
                    graphql_field,
                    operation,
                ):
                    if hook_result is not None:
                        result = hook_result
                data[graphql_field.response_key] = await self._shape_result(
                    result,
                    graphql_field,
                    [graphql_field.response_key],
                    self._handler_owner_type(handler),
                )
            except Exception as exc:
                data[graphql_field.response_key] = None
                error = self._format_error(
                    str(exc),
                    path=[graphql_field.response_key],
                    location=graphql_field.location,
                )
                await self._run_plugin_hook("on_error", error, graphql_field, operation)
                errors.append(error)
        response: dict[str, Any] = {"data": data}
        if errors:
            response["errors"] = errors
        return await self._finalize_response(response)

    async def subscribe(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            await self._run_plugin_hook("before_execute", document, variables or {}, operation_name)
            operation_start = self._operation_start(document, operation_name)
            variables = {
                **self._variable_defaults(document, operation_start),
                **(variables or {}),
            }
            operation = self._operation_type(document, operation_start)
            if operation != "subscription":
                yield await self._finalize_response(
                    {"errors": [self._format_error("GraphQL WebSocket subscribe requires a subscription operation")]}
                )
                return
            fields = self._operation_fields(document, variables, operation_start)
        except GraphQLParseError as exc:
            yield await self._finalize_response({"errors": [self._format_error(str(exc))]})
            return
        blocking_issues = [
            issue
            for issue in self.validate_fields("subscription", fields)
            if issue.message.startswith("Field conflict")
        ]
        if blocking_issues:
            yield await self._finalize_response({"errors": [_format_validation_issue(self, issue) for issue in blocking_issues]})
            return
        complexity_issue = self._complexity_issue(fields)
        if complexity_issue is not None:
            yield await self._finalize_response({"errors": [self._format_validation_issue(complexity_issue)]})
            return
        async for payload in self._subscribe_fields(fields, variables):
            yield await self._finalize_response(payload)

    async def _subscribe_fields(
        self,
        fields: list[GraphQLField],
        variables: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        for graphql_field in fields:
            handler = self.subscriptions.get(graphql_field.handler_name)
            if handler is None:
                error = self._format_error(
                    f"Unknown subscription field: {graphql_field.handler_name}",
                    path=[graphql_field.response_key],
                    location=graphql_field.location,
                )
                await self._run_plugin_hook("on_error", error, graphql_field, "subscription")
                yield {
                    "data": {graphql_field.response_key: None},
                    "errors": [error],
                }
                continue
            try:
                await self._run_plugin_hook("before_resolve", graphql_field, "subscription")
                result = await self._execute_subscription_source(
                    handler,
                    variables,
                    graphql_field.args,
                    graphql_field,
                )
                if hasattr(result, "__aiter__"):
                    async for item in result:
                        item = await self._apply_subscription_hooks(handler, item, variables, graphql_field.args)
                        if item is _SUBSCRIPTION_FILTERED:
                            continue
                        for hook_result in await self._run_plugin_hook(
                            "after_resolve",
                            item,
                            graphql_field,
                            "subscription",
                        ):
                            if hook_result is not None:
                                item = hook_result
                        shaped = await self._shape_result(item, graphql_field, [graphql_field.response_key], self._handler_owner_type(handler))
                        yield {"data": {graphql_field.response_key: shaped}}
                elif inspect.isgenerator(result):
                    for item in result:
                        item = await self._apply_subscription_hooks(handler, item, variables, graphql_field.args)
                        if item is _SUBSCRIPTION_FILTERED:
                            continue
                        for hook_result in await self._run_plugin_hook(
                            "after_resolve",
                            item,
                            graphql_field,
                            "subscription",
                        ):
                            if hook_result is not None:
                                item = hook_result
                        shaped = await self._shape_result(item, graphql_field, [graphql_field.response_key], self._handler_owner_type(handler))
                        yield {"data": {graphql_field.response_key: shaped}}
                else:
                    result = await self._apply_subscription_hooks(handler, result, variables, graphql_field.args)
                    if result is _SUBSCRIPTION_FILTERED:
                        continue
                    for hook_result in await self._run_plugin_hook(
                        "after_resolve",
                        result,
                        graphql_field,
                        "subscription",
                    ):
                        if hook_result is not None:
                            result = hook_result
                    shaped = await self._shape_result(result, graphql_field, [graphql_field.response_key], self._handler_owner_type(handler))
                    yield {"data": {graphql_field.response_key: shaped}}
            except Exception as exc:
                error = self._format_error(str(exc), path=[graphql_field.response_key], location=graphql_field.location)
                await self._run_plugin_hook("on_error", error, graphql_field, "subscription")
                yield {
                    "data": {graphql_field.response_key: None},
                    "errors": [error],
                }

    def validate(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            operation_start = self._operation_start(document, operation_name)
            merged_variables = {
                **self._variable_defaults(document, operation_start),
                **(variables or {}),
            }
            operation = self._operation_type(document, operation_start)
            fields = self._operation_fields(document, merged_variables, operation_start)
        except GraphQLParseError as exc:
            return [self._format_error(str(exc))]
        issues = self.validate_fields(operation, fields)
        complexity_issue = self._complexity_issue(fields)
        if complexity_issue is not None:
            issues.append(complexity_issue)
        return [_format_validation_issue(self, issue) for issue in issues]

    def _complexity_issue(self, fields: list[GraphQLField]) -> GraphQLValidationIssue | None:
        if self.max_complexity is None:
            return None
        complexity = self._field_complexity(fields)
        if complexity <= self.max_complexity:
            return None
        return GraphQLValidationIssue(
            f"GraphQL query complexity {complexity} exceeds max_complexity {self.max_complexity}"
        )

    def _field_complexity(self, fields: list[GraphQLField]) -> int:
        total = 0
        for graphql_field in fields:
            total += 1
            if graphql_field.selection:
                total += self._field_complexity(graphql_field.selection)
        return total

    async def _run_plugin_hook(self, hook_name: str, *args: Any) -> Any:
        results = []
        for plugin in self.plugins:
            hook = getattr(plugin, hook_name, None)
            if hook is None and isinstance(plugin, dict):
                hook = plugin.get(hook_name)
            if hook is None:
                continue
            result = hook(self, *args)
            if inspect.isawaitable(result):
                result = await result
            results.append(result)
        return results

    async def _finalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if self.extensions:
            response = {**response, "extensions": dict(self.extensions)}
        for result in await self._run_plugin_hook("after_execute", response):
            if result is not None:
                response = result
        return response

    def validate_fields(self, operation: str, fields: list[GraphQLField]) -> list[GraphQLValidationIssue]:
        handlers = {
            "query": self.queries,
            "mutation": self.mutations,
            "subscription": self.subscriptions,
        }[operation]
        issues: list[GraphQLValidationIssue] = []
        seen_response_keys: dict[str, GraphQLField] = {}
        for graphql_field in fields:
            previous = seen_response_keys.get(graphql_field.response_key)
            if previous is not None and previous.handler_name != graphql_field.handler_name:
                issues.append(
                    GraphQLValidationIssue(
                        f"Field conflict for response key '{graphql_field.response_key}'",
                        path=[graphql_field.response_key],
                        location=graphql_field.location,
                    )
                )
            seen_response_keys[graphql_field.response_key] = graphql_field
            if not self._is_root_meta_field(graphql_field.handler_name) and graphql_field.handler_name not in handlers:
                issues.append(
                    GraphQLValidationIssue(
                        f"Unknown {operation} field: {graphql_field.handler_name}",
                        path=[graphql_field.response_key],
                        location=graphql_field.location,
                    )
                )
        return issues

    def _is_root_meta_field(self, field_name: str) -> bool:
        return field_name in {"__typename", "__schema", "__type"}

    def _root_meta_field(self, operation: str, field_name: str, args: dict[str, Any] | None = None) -> Any:
        if field_name == "__typename":
            return {
                "query": "Query",
                "mutation": "Mutation",
                "subscription": "Subscription",
            }[operation]
        if field_name == "__schema":
            return {
                "queryType": {"name": "Query"},
                "mutationType": {"name": "Mutation"},
                "subscriptionType": {"name": "Subscription"},
                "directives": [
                    {
                        "name": directive.name,
                        "locations": list(directive.locations),
                        "isRepeatable": directive.repeatable,
                    }
                    for directive in self.directives.values()
                ],
                "types": [
                    {"kind": "OBJECT", "name": "Query"},
                    {"kind": "OBJECT", "name": "Mutation"},
                    {"kind": "OBJECT", "name": "Subscription"},
                    *[
                        {"kind": "SCALAR", "name": scalar_name}
                        for scalar_name in sorted(self._builtin_scalars)
                    ],
                    *[
                        {"kind": self._introspection_kind(item.kind), "name": item.name}
                        for item in self.types.values()
                    ],
                ],
                "federation": self._federation_metadata(),
            }
        if field_name == "__type":
            name = (args or {}).get("name")
            if name in {"Query", "Mutation", "Subscription"}:
                return {
                    "kind": "OBJECT",
                    "name": name,
                    "fields": self._introspection_fields_for(name),
                }
            if name in self._builtin_scalars:
                return {"kind": "SCALAR", "name": name, "fields": None}
            if not isinstance(name, str):
                return None
            graph_type = self.types.get(name)
            if graph_type is not None:
                return {
                    "kind": self._introspection_kind(graph_type.kind),
                    "name": graph_type.name,
                    "fields": self._introspection_fields_for_metadata(graph_type),
                    "possibleTypes": self._possible_types_for(graph_type),
                    "enumValues": self._enum_values_for(graph_type),
                    "federation": graph_type.federation,
                }
            return None
        return None

    def _introspection_kind(self, kind: str) -> str:
        return {
            "object": "OBJECT",
            "input": "INPUT_OBJECT",
            "interface": "INTERFACE",
            "union": "UNION",
            "enum": "ENUM",
            "scalar": "SCALAR",
        }.get(kind, "OBJECT")

    def _introspection_fields_for_metadata(self, graph_type: GraphQLObjectMetadata) -> list[dict[str, Any]] | None:
        if graph_type.kind in {"enum", "scalar", "union"}:
            return None
        return [
            {"name": field_name, "type": self._introspection_type_ref(field_type)}
            for field_name, field_type in graph_type.fields.items()
        ]

    def _possible_types_for(self, graph_type: GraphQLObjectMetadata) -> list[dict[str, str]] | None:
        if graph_type.kind != "union":
            return None
        union_types = graph_type.fields.get("types", ())
        return [{"name": str(type_name)} for type_name in union_types]

    def _enum_values_for(self, graph_type: GraphQLObjectMetadata) -> list[dict[str, str]] | None:
        if graph_type.kind != "enum":
            return None
        return [{"name": str(value)} for value in graph_type.fields]

    def _introspection_fields_for(self, type_name: str) -> list[dict[str, str]]:
        handlers = {
            "Query": self.queries,
            "Mutation": self.mutations,
            "Subscription": self.subscriptions,
        }[type_name]
        return [{"name": name} for name in handlers]

    def _introspection_type_ref(self, field_type: Any) -> dict[str, Any]:
        if isinstance(field_type, str):
            normalized = field_type.strip().rstrip("!")
            if normalized.startswith("[") and normalized.endswith("]"):
                return {"kind": "LIST", "name": normalized[1:-1].rstrip("!")}
            return {"kind": "TYPE", "name": normalized}
        if isinstance(field_type, type):
            return {"kind": "TYPE", "name": field_type.__name__}
        if field_type is None:
            return {"kind": "TYPE", "name": None}
        return {"kind": "TYPE", "name": str(field_type)}

    def _federation_metadata(self) -> dict[str, Any]:
        entities = []
        for metadata in self.types.values():
            if metadata.federation:
                entities.append({"name": metadata.name, **metadata.federation})
        return {"entities": entities}

    async def _execute_handler(
        self,
        handler: Any,
        variables: dict[str, Any],
        args: dict[str, Any],
        field: GraphQLField,
        *,
        operation: str,
        parent: Any = None,
    ) -> Any:
        kwargs = self._handler_kwargs(handler, variables, args, parent=parent)
        context = ExecutionContext(
            handler=handler,
            controller=None,
            request=None,
            kwargs={"graphql_field": field, "graphql_operation": operation, **kwargs},
        )
        await self._run_guards(handler, context)
        kwargs = await self._run_pipes(handler, context, kwargs)

        async def call_handler() -> Any:
            result = handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if operation == "subscription":
                result = await self._apply_subscription_hooks(handler, result, variables, args)
                if result is _SUBSCRIPTION_FILTERED:
                    result = None
            return await self._serialize_handler_result(handler, result)

        return await self._run_interceptors(handler, context, call_handler)

    async def _execute_subscription_source(
        self,
        handler: Any,
        variables: dict[str, Any],
        args: dict[str, Any],
        field: GraphQLField,
    ) -> Any:
        kwargs = self._handler_kwargs(handler, variables, args)
        context = ExecutionContext(
            handler=handler,
            controller=None,
            request=None,
            kwargs={"graphql_field": field, "graphql_operation": "subscription", **kwargs},
        )
        await self._run_guards(handler, context)
        kwargs = await self._run_pipes(handler, context, kwargs)

        async def call_handler() -> Any:
            result = handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return await self._serialize_handler_result(handler, result)

        return await self._run_interceptors(handler, context, call_handler)

    async def _apply_subscription_hooks(
        self,
        handler: Any,
        result: Any,
        variables: dict[str, Any],
        args: dict[str, Any],
    ) -> Any:
        metadata = getattr(handler, "__fanest_graphql__", {})
        filter_hook = metadata.get("filter")
        if filter_hook is not None:
            allowed = filter_hook(result, variables, args)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return _SUBSCRIPTION_FILTERED
        resolve_hook = metadata.get("resolve")
        if resolve_hook is not None:
            resolved = resolve_hook(result, variables, args)
            if inspect.isawaitable(resolved):
                return await resolved
            return resolved
        return result

    async def _run_guards(self, handler: Any, context: ExecutionContext) -> None:
        for guard in getattr(handler, "__fanest_guards__", []):
            instance = self._component_instance(guard)
            result = instance.can_activate(context)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise PermissionError("Forbidden")

    async def _run_pipes(self, handler: Any, context: ExecutionContext, kwargs: dict[str, Any]) -> dict[str, Any]:
        transformed = dict(kwargs)
        for pipe in getattr(handler, "__fanest_pipes__", []):
            instance = self._component_instance(pipe)
            for name, value in list(transformed.items()):
                result = instance.transform(value, {"name": name, "handler": handler, "source": "graphql"})
                if inspect.isawaitable(result):
                    result = await result
                transformed[name] = result
        signature = getattr(handler, "__fanest_target_signature__", inspect.signature(handler))
        for name, parameter in signature.parameters.items():
            default = parameter.default
            if not isinstance(default, GraphQLArg) or name not in transformed:
                continue
            for pipe in default.pipes:
                instance = self._component_instance(pipe)
                result = instance.transform(
                    transformed[name],
                    {"name": name, "handler": handler, "source": "graphql", "data": default.name},
                )
                if inspect.isawaitable(result):
                    result = await result
                transformed[name] = result
        context.kwargs.update(transformed)
        return transformed

    async def _run_interceptors(
        self,
        handler: Any,
        context: ExecutionContext,
        call_handler: Callable[[], Any],
    ) -> Any:
        interceptors = list(getattr(handler, "__fanest_interceptors__", []))

        async def dispatch(index: int) -> Any:
            if index >= len(interceptors):
                return await call_handler()
            instance = self._component_instance(interceptors[index])
            result = instance.intercept(context, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await result
            return result

        return await dispatch(0)

    def _component_instance(self, component: Any) -> Any:
        return component() if inspect.isclass(component) else component

    def _handler_owner_type(self, handler: Any) -> str | None:
        tagged = getattr(handler, "__fanest_graphql_owner_type__", None)
        if isinstance(tagged, str):
            return tagged
        owner = getattr(handler, "__self__", None)
        if owner is None:
            return None
        metadata = getattr(type(owner), "__fanest_graphql_type__", None)
        if metadata is not None and metadata.kind in {"object", "interface"}:
            return metadata.name
        return None

    async def _serialize_handler_result(self, handler: Any, value: Any) -> Any:
        signature = getattr(handler, "__fanest_target_signature__", inspect.signature(handler))
        return await self._coerce_scalar(
            value,
            signature.return_annotation,
            hook_name="serialize",
        )

    async def _coerce_scalar(self, value: Any, annotation: Any, *, hook_name: str) -> Any:
        if value is None:
            return None
        if hook_name == "serialize" and isinstance(value, enum.Enum):
            metadata = getattr(type(value), "__fanest_graphql_type__", None)
            if metadata is not None and metadata.kind == "enum":
                return value.name
        scalar = self._scalar_for_annotation(annotation)
        if scalar is None:
            return value
        hook = scalar.get(hook_name)
        if hook is None:
            return value
        instance = self._component_instance(scalar["target"])
        bound_hook = getattr(instance, hook_name, hook)
        result = bound_hook(value)
        if inspect.isawaitable(result):
            return await result
        return result

    def _scalar_for_annotation(self, annotation: Any) -> dict[str, Any] | None:
        if annotation is inspect.Signature.empty:
            return None
        metadata = getattr(annotation, "__fanest_graphql_scalar__", None)
        if metadata is not None:
            return self.scalars.get(str(metadata["name"]))
        if isinstance(annotation, str):
            return self.scalars.get(annotation)
        name = getattr(annotation, "__name__", None)
        if name is not None:
            return self.scalars.get(str(name))
        return None

    async def _shape_result(
        self,
        value: Any,
        field: GraphQLField,
        path: list[str | int],
        parent_type: str | None = None,
    ) -> Any:
        selection = field.selection or []
        if not selection:
            return self._serialize_leaf(value)
        if value is None:
            return None
        if isinstance(value, list | tuple):
            shaped_items = []
            for index, item in enumerate(value):
                shaped_items.append(await self._shape_selection(item, selection, [*path, index], parent_type))
            return shaped_items
        return await self._shape_selection(value, selection, path, parent_type)

    def _serialize_leaf(self, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return [self._serialize_leaf(item) for item in value]
        if isinstance(value, enum.Enum):
            metadata = getattr(type(value), "__fanest_graphql_type__", None)
            if metadata is not None and metadata.kind == "enum":
                return value.name
        return value

    async def _shape_selection(
        self,
        value: Any,
        selection: list[GraphQLField],
        path: list[str | int],
        parent_type: str | None = None,
    ) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        current_type = self._value_type_name(value) or parent_type
        for child in selection:
            if child.type_condition is not None and not self._matches_type_condition(value, child.type_condition):
                continue
            if child.handler_name == "__typename":
                shaped[child.response_key] = self._typename_for(value)
                continue
            child_value = await self._resolve_child_field(value, child, path, current_type)
            shaped[child.response_key] = await self._shape_result(
                child_value,
                child,
                [*path, child.response_key],
                self._value_type_name(child_value),
            )
        return shaped

    async def _resolve_child_field(
        self,
        value: Any,
        child: GraphQLField,
        path: list[str | int],
        parent_type: str | None = None,
    ) -> Any:
        field_resolver = None
        if parent_type is not None:
            field_resolver = self.field_resolvers.get((parent_type, child.handler_name))
        middleware = list(self.field_middleware)
        if field_resolver is not None:
            middleware.extend(getattr(field_resolver, "__fanest_graphql_field_middleware__", []))

            async def resolve() -> Any:
                return await self._execute_handler(
                    field_resolver,
                    {},
                    child.args,
                    child,
                    operation="field",
                    parent=value,
                )

        else:

            async def resolve() -> Any:
                return await self._read_result_field(value, child.handler_name)

        return await self._run_field_middleware(middleware, value, child, path, resolve)

    async def _run_field_middleware(
        self,
        middleware: list[Any],
        parent: Any,
        field: GraphQLField,
        path: list[str | int],
        resolve: Callable[[], Any],
    ) -> Any:
        context = {
            "parent": parent,
            "field": field,
            "path": path,
            "args": field.args,
        }

        async def dispatch(index: int) -> Any:
            if index >= len(middleware):
                return await resolve()
            instance = self._component_instance(middleware[index])
            hook = getattr(instance, "resolve", None) or getattr(instance, "use", None)
            if hook is None and callable(instance):
                hook = instance
            if hook is None:
                raise GraphQLUnsupportedFeatureError(
                    f"GraphQL field middleware must be callable or expose resolve/use: {instance!r}"
                )
            result = hook(context, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await result
            return result

        return await dispatch(0)

    async def _read_result_field(self, value: Any, field_name: str) -> Any:
        if isinstance(value, dict):
            field_value = value.get(field_name)
            if inspect.isawaitable(field_value):
                return await field_value
            return field_value
        if hasattr(value, field_name):
            attr = getattr(value, field_name)
            if callable(attr):
                attr = attr()
            if inspect.isawaitable(attr):
                return await attr
            return attr
        return None

    def _value_type_name(self, value: Any) -> str | None:
        metadata = getattr(type(value), "__fanest_graphql_type__", None)
        if metadata is not None and metadata.kind in {"object", "interface"}:
            return metadata.name
        if isinstance(value, dict):
            typename = value.get("__typename")
            if typename:
                return str(typename)
        return None

    def _typename_for(self, value: Any) -> str:
        name = self._value_type_name(value)
        if name is not None:
            return name
        if isinstance(value, dict):
            return "Object"
        return type(value).__name__

    def _matches_type_condition(self, value: Any, type_condition: str) -> bool:
        typename = self._typename_for(value)
        if typename == type_condition:
            return True
        metadata = self.types.get(type_condition)
        return metadata is not None and metadata.kind == "interface"

    def _operation_names(self, document: str) -> list[str]:
        return [field.handler_name for field in self._operation_fields(document, {})]

    def _operation_type(self, document: str, operation_start: int | None = None) -> str:
        index = self._skip_ignored(document, operation_start or 0)
        if self._name_at(document, index, "subscription"):
            return "subscription"
        if self._name_at(document, index, "mutation"):
            return "mutation"
        return "query"

    def _operation_fields(
        self,
        document: str,
        variables: dict[str, Any],
        operation_start: int | None = None,
    ) -> list[GraphQLField]:
        start = self._selection_start(document, operation_start)
        if start < 0:
            return []
        return self._fields_from_selection(
            document,
            variables,
            start,
            self._fragment_definitions(document),
            set(),
        )

    def _fields_from_selection(
        self,
        document: str,
        variables: dict[str, Any],
        selection_start: int,
        fragments: dict[str, int],
        seen_fragments: set[str],
    ) -> list[GraphQLField]:
        fields: list[GraphQLField] = []
        index = selection_start + 1
        depth = 1
        while index < len(document) and depth > 0:
            char = document[index]
            if char == "{":
                depth += 1
                index += 1
                continue
            if char == "}":
                depth -= 1
                index += 1
                continue
            if depth != 1:
                index += 1
                continue
            ignored = self._skip_ignored(document, index)
            if ignored != index:
                index = ignored
                continue
            if document.startswith("...", index):
                expanded, index = self._expand_fragment_selection(
                    document,
                    variables,
                    index + 3,
                    fragments,
                    seen_fragments,
                )
                fields.extend(expanded)
                continue
            if not (char.isalpha() or char == "_"):
                index += 1
                continue
            location = self._line_column(document, index)
            first_name, index = self._read_name(document, index)
            cursor = self._skip_ignored(document, index)
            response_key = first_name
            handler_name = first_name
            if cursor < len(document) and document[cursor] == ":":
                cursor = self._skip_ignored(document, cursor + 1)
                if cursor < len(document) and (document[cursor].isalpha() or document[cursor] == "_"):
                    handler_name, cursor = self._read_name(document, cursor)
            args, cursor = self._read_args(document, cursor, variables)
            include, cursor = self._read_directives(document, cursor, variables)
            selection: list[GraphQLField] | None = None
            cursor = self._skip_ignored(document, cursor)
            if cursor < len(document) and document[cursor] == "{":
                selection = self._fields_from_selection(
                    document,
                    variables,
                    cursor,
                    fragments,
                    seen_fragments,
                )
                cursor = self._skip_balanced(document, cursor, "{", "}")
            if include:
                fields.append(
                    GraphQLField(
                        response_key=response_key,
                        handler_name=handler_name,
                        args=args,
                        selection=selection,
                        location=location,
                    )
                )
            index = cursor
        return fields

    def _fragment_definitions(self, document: str) -> dict[str, int]:
        fragments: dict[str, int] = {}
        index = self._skip_ignored(document, 0)
        while index < len(document):
            if not self._name_at(document, index, "fragment"):
                index += 1
                index = self._skip_ignored(document, index)
                continue
            cursor = self._skip_ignored(document, index + len("fragment"))
            if cursor >= len(document) or not (document[cursor].isalpha() or document[cursor] == "_"):
                raise GraphQLParseError("Expected fragment name")
            name, cursor = self._read_name(document, cursor)
            selection_start = self._next_selection_start(document, cursor)
            fragments[name] = selection_start
            index = self._skip_balanced(document, selection_start, "{", "}")
            index = self._skip_ignored(document, index)
        return fragments

    def _expand_fragment_selection(
        self,
        document: str,
        variables: dict[str, Any],
        index: int,
        fragments: dict[str, int],
        seen_fragments: set[str],
    ) -> tuple[list[GraphQLField], int]:
        index = self._skip_ignored(document, index)
        if self._name_at(document, index, "on"):
            index = self._skip_ignored(document, index + len("on"))
            type_condition = ""
            if index < len(document) and (document[index].isalpha() or document[index] == "_"):
                type_condition, index = self._read_name(document, index)
            include, index = self._read_directives(document, index, variables)
            selection_start = self._next_selection_start(document, index)
            end = self._skip_balanced(document, selection_start, "{", "}")
            if not include:
                return [], end
            fields = self._fields_from_selection(
                document,
                variables,
                selection_start,
                fragments,
                seen_fragments,
            )
            return self._with_type_condition(fields, type_condition), end
        if index < len(document) and document[index] in {"{", "@"}:
            include, index = self._read_directives(document, index, variables)
            selection_start = self._next_selection_start(document, index)
            end = self._skip_balanced(document, selection_start, "{", "}")
            if not include:
                return [], end
            fields = self._fields_from_selection(
                document,
                variables,
                selection_start,
                fragments,
                seen_fragments,
            )
            return fields, end
        if index >= len(document) or not (document[index].isalpha() or document[index] == "_"):
            raise GraphQLParseError("Expected fragment name")
        name, index = self._read_name(document, index)
        include, index = self._read_directives(document, index, variables)
        selection_start = fragments.get(name)
        if selection_start is None:
            raise GraphQLParseError(f"Unknown fragment: {name}")
        if name in seen_fragments:
            raise GraphQLParseError(f"Circular fragment reference: {name}")
        if not include:
            return [], index
        nested_seen = {*seen_fragments, name}
        type_condition = self._fragment_type_conditions(document).get(name)
        return (
            self._with_type_condition(
                self._fields_from_selection(
                    document,
                    variables,
                    selection_start,
                    fragments,
                    nested_seen,
                ),
                type_condition,
            ),
            index,
        )

    def _fragment_type_conditions(self, document: str) -> dict[str, str]:
        conditions: dict[str, str] = {}
        index = self._skip_ignored(document, 0)
        while index < len(document):
            if not self._name_at(document, index, "fragment"):
                index += 1
                index = self._skip_ignored(document, index)
                continue
            cursor = self._skip_ignored(document, index + len("fragment"))
            if cursor >= len(document) or not (document[cursor].isalpha() or document[cursor] == "_"):
                raise GraphQLParseError("Expected fragment name")
            name, cursor = self._read_name(document, cursor)
            cursor = self._skip_ignored(document, cursor)
            if not self._name_at(document, cursor, "on"):
                raise GraphQLParseError(f"Expected type condition for fragment: {name}")
            cursor = self._skip_ignored(document, cursor + len("on"))
            if cursor >= len(document) or not (document[cursor].isalpha() or document[cursor] == "_"):
                raise GraphQLParseError(f"Expected type condition for fragment: {name}")
            type_condition, cursor = self._read_name(document, cursor)
            conditions[name] = type_condition
            selection_start = self._next_selection_start(document, cursor)
            index = self._skip_balanced(document, selection_start, "{", "}")
            index = self._skip_ignored(document, index)
        return conditions

    def _with_type_condition(
        self,
        fields: list[GraphQLField],
        type_condition: str | None,
    ) -> list[GraphQLField]:
        if not type_condition:
            return fields
        return [
            GraphQLField(
                response_key=field.response_key,
                handler_name=field.handler_name,
                args=field.args,
                selection=field.selection,
                location=field.location,
                type_condition=type_condition,
            )
            for field in fields
        ]

    def _skip_fragment_directives(self, document: str, index: int) -> int:
        index = self._skip_ignored(document, index)
        while index < len(document) and document[index] == "@":
            index += 1
            if index < len(document) and (document[index].isalpha() or document[index] == "_"):
                _, index = self._read_name(document, index)
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == "(":
                index = self._skip_balanced(document, index, "(", ")")
            index = self._skip_ignored(document, index)
        return index

    def _read_directives(
        self,
        document: str,
        index: int,
        variables: dict[str, Any],
    ) -> tuple[bool, int]:
        include = True
        index = self._skip_ignored(document, index)
        while index < len(document) and document[index] == "@":
            index += 1
            if index >= len(document) or not (document[index].isalpha() or document[index] == "_"):
                raise GraphQLParseError("Expected directive name")
            name, index = self._read_name(document, index)
            args, index = self._read_args(document, index, variables)
            if name not in self.directives:
                raise GraphQLParseError(f"Unknown directive: {name}")
            if name not in {"skip", "include"}:
                index = self._skip_ignored(document, index)
                continue
            if "if" not in args:
                raise GraphQLParseError(f"Directive @{name} requires an if argument")
            if name == "skip" and bool(args.get("if")):
                include = False
            elif name == "include" and not bool(args.get("if")):
                include = False
            index = self._skip_ignored(document, index)
        return include, index

    def _next_selection_start(self, document: str, index: int) -> int:
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == "{":
                return index
            if index < len(document) and document[index] == "(":
                index = self._skip_balanced(document, index, "(", ")")
                continue
            index += 1
        raise GraphQLParseError("GraphQL selection set was not found")

    def _operation_start(self, document: str, operation_name: str | None = None) -> int:
        if operation_name is None:
            if self._document_operation_count(document) > 1:
                raise GraphQLParseError("Operation name is required when a document contains multiple operations")
            index = self._skip_ignored(document, 0)
            while index < len(document):
                if self._name_at(document, index, "fragment"):
                    index = self._skip_definition(document, index + len("fragment"))
                    index = self._skip_ignored(document, index)
                    continue
                if (
                    self._name_at(document, index, "query")
                    or self._name_at(document, index, "mutation")
                    or self._name_at(document, index, "subscription")
                    or document[index] == "{"
                ):
                    return index
                index += 1
                index = self._skip_ignored(document, index)
            return self._skip_ignored(document, 0)
        index = self._skip_ignored(document, 0)
        while index < len(document):
            if self._name_at(document, index, "fragment"):
                index = self._skip_definition(document, index + len("fragment"))
                continue
            if (
                self._name_at(document, index, "query")
                or self._name_at(document, index, "mutation")
                or self._name_at(document, index, "subscription")
            ):
                start = index
                _, cursor = self._read_name(document, index)
                cursor = self._skip_ignored(document, cursor)
                name = ""
                if cursor < len(document) and (document[cursor].isalpha() or document[cursor] == "_"):
                    name, _ = self._read_name(document, cursor)
                if name == operation_name:
                    return start
                index = self._skip_definition(document, cursor)
                continue
            if document[index] == "{":
                index = self._skip_balanced(document, index, "{", "}")
                continue
            index += 1
            index = self._skip_ignored(document, index)
        raise GraphQLParseError(f"Unknown operation: {operation_name}")

    def _document_operation_count(self, document: str) -> int:
        count = 0
        index = self._skip_ignored(document, 0)
        while index < len(document):
            if self._name_at(document, index, "fragment"):
                index = self._skip_definition(document, index + len("fragment"))
                index = self._skip_ignored(document, index)
                continue
            if (
                self._name_at(document, index, "query")
                or self._name_at(document, index, "mutation")
                or self._name_at(document, index, "subscription")
            ):
                count += 1
                _, cursor = self._read_name(document, index)
                index = self._skip_definition(document, cursor)
                index = self._skip_ignored(document, index)
                continue
            if document[index] == "{":
                count += 1
                index = self._skip_balanced(document, index, "{", "}")
                index = self._skip_ignored(document, index)
                continue
            index += 1
            index = self._skip_ignored(document, index)
        return count

    def _skip_definition(self, document: str, index: int) -> int:
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index >= len(document):
                return index
            if document[index] == "{":
                return self._skip_balanced(document, index, "{", "}")
            if document[index] == "(":
                index = self._skip_balanced(document, index, "(", ")")
                continue
            index += 1
        return index

    def _selection_start(self, document: str, operation_start: int | None = None) -> int:
        index = self._skip_ignored(document, operation_start or 0)
        if self._name_at(document, index, "query"):
            index += len("query")
        elif self._name_at(document, index, "mutation"):
            index += len("mutation")
        elif self._name_at(document, index, "subscription"):
            index += len("subscription")
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index >= len(document):
                break
            if document[index] == "{":
                return index
            if document[index] == "(":
                index = self._skip_balanced(document, index, "(", ")")
                continue
            if document[index].isalpha() or document[index] == "_":
                _, index = self._read_name(document, index)
                continue
            index += 1
        return -1

    def _selection_is_empty(self, document: str, operation_start: int | None = None) -> bool:
        start = self._selection_start(document, operation_start)
        if start < 0:
            return True
        index = self._skip_ignored(document, start + 1)
        return index >= len(document) or document[index] == "}"

    def _read_name(self, document: str, index: int) -> tuple[str, int]:
        start = index
        while index < len(document) and (document[index].isalnum() or document[index] == "_"):
            index += 1
        return document[start:index], index

    def _skip_ws(self, document: str, index: int) -> int:
        while index < len(document) and document[index].isspace():
            index += 1
        return index

    def _skip_ignored(self, document: str, index: int) -> int:
        while index < len(document):
            if document[index].isspace() or document[index] == ",":
                index += 1
                continue
            if document[index] == "#":
                while index < len(document) and document[index] not in {"\n", "\r"}:
                    index += 1
                continue
            break
        return index

    def _skip_selection(self, document: str, index: int) -> int:
        index = self._skip_ignored(document, index)
        if index < len(document) and document[index] == "(":
            index = self._skip_balanced(document, index, "(", ")")
        index = self._skip_ignored(document, index)
        while index < len(document) and document[index] == "@":
            index += 1
            if index < len(document) and (document[index].isalpha() or document[index] == "_"):
                _, index = self._read_name(document, index)
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == "(":
                index = self._skip_balanced(document, index, "(", ")")
            index = self._skip_ignored(document, index)
        if index < len(document) and document[index] == "{":
            index = self._skip_balanced(document, index, "{", "}")
        return index

    def _skip_balanced(self, document: str, index: int, open_char: str, close_char: str) -> int:
        depth = 1
        index += 1
        while index < len(document) and depth > 0:
            char = document[index]
            if char == "#":
                while index < len(document) and document[index] not in {"\n", "\r"}:
                    index += 1
                continue
            if char in {'"', "'"}:
                index = self._skip_string(document, index)
                continue
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
            index += 1
        if depth:
            raise GraphQLParseError(f"Unclosed {open_char}")
        return index

    def _skip_string(self, document: str, index: int) -> int:
        quote = document[index]
        if quote == '"' and document[index : index + 3] == '"""':
            index += 3
            while index < len(document):
                if document[index] == "\\" and document[index : index + 4] == '\\"""':
                    index += 4
                    continue
                if document[index : index + 3] == '"""':
                    return index + 3
                index += 1
            raise GraphQLParseError("Unclosed string literal")
        index += 1
        while index < len(document):
            if document[index] == "\\":
                index += 2
                continue
            if document[index] == quote:
                return index + 1
            index += 1
        raise GraphQLParseError("Unclosed string literal")

    def _read_args(
        self,
        document: str,
        index: int,
        variables: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        index = self._skip_ignored(document, index)
        if index >= len(document) or document[index] != "(":
            return {}, index
        index += 1
        args: dict[str, Any] = {}
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == ")":
                return args, index + 1
            if index < len(document) and document[index] == "{":
                raise GraphQLParseError("Unclosed (")
            if index >= len(document) or not (document[index].isalpha() or document[index] == "_"):
                raise GraphQLParseError("Expected argument name")
            name, index = self._read_name(document, index)
            index = self._skip_ignored(document, index)
            if index >= len(document) or document[index] != ":":
                raise GraphQLParseError(f"Expected ':' after argument {name}")
            index += 1
            value, index = self._read_value(document, index, variables)
            args[name] = value
        raise GraphQLParseError("Unclosed argument list")

    def _parse_args(self, raw: str, variables: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for part in self._split_args(raw):
            if ":" not in part:
                continue
            name, value = part.split(":", 1)
            args[name.strip()] = self._parse_value(value.strip(), variables)
        return args

    def _split_args(self, raw: str) -> list[str]:
        parts: list[str] = []
        start = 0
        in_string = False
        quote = ""
        for index, char in enumerate(raw):
            if char in {"'", '"'} and (index == 0 or raw[index - 1] != "\\"):
                if in_string and char == quote:
                    in_string = False
                elif not in_string:
                    in_string = True
                    quote = char
            elif char == "," and not in_string:
                parts.append(raw[start:index].strip())
                start = index + 1
        tail = raw[start:].strip()
        if tail:
            parts.append(tail)
        return parts

    def _parse_value(self, raw: str, variables: dict[str, Any]) -> Any:
        if raw.startswith("$"):
            return variables.get(raw[1:])
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            return raw[1:-1]
        if raw == "true":
            return True
        if raw == "false":
            return False
        if raw == "null":
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            return raw

    def _read_value(self, document: str, index: int, variables: dict[str, Any]) -> tuple[Any, int]:
        index = self._skip_ignored(document, index)
        if index >= len(document):
            raise GraphQLParseError("Expected value")
        char = document[index]
        if char == "$":
            name, index = self._read_name(document, index + 1)
            if name not in variables:
                raise GraphQLParseError(f"Variable ${name} was not provided")
            return variables[name], index
        if char in {'"', "'"}:
            return self._read_string_value(document, index)
        if char == "[":
            return self._read_list_value(document, index, variables)
        if char == "{":
            return self._read_object_value(document, index, variables)
        if char == "-" or char.isdigit():
            return self._read_number_value(document, index)
        if char.isalpha() or char == "_":
            name, index = self._read_name(document, index)
            if name == "true":
                return True, index
            if name == "false":
                return False, index
            if name == "null":
                return None, index
            return name, index
        raise GraphQLParseError(f"Unexpected value token: {char}")

    _block_string_line_pattern = re.compile(r"\r\n|[\n\r]")

    def _read_string_value(self, document: str, index: int) -> tuple[str, int]:
        quote = document[index]
        if quote == '"' and document[index : index + 3] == '"""':
            return self._read_block_string_value(document, index)
        index += 1
        value: list[str] = []
        escapes = {
            '"': '"',
            "'": "'",
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        while index < len(document):
            char = document[index]
            if char == "\\":
                index += 1
                if index >= len(document):
                    raise GraphQLParseError("Unclosed escape sequence")
                escaped = document[index]
                value.append(escapes.get(escaped, escaped))
                index += 1
                continue
            if char == quote:
                return "".join(value), index + 1
            value.append(char)
            index += 1
        raise GraphQLParseError("Unclosed string literal")

    def _read_block_string_value(self, document: str, index: int) -> tuple[str, int]:
        index += 3
        chars: list[str] = []
        while index < len(document):
            if document[index] == "\\" and document[index : index + 4] == '\\"""':
                chars.append('"""')
                index += 4
                continue
            if document[index : index + 3] == '"""':
                return self._dedent_block_string("".join(chars)), index + 3
            chars.append(document[index])
            index += 1
        raise GraphQLParseError("Unclosed string literal")

    def _dedent_block_string(self, raw: str) -> str:
        lines = self._block_string_line_pattern.split(raw)
        common_indent: int | None = None
        for line in lines[1:]:
            stripped = line.lstrip(" \t")
            if stripped:
                indent = len(line) - len(stripped)
                if common_indent is None or indent < common_indent:
                    common_indent = indent
        if common_indent:
            lines = [lines[0], *(line[common_indent:] for line in lines[1:])]
        while lines and not lines[0].strip(" \t"):
            lines.pop(0)
        while lines and not lines[-1].strip(" \t"):
            lines.pop()
        return "\n".join(lines)

    def _read_list_value(
        self,
        document: str,
        index: int,
        variables: dict[str, Any],
    ) -> tuple[list[Any], int]:
        index += 1
        values: list[Any] = []
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == "]":
                return values, index + 1
            value, index = self._read_value(document, index, variables)
            values.append(value)
        raise GraphQLParseError("Unclosed list value")

    def _read_object_value(
        self,
        document: str,
        index: int,
        variables: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        index += 1
        value: dict[str, Any] = {}
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == "}":
                return value, index + 1
            if index >= len(document) or not (document[index].isalpha() or document[index] == "_"):
                raise GraphQLParseError("Expected object field name")
            name, index = self._read_name(document, index)
            index = self._skip_ignored(document, index)
            if index >= len(document) or document[index] != ":":
                raise GraphQLParseError(f"Expected ':' after object field {name}")
            field_value, index = self._read_value(document, index + 1, variables)
            value[name] = field_value
        raise GraphQLParseError("Unclosed object value")

    def _read_number_value(self, document: str, index: int) -> tuple[int | float, int]:
        start = index
        if document[index] == "-":
            index += 1
        while index < len(document) and document[index].isdigit():
            index += 1
        is_float = False
        if index < len(document) and document[index] == ".":
            is_float = True
            index += 1
            while index < len(document) and document[index].isdigit():
                index += 1
        if index < len(document) and document[index] in {"e", "E"}:
            is_float = True
            index += 1
            if index < len(document) and document[index] in {"+", "-"}:
                index += 1
            while index < len(document) and document[index].isdigit():
                index += 1
        raw = document[start:index]
        try:
            return (float(raw) if is_float else int(raw)), index
        except ValueError as exc:
            raise GraphQLParseError(f"Invalid number literal: {raw}") from exc

    def _variable_defaults(self, document: str, operation_start: int | None = None) -> dict[str, Any]:
        index = self._skip_ignored(document, operation_start or 0)
        if self._name_at(document, index, "query"):
            index += len("query")
        elif self._name_at(document, index, "mutation"):
            index += len("mutation")
        elif self._name_at(document, index, "subscription"):
            index += len("subscription")
        else:
            return {}
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index >= len(document) or document[index] in {"{", "("}:
                break
            if document[index].isalpha() or document[index] == "_":
                _, index = self._read_name(document, index)
                continue
            index += 1
        if index >= len(document) or document[index] != "(":
            return {}
        index += 1
        defaults: dict[str, Any] = {}
        while index < len(document):
            index = self._skip_ignored(document, index)
            if index < len(document) and document[index] == ")":
                return defaults
            if index >= len(document) or document[index] != "$":
                raise GraphQLParseError("Expected variable definition")
            name, index = self._read_name(document, index + 1)
            index = self._skip_ignored(document, index)
            if index >= len(document) or document[index] != ":":
                raise GraphQLParseError(f"Expected type for variable ${name}")
            index += 1
            type_start = index
            while index < len(document):
                index = self._skip_ignored(document, index)
                if index >= len(document) or document[index] in {"=", ")", "$"}:
                    break
                if document[index] == "[":
                    index = self._skip_balanced(document, index, "[", "]")
                    continue
                if document[index].isalpha() or document[index] in {"_", "!"}:
                    index += 1
                    continue
                break
            nullable = not document[type_start:index].strip().endswith("!")
            if index < len(document) and document[index] == "=":
                default, index = self._read_value(document, index + 1, defaults)
                defaults[name] = default
            elif nullable:
                defaults.setdefault(name, None)
        raise GraphQLParseError("Unclosed variable definition list")

    def _name_at(self, document: str, index: int, name: str) -> bool:
        if not document.startswith(name, index):
            return False
        end = index + len(name)
        return end >= len(document) or not (document[end].isalnum() or document[end] == "_")

    def _line_column(self, document: str, index: int) -> tuple[int, int]:
        line = document.count("\n", 0, index) + 1
        last_newline = document.rfind("\n", 0, index)
        column = index + 1 if last_newline < 0 else index - last_newline
        return line, column

    def _format_error(
        self,
        message: str,
        *,
        path: list[str] | None = None,
        location: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"message": message}
        if path is not None:
            error["path"] = path
        if location is not None:
            line, column = location
            error["locations"] = [{"line": line, "column": column}]
        return error

    def _format_validation_issue(self, issue: GraphQLValidationIssue) -> dict[str, Any]:
        return self._format_error(issue.message, path=issue.path, location=issue.location)

    def _handler_kwargs(
        self,
        handler: Any,
        variables: dict[str, Any],
        args: dict[str, Any],
        *,
        parent: Any = None,
    ) -> dict[str, Any]:
        signature = getattr(handler, "__fanest_target_signature__", inspect.signature(handler))
        parameters = signature.parameters
        values = {**variables, **args}
        for name, parameter in parameters.items():
            default = parameter.default
            if isinstance(default, GraphQLArg):
                key = default.name or name
                if key in args:
                    values[name] = args[key]
                elif key in variables:
                    values[name] = variables[key]
                elif default.default is not ...:
                    values[name] = default.default
            elif name in {"parent", "root"} and parent is not None:
                values[name] = parent
            if name in values:
                coerced = self._coerce_scalar_sync(values[name], parameter.annotation, hook_name="parse_value")
                values[name] = coerced
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return values
        accepted = set(parameters)
        return {key: value for key, value in values.items() if key in accepted}

    def _coerce_scalar_sync(self, value: Any, annotation: Any, *, hook_name: str) -> Any:
        if value is None:
            return None
        scalar = self._scalar_for_annotation(annotation)
        if scalar is None:
            return value
        hook = scalar.get(hook_name)
        if hook is None:
            return value
        instance = self._component_instance(scalar["target"])
        bound_hook = getattr(instance, hook_name, hook)
        result = bound_hook(value)
        if inspect.isawaitable(result):
            raise GraphQLUnsupportedFeatureError(
                f"Async GraphQL scalar {hook_name} hooks are not supported during argument coercion"
            )
        return result


class GraphQLModule:
    @staticmethod
    def for_root(
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        providers: list[Any] | None = None,
        path: str = "graphql",
        schema: str | None = None,
        websocket: bool = True,
        federation: bool = False,
        types: list[type] | None = None,
        scalars: list[Any] | None = None,
        directives: list[Any] | None = None,
        max_complexity: int | None = None,
        extensions: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        field_middleware: list[Any] | None = None,
    ) -> type:
        controller_path = path.strip("/")
        module_imports = imports or []
        module_providers = providers or []
        sdl = schema or ""

        def schema_factory() -> GraphQLSchema:
            graph_schema = GraphQLSchema(
                federation=federation,
                max_complexity=max_complexity,
                extensions=extensions,
                plugins=plugins,
                field_middleware=field_middleware,
            )
            if sdl:
                graph_schema.register_sdl(sdl)
            for model in types or []:
                graph_schema.register_model(model)
            for scalar in scalars or []:
                graph_schema.register_scalar(scalar)
            for directive in directives or []:
                graph_schema.register_directive(directive)
            return graph_schema

        schema_provider = (
            use_factory(GraphQLSchema, schema_factory)
            if any(
                [
                    federation,
                    sdl,
                    types,
                    scalars,
                    directives,
                    max_complexity is not None,
                    extensions,
                    plugins,
                    field_middleware,
                ]
            )
            else GraphQLSchema
        )

        @Controller(controller_path)
        class GraphQLController:
            def __init__(self, schema: GraphQLSchema):
                self.schema = schema

            @Post("/")
            @HttpCode(200)
            async def execute(self, payload: dict[str, Any] = Body()):  # type: ignore[assignment]
                return await self.schema.execute(
                    payload.get("query", ""),
                    variables=payload.get("variables"),
                    operation_name=payload.get("operationName"),
                )

        @WebSocketGateway(f"/{controller_path}/ws")
        class GraphQLSubscriptionGateway:
            def __init__(self, schema: GraphQLSchema):
                self.schema = schema

            @SubscribeMessage("connection_init")
            async def connection_init(self, websocket: Any = ConnectedSocket()):  # type: ignore[assignment]
                await cast(Any, websocket).send_json({"type": "connection_ack"})
                return None

            @SubscribeMessage("ping")
            async def ping(self, websocket: Any = ConnectedSocket()):  # type: ignore[assignment]
                await cast(Any, websocket).send_json({"type": "pong"})
                return None

            @SubscribeMessage("complete")
            async def complete(self):
                return None

            @SubscribeMessage("subscribe")
            async def subscribe(  # type: ignore[assignment]
                self,
                payload: Any = MessageBody(),
                websocket: Any = ConnectedSocket(),
            ):
                operation_id = str(payload.get("id", ""))
                request_payload = payload.get("payload") or {}
                if not isinstance(request_payload, dict):
                    await cast(Any, websocket).send_json(
                        {
                            "type": "error",
                            "id": operation_id,
                            "payload": {"message": "GraphQL subscribe payload must be an object"},
                        }
                    )
                    return None
                async for item in self.schema.subscribe(
                    request_payload.get("query", ""),
                    variables=request_payload.get("variables"),
                    operation_name=request_payload.get("operationName"),
                ):
                    await cast(Any, websocket).send_json({"type": "next", "id": operation_id, "payload": item})
                await cast(Any, websocket).send_json({"type": "complete", "id": operation_id})
                return None

            @SubscribeMessage("start")
            async def start(  # type: ignore[assignment]
                self,
                payload: Any = MessageBody(),
                websocket: Any = ConnectedSocket(),
            ):
                await self.subscribe(payload, websocket)
                return None

        @Module(
            imports=module_imports,
            controllers=[GraphQLController],
            providers=[schema_provider, *module_providers, *resolvers],
            gateways=[GraphQLSubscriptionGateway] if websocket else [],
            exports=[GraphQLSchema],
            global_module=federation,
        )
        class DynamicGraphQLModule:
            pass

        return DynamicGraphQLModule

    @staticmethod
    def for_schema(
        schema: str,
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        providers: list[Any] | None = None,
        path: str = "graphql",
        websocket: bool = True,
        types: list[type] | None = None,
        scalars: list[Any] | None = None,
        directives: list[Any] | None = None,
        max_complexity: int | None = None,
        extensions: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        field_middleware: list[Any] | None = None,
    ) -> type:
        return GraphQLModule.for_root(
            resolvers=resolvers,
            imports=imports,
            providers=providers,
            path=path,
            schema=schema,
            websocket=websocket,
            types=types,
            scalars=scalars,
            directives=directives,
            max_complexity=max_complexity,
            extensions=extensions,
            plugins=plugins,
            field_middleware=field_middleware,
        )

    @staticmethod
    def for_federation(
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        providers: list[Any] | None = None,
        path: str = "graphql",
        schema: str | None = None,
        websocket: bool = True,
        types: list[type] | None = None,
        scalars: list[Any] | None = None,
        directives: list[Any] | None = None,
        max_complexity: int | None = None,
        extensions: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        field_middleware: list[Any] | None = None,
    ) -> type:
        return GraphQLModule.for_root(
            resolvers=resolvers,
            imports=imports,
            providers=providers,
            path=path,
            schema=schema,
            websocket=websocket,
            federation=True,
            types=types,
            scalars=scalars,
            directives=directives,
            max_complexity=max_complexity,
            extensions=extensions,
            plugins=plugins,
            field_middleware=field_middleware,
        )
