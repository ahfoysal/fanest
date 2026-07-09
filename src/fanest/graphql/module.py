import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from fanest import (
    Body,
    ConnectedSocket,
    Controller,
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


@dataclass(frozen=True)
class GraphQLField:
    response_key: str
    handler_name: str
    args: dict[str, Any]
    selection: list["GraphQLField"] | None = None
    location: tuple[int, int] | None = None


class GraphQLParseError(ValueError):
    pass


class GraphQLUnsupportedFeatureError(NotImplementedError):
    pass


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


@dataclass(frozen=True)
class GraphQLValidationIssue:
    message: str
    path: list[str] | None = None
    location: tuple[int, int] | None = None


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
        return [await self.load(key) for key in keys]

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
            GraphQLObjectMetadata(name=name or cls.__name__, kind="object", federation=federation),
        )
        return cls

    return decorator


def InputType(name: str | None = None):
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_graphql_type__", GraphQLObjectMetadata(name=name or cls.__name__, kind="input"))
        return cls

    return decorator


def Args(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return GraphQLArg(name=name, default=default, pipes=pipes)


def Field(type_: Any = None, *, name: str | None = None):
    def decorator(target):
        field_name = name or getattr(target, "__name__", None)
        setattr(target, "__fanest_graphql_field__", {"name": field_name, "type": type_})
        return target

    if callable(type_) and not isinstance(type_, type):
        target = type_
        type_hint = getattr(target, "__annotations__", {}).get("return")
        setattr(target, "__fanest_graphql_field__", {"name": getattr(target, "__name__", None), "type": type_hint})
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
    metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
    metadata["extends"] = True
    setattr(target, "__fanest_graphql_federation__", metadata)
    return target


def External(target: T) -> T:
    metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
    metadata["external"] = True
    setattr(target, "__fanest_graphql_federation__", metadata)
    return target


def Provides(fields: str):
    def decorator(target: T) -> T:
        metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
        metadata["provides"] = fields
        setattr(target, "__fanest_graphql_federation__", metadata)
        return target

    return decorator


def Requires(fields: str):
    def decorator(target: T) -> T:
        metadata = dict(getattr(target, "__fanest_graphql_federation__", {}))
        metadata["requires"] = fields
        setattr(target, "__fanest_graphql_federation__", metadata)
        return target

    return decorator


class GraphQLSDLParser:
    _type_pattern = re.compile(r"\b(type|input|interface)\s+([_A-Za-z][_0-9A-Za-z]*)[^{]*\{([^}]*)\}", re.DOTALL)
    _field_pattern = re.compile(r"^([_A-Za-z][_0-9A-Za-z]*)\s*(?:\([^)]*\))?\s*:\s*([^@#\n]+)")
    _key_pattern = re.compile(r'@key\s*\(\s*fields\s*:\s*"([^"]+)"\s*\)')

    @classmethod
    def parse(cls, sdl: str) -> dict[str, GraphQLObjectMetadata]:
        types: dict[str, GraphQLObjectMetadata] = {}
        for match in cls._type_pattern.finditer(sdl):
            kind, name, body = match.groups()
            fields: dict[str, str] = {}
            for raw_line in body.splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                field_match = cls._field_pattern.match(line)
                if field_match is None:
                    continue
                field_name, field_type = field_match.groups()
                fields[field_name] = field_type.strip()
            federation: dict[str, Any] = {}
            key_match = cls._key_pattern.search(match.group(0))
            if key_match is not None:
                federation["keys"] = [key_match.group(1)]
            types[name] = GraphQLObjectMetadata(
                name=name,
                kind="input" if kind == "input" else "object",
                fields=fields,
                federation=federation,
            )
        return types


@Injectable()
class GraphQLSchema:
    def __init__(self, *, federation: bool = False, federation_sdl: str | None = None):
        self.queries: dict[str, Any] = {}
        self.mutations: dict[str, Any] = {}
        self.subscriptions: dict[str, Any] = {}
        self.field_resolvers: dict[str, Any] = {}
        self.types: dict[str, GraphQLObjectMetadata] = {}
        self._field_registrations: dict[tuple[str, str], Any] = {}
        self._sdl = federation_sdl or ""
        self.federation = federation
        self._entity_resolvers: dict[str, Any] = {}
        if federation:
            self.queries["_service"] = self._service
            self.queries["_entities"] = self._entities

    def register_sdl(self, sdl: str) -> None:
        self._sdl = f"{self._sdl}\n{sdl}".strip()
        self.types.update(GraphQLSDLParser.parse(sdl))

    def register_resolver(self, resolver: Any) -> None:
        type_metadata = getattr(resolver.__class__, "__fanest_graphql_type__", None)
        if type_metadata is not None:
            self.types[type_metadata.name] = type_metadata
        for _, handler in inspect.getmembers(resolver, predicate=callable):
            metadata = getattr(handler, "__fanest_graphql__", None)
            field_metadata = getattr(handler, "__fanest_graphql_field__", None)
            if field_metadata is not None:
                type_name = type_metadata.name if type_metadata is not None else resolver.__class__.__name__
                current = self.types.get(type_name) or GraphQLObjectMetadata(name=type_name, kind="object")
                current.fields[field_metadata["name"] or handler.__name__] = field_metadata.get("type")
                self.types[type_name] = current
            if metadata is None:
                continue
            kind = metadata["kind"]
            name = metadata["name"]
            registration_key = getattr(handler, "__fanest_registration_key__", id(handler))
            field_key = (kind, name)
            previous_registration = self._field_registrations.get(field_key)
            if previous_registration is not None and previous_registration != registration_key:
                raise ValueError(f"Duplicate GraphQL {kind} field registered: {name}")
            self._field_registrations[field_key] = registration_key
            if kind == "query":
                self.queries[name] = handler
            elif kind == "mutation":
                self.mutations[name] = handler
            elif kind == "field":
                self.field_resolvers[name] = handler
            else:
                self.subscriptions[name] = handler
        if type_metadata is not None:
            reference_handler = self._reference_handler(resolver)
            if reference_handler is not None:
                self._entity_resolvers[type_metadata.name] = reference_handler

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
            return self._sdl
        lines = [
            'scalar _Any',
            'type _Service { sdl: String }',
            'union _Entity = ' + (' | '.join(sorted(self._entity_resolvers)) or '_EmptyEntity'),
        ]
        if not self._entity_resolvers:
            lines.append('type _EmptyEntity { _empty: String }')
        for graph_type in sorted(self.types.values(), key=lambda item: item.name):
            if graph_type.kind == "input":
                continue
            lines.append(f"type {graph_type.name}{self._federation_directive(graph_type)} {{")
            for field_name in self._sdl_fields(graph_type):
                lines.append(f"  {field_name}: String")
            lines.append("}")
        lines.append("type Query {")
        for name in sorted(self.queries):
            if name == "_service":
                lines.append("  _service: _Service")
            elif name == "_entities":
                lines.append("  _entities(representations: [_Any!]!): [_Entity]!")
            else:
                lines.append(f"  {name}: String")
        lines.append("}")
        return "\n".join(lines)

    def _federation_directive(self, graph_type: GraphQLObjectMetadata) -> str:
        keys = graph_type.federation.get("keys")
        if isinstance(keys, list):
            return "".join(f' @key(fields: "{key}")' for key in keys)
        return ""

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
            operation_start = self._operation_start(document, operation_name)
            variables = {
                **self._variable_defaults(document, operation_start),
                **(variables or {}),
            }
            operation = self._operation_type(document, operation_start)
            fields = self._operation_fields(document, variables, operation_start)
        except GraphQLParseError as exc:
            return {"errors": [self._format_error(str(exc))]}
        blocking_issues = [
            issue
            for issue in self.validate_fields(operation, fields)
            if issue.message.startswith("Field conflict")
        ]
        if blocking_issues:
            return {"errors": [_format_validation_issue(self, issue) for issue in blocking_issues]}
        if not fields:
            return {"errors": [self._format_error("GraphQL document must contain at least one field")]}
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
                errors.append(
                    self._format_error(
                        f"Unknown {operation} field: {graphql_field.handler_name}",
                        path=[graphql_field.response_key],
                        location=graphql_field.location,
                    )
                )
                continue
            try:
                result = await self._execute_handler(
                    handler,
                    variables,
                    graphql_field.args,
                    graphql_field,
                    operation=operation,
                )
                data[graphql_field.response_key] = await self._shape_result(
                    result,
                    graphql_field,
                    [graphql_field.response_key],
                )
            except Exception as exc:
                data[graphql_field.response_key] = None
                errors.append(
                    self._format_error(
                        str(exc),
                        path=[graphql_field.response_key],
                        location=graphql_field.location,
                    )
                )
        response: dict[str, Any] = {"data": data}
        if errors:
            response["errors"] = errors
        return response

    async def subscribe(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            operation_start = self._operation_start(document, operation_name)
            variables = {
                **self._variable_defaults(document, operation_start),
                **(variables or {}),
            }
            operation = self._operation_type(document, operation_start)
            if operation != "subscription":
                yield {"errors": [self._format_error("GraphQL WebSocket subscribe requires a subscription operation")]}
                return
            fields = self._operation_fields(document, variables, operation_start)
        except GraphQLParseError as exc:
            yield {"errors": [self._format_error(str(exc))]}
            return
        issues = self.validate_fields("subscription", fields)
        if issues:
            yield {"errors": [_format_validation_issue(self, issue) for issue in issues]}
            return
        async for payload in self._subscribe_fields(fields, variables):
            yield payload

    async def _subscribe_fields(
        self,
        fields: list[GraphQLField],
        variables: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        for graphql_field in fields:
            handler = self.subscriptions.get(graphql_field.handler_name)
            if handler is None:
                yield {
                    "data": {graphql_field.response_key: None},
                    "errors": [
                        self._format_error(
                            f"Unknown subscription field: {graphql_field.handler_name}",
                            path=[graphql_field.response_key],
                            location=graphql_field.location,
                        )
                    ],
                }
                continue
            try:
                kwargs = self._handler_kwargs(handler, variables, graphql_field.args)
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if hasattr(result, "__aiter__"):
                    async for item in result:
                        item = await self._apply_subscription_hooks(handler, item, variables, graphql_field.args)
                        shaped = await self._shape_result(item, graphql_field, [graphql_field.response_key])
                        yield {"data": {graphql_field.response_key: shaped}}
                elif inspect.isgenerator(result):
                    for item in result:
                        item = await self._apply_subscription_hooks(handler, item, variables, graphql_field.args)
                        shaped = await self._shape_result(item, graphql_field, [graphql_field.response_key])
                        yield {"data": {graphql_field.response_key: shaped}}
                else:
                    result = await self._apply_subscription_hooks(handler, result, variables, graphql_field.args)
                    shaped = await self._shape_result(result, graphql_field, [graphql_field.response_key])
                    yield {"data": {graphql_field.response_key: shaped}}
            except Exception as exc:
                yield {
                    "data": {graphql_field.response_key: None},
                    "errors": [self._format_error(str(exc), path=[graphql_field.response_key], location=graphql_field.location)],
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
        return [_format_validation_issue(self, issue) for issue in self.validate_fields(operation, fields)]

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
                "directives": [{"name": "include"}, {"name": "skip"}],
                "types": [
                    {"kind": "OBJECT", "name": "Query"},
                    {"kind": "OBJECT", "name": "Mutation"},
                    {"kind": "OBJECT", "name": "Subscription"},
                    {"kind": "SCALAR", "name": "String"},
                    {"kind": "SCALAR", "name": "Int"},
                    {"kind": "SCALAR", "name": "Float"},
                    {"kind": "SCALAR", "name": "Boolean"},
                    {"kind": "SCALAR", "name": "ID"},
                    *[
                        {"kind": "INPUT_OBJECT" if item.kind == "input" else "OBJECT", "name": item.name}
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
            if name in {"String", "Int", "Float", "Boolean", "ID"}:
                return {"kind": "SCALAR", "name": name, "fields": None}
            if not isinstance(name, str):
                return None
            graph_type = self.types.get(name)
            if graph_type is not None:
                return {
                    "kind": "INPUT_OBJECT" if graph_type.kind == "input" else "OBJECT",
                    "name": graph_type.name,
                    "fields": [
                        {"name": field_name, "type": self._introspection_type_ref(field_type)}
                        for field_name, field_type in graph_type.fields.items()
                    ],
                    "federation": graph_type.federation,
                }
            return None
        return None

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
            return result

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
                return None
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

    async def _shape_result(self, value: Any, field: GraphQLField, path: list[str | int]) -> Any:
        selection = field.selection or []
        if not selection:
            return value
        if value is None:
            return None
        if isinstance(value, list | tuple):
            shaped_items = []
            for index, item in enumerate(value):
                shaped_items.append(await self._shape_selection(item, selection, [*path, index]))
            return shaped_items
        return await self._shape_selection(value, selection, path)

    async def _shape_selection(self, value: Any, selection: list[GraphQLField], path: list[str | int]) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for child in selection:
            if child.handler_name == "__typename":
                shaped[child.response_key] = self._typename_for(value)
                continue
            child_value = await self._resolve_child_field(value, child, path)
            shaped[child.response_key] = await self._shape_result(child_value, child, [*path, child.response_key])
        return shaped

    async def _resolve_child_field(self, value: Any, child: GraphQLField, path: list[str | int]) -> Any:
        field_resolver = self.field_resolvers.get(child.handler_name)
        if field_resolver is not None:
            return await self._execute_handler(
                field_resolver,
                {},
                child.args,
                child,
                operation="field",
                parent=value,
            )
        return await self._read_result_field(value, child.handler_name)

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

    def _typename_for(self, value: Any) -> str:
        if isinstance(value, dict):
            return value.get("__typename") or "Object"
        return type(value).__name__

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
            if index < len(document) and (document[index].isalpha() or document[index] == "_"):
                _, index = self._read_name(document, index)
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
        return (
            self._fields_from_selection(
                document,
                variables,
                selection_start,
                fragments,
                nested_seen,
            ),
            index,
        )

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
            if name not in {"skip", "include"}:
                raise GraphQLParseError(f"Unknown directive: {name}")
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

    def _read_string_value(self, document: str, index: int) -> tuple[str, int]:
        quote = document[index]
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
            if index < len(document) and document[index] == "=":
                default, index = self._read_value(document, index + 1, defaults)
                defaults[name] = default
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
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return values
        accepted = set(parameters)
        return {key: value for key, value in values.items() if key in accepted}


class GraphQLModule:
    @staticmethod
    def for_root(
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        path: str = "graphql",
        schema: str | None = None,
        websocket: bool = True,
        federation: bool = False,
    ) -> type:
        controller_path = path.strip("/")
        module_imports = imports or []
        sdl = schema or ""

        def schema_factory() -> GraphQLSchema:
            graph_schema = GraphQLSchema(federation=federation)
            if sdl:
                graph_schema.register_sdl(sdl)
            return graph_schema

        schema_provider = use_factory(GraphQLSchema, schema_factory) if federation or sdl else GraphQLSchema

        @Controller(controller_path)
        class GraphQLController:
            def __init__(self, schema: GraphQLSchema):
                self.schema = schema

            @Post("/")
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
            providers=[schema_provider, *resolvers],
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
        path: str = "graphql",
        websocket: bool = True,
    ) -> type:
        return GraphQLModule.for_root(
            resolvers=resolvers,
            imports=imports,
            path=path,
            schema=schema,
            websocket=websocket,
        )

    @staticmethod
    def for_federation(
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        path: str = "graphql",
        schema: str | None = None,
        websocket: bool = True,
    ) -> type:
        return GraphQLModule.for_root(
            resolvers=resolvers,
            imports=imports,
            path=path,
            schema=schema,
            websocket=websocket,
            federation=True,
        )
