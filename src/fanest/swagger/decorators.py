from collections.abc import Callable
from copy import deepcopy
from enum import Enum
import inspect
from types import UnionType
from typing import Any, Literal, TypeVar, Union, get_args, get_origin

from pydantic import Field

T = TypeVar("T")

_FANEST_EXTRA_MODELS: list[type] = []


def ApiTags(*tags: str) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_swagger_tags__", list(tags))
        return target

    return decorator


def ApiResponse(
    status_code: int | dict[str, Any],
    description: str | None = None,
    model: type | None = None,
    *,
    schema: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    examples: dict[str, Any] | None = None,
):
    if isinstance(status_code, dict):
        options = dict(status_code)
        raw_status = options.pop("status", options.pop("status_code", 200))
        status = raw_status if raw_status == "default" else int(raw_status)
        description = options.pop("description", description)
        model = options.pop("model", options.pop("type", model))
        schema = options.pop("schema", schema)
        content = options.pop("content", content)
        headers = options.pop("headers", headers)
        examples = options.pop("examples", examples)
        is_array = bool(options.pop("isArray", options.pop("is_array", False)))
        extra_options = options
    else:
        status = status_code
        is_array = False
        extra_options = {}

    def decorator(handler):
        responses = _response_metadata(handler)
        response: dict[str, Any] = dict(extra_options)
        if description:
            response["description"] = description
        if model:
            response["model"] = model
            if not schema and not content:
                schema_for_model = _schema_for_type(model, is_array=is_array)
                if schema_for_model:
                    response["content"] = {"application/json": {"schema": schema_for_model}}
        if headers:
            response["headers"] = headers
        if content:
            response["content"] = content
        elif schema:
            response["content"] = {"application/json": {"schema": schema}}
        if examples:
            response.setdefault("content", {}).setdefault("application/json", {})["examples"] = examples
        responses[status] = response
        route = getattr(handler, "__fanest_route__", None)
        if route is not None:
            route.options["responses"] = responses
        setattr(handler, "__fanest_pending_responses__", responses)
        return handler

    return decorator


def ApiOkResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 200, "description": description, "model": model, **options})


def ApiCreatedResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 201, "description": description, "model": model, **options})


def ApiAcceptedResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 202, "description": description, "model": model, **options})


def ApiNoContentResponse(description: str | None = None):
    return ApiResponse(204, description)


def ApiDefaultResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": "default", "description": description, "model": model, **options})


def ApiMovedPermanentlyResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 301, "description": description, "model": model, **options})


def ApiFoundResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 302, "description": description, "model": model, **options})


def ApiBadRequestResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 400, "description": description, "model": model, **options})


def ApiUnauthorizedResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 401, "description": description, "model": model, **options})


def ApiForbiddenResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 403, "description": description, "model": model, **options})


def ApiNotFoundResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 404, "description": description, "model": model, **options})


def ApiConflictResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 409, "description": description, "model": model, **options})


def ApiUnprocessableEntityResponse(
    description: str | None = None,
    model: type | None = None,
    **options: Any,
):
    return ApiResponse({"status": 422, "description": description, "model": model, **options})


def ApiTooManyRequestsResponse(description: str | None = None, model: type | None = None, **options: Any):
    return ApiResponse({"status": 429, "description": description, "model": model, **options})


def ApiInternalServerErrorResponse(
    description: str | None = None,
    model: type | None = None,
    **options: Any,
):
    return ApiResponse({"status": 500, "description": description, "model": model, **options})


def ApiServiceUnavailableResponse(
    description: str | None = None,
    model: type | None = None,
    **options: Any,
):
    return ApiResponse({"status": 503, "description": description, "model": model, **options})


def ApiOperation(
    *,
    summary: str | None = None,
    description: str | None = None,
    operation_id: str | None = None,
    deprecated: bool | None = None,
    tags: list[str] | None = None,
):
    def decorator(handler):
        _set_route_option(handler, "summary", summary)
        _set_route_option(handler, "description", description)
        _set_route_option(handler, "operation_id", operation_id)
        _set_route_option(handler, "deprecated", deprecated)
        _set_route_option(handler, "tags", tags)
        return handler

    return decorator


def ApiParam(
    name: str,
    description: str | None = None,
    *,
    required: bool = True,
    schema: dict[str, Any] | None = None,
    type: Any | None = None,
    enum: list[Any] | None = None,
    example: Any = None,
    deprecated: bool | None = None,
):
    def decorator(target):
        _append_openapi_extra(
            target,
            "parameters",
            _parameter(
                name,
                "path",
                description,
                required=required,
                schema=schema,
                type=type,
                enum=enum,
                example=example,
                deprecated=deprecated,
            ),
        )
        return target

    return decorator


def ApiQuery(
    name: str,
    description: str | None = None,
    required: bool = False,
    *,
    schema: dict[str, Any] | None = None,
    type: Any | None = None,
    enum: list[Any] | None = None,
    example: Any = None,
    deprecated: bool | None = None,
):
    def decorator(target):
        _append_openapi_extra(
            target,
            "parameters",
            _parameter(
                name,
                "query",
                description,
                required=required,
                schema=schema,
                type=type,
                enum=enum,
                example=example,
                deprecated=deprecated,
            ),
        )
        return target

    return decorator


def ApiBody(
    description: str | None = None,
    *,
    type: Any | None = None,
    schema: dict[str, Any] | None = None,
    required: bool | None = None,
    examples: dict[str, Any] | None = None,
    content_type: str = "application/json",
    is_array: bool = False,
):
    def decorator(target):
        body: dict[str, Any] = {}
        if description:
            body["description"] = description
        if required is not None:
            body["required"] = required
        resolved_schema = schema or _schema_for_type(type, is_array=is_array)
        if resolved_schema:
            body["content"] = {content_type: {"schema": resolved_schema}}
        if examples:
            body.setdefault("content", {}).setdefault(content_type, {})["examples"] = examples
        if body:
            _merge_openapi_extra(target, {"requestBody": body})
        return target

    return decorator


def ApiHeader(
    name: str,
    description: str | None = None,
    required: bool = False,
    *,
    schema: dict[str, Any] | None = None,
    example: Any = None,
    deprecated: bool | None = None,
):
    def decorator(target):
        _append_openapi_extra(
            target,
            "parameters",
            _parameter(
                name,
                "header",
                description,
                required=required,
                schema=schema,
                example=example,
                deprecated=deprecated,
            ),
        )
        return target

    return decorator


def ApiCookie(
    name: str,
    description: str | None = None,
    required: bool = False,
    *,
    schema: dict[str, Any] | None = None,
    example: Any = None,
    deprecated: bool | None = None,
):
    def decorator(target):
        _append_openapi_extra(
            target,
            "parameters",
            _parameter(
                name,
                "cookie",
                description,
                required=required,
                schema=schema,
                example=example,
                deprecated=deprecated,
            ),
        )
        return target

    return decorator


def ApiConsumes(*mime_types: str):
    def decorator(target):
        _merge_openapi_extra(
            target,
            {
                "requestBody": {
                    "content": {
                        mime_type: {} for mime_type in mime_types
                    }
                }
            },
        )
        return target

    return decorator


def ApiProduces(*mime_types: str):
    def decorator(target):
        _merge_openapi_extra(
            target,
            {
                "responses": {
                    "200": {
                        "content": {
                            mime_type: {"schema": {"type": "object"}} for mime_type in mime_types
                        }
                    }
                }
            },
        )
        return target

    return decorator


def ApiExcludeController():
    def decorator(target: T) -> T:
        setattr(target, "__fanest_swagger_exclude_controller__", True)
        return target

    return decorator


def ApiExcludeEndpoint():
    def decorator(handler):
        _set_route_option(handler, "include_in_schema", False)
        return handler

    return decorator


def ApiSecurity(name: str, scopes: list[str] | None = None) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        securities = list(getattr(target, "__fanest_security__", []))
        securities.append({name: scopes or []})
        setattr(target, "__fanest_security__", securities)
        return target

    return decorator


def ApiOAuth2(scopes: list[str] | None = None, name: str = "oauth2") -> Callable[[T], T]:
    return ApiSecurity(name, scopes or [])


def ApiBearerAuth(name: str = "bearer") -> Callable[[T], T]:
    def decorator(target: T) -> T:
        if name == "bearer":
            setattr(target, "__fanest_bearer_auth__", True)
            return target
        return ApiSecurity(name)(target)

    return decorator


def ApiBasicAuth(name: str = "basic") -> Callable[[T], T]:
    return ApiSecurity(name)


def ApiCookieAuth(name: str = "cookie") -> Callable[[T], T]:
    return ApiSecurity(name)


def ApiExtraModels(*models: type) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        existing = list(getattr(target, "__fanest_extra_models__", []))
        existing.extend(models)
        setattr(target, "__fanest_extra_models__", existing)
        for model in models:
            if model not in _FANEST_EXTRA_MODELS:
                _FANEST_EXTRA_MODELS.append(model)
        return target

    return decorator


def get_schema_path(model: type | str) -> str:
    name = model if isinstance(model, str) else getattr(model, "__fanest_schema_name__", model.__name__)
    if inspect.isclass(model) and model not in _FANEST_EXTRA_MODELS:
        _FANEST_EXTRA_MODELS.append(model)
    return f"#/components/schemas/{name}"


def ApiSchema(*, name: str | None = None, description: str | None = None) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_schema_name__", name or getattr(target, "__name__", None))
        if description is not None:
            setattr(target, "__fanest_schema_description__", description)
        if inspect.isclass(target) and target not in _FANEST_EXTRA_MODELS:
            _FANEST_EXTRA_MODELS.append(target)
        return target

    return decorator


def one_of(*models_or_schemas: Any, discriminator: dict[str, Any] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"oneOf": [_schema_ref_or_inline(item) for item in models_or_schemas]}
    if discriminator is not None:
        schema["discriminator"] = discriminator
    return schema


def any_of(*models_or_schemas: Any) -> dict[str, Any]:
    return {"anyOf": [_schema_ref_or_inline(item) for item in models_or_schemas]}


def all_of(*models_or_schemas: Any) -> dict[str, Any]:
    return {"allOf": [_schema_ref_or_inline(item) for item in models_or_schemas]}


def ApiExtraSchema(schema: dict[str, Any]) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _merge_openapi_extra(target, schema)
        return target

    return decorator


def ApiExtension(name: str, value: Any) -> Callable[[T], T]:
    extension_name = name if name.startswith("x-") else f"x-{name}"

    def decorator(target: T) -> T:
        if inspect.isfunction(target) or hasattr(target, "__fanest_route__"):
            _merge_openapi_extra(target, {extension_name: value})
        else:
            extensions = dict(getattr(target, "__fanest_openapi_extensions__", {}))
            extensions[extension_name] = value
            setattr(target, "__fanest_openapi_extensions__", extensions)
        return target

    return decorator


def ApiProperty(
    default: Any = ...,
    *,
    description: str | None = None,
    example: Any = None,
    examples: list[Any] | None = None,
    deprecated: bool | None = None,
    type: Any | None = None,
    enum: Any | None = None,
    is_array: bool = False,
    nullable: bool | None = None,
    one_of: list[Any] | None = None,
    any_of: list[Any] | None = None,
    all_of: list[Any] | None = None,
    discriminator: dict[str, Any] | None = None,
    **extra: Any,
) -> Any:
    kwargs = dict(extra)
    if description is not None:
        kwargs["description"] = description
    if example is not None:
        kwargs["examples"] = [example]
    if examples is not None:
        kwargs["examples"] = examples
    if deprecated is not None:
        kwargs["deprecated"] = deprecated
    schema_extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    schema = _schema_for_type(type, is_array=is_array)
    if schema is not None:
        schema_extra.update(schema)
    if enum is not None:
        enum_values = _enum_values(enum)
        if enum_values is not None:
            schema_extra["enum"] = enum_values
    if nullable is not None:
        schema_extra["nullable"] = nullable
    if one_of is not None:
        schema_extra["x-fanest-oneOf"] = [_schema_marker_or_inline(item) for item in one_of]
    if any_of is not None:
        schema_extra["x-fanest-anyOf"] = [_schema_marker_or_inline(item) for item in any_of]
    if all_of is not None:
        schema_extra["x-fanest-allOf"] = [_schema_marker_or_inline(item) for item in all_of]
    if discriminator is not None:
        schema_extra["discriminator"] = discriminator
    if schema_extra:
        kwargs["json_schema_extra"] = schema_extra
    return Field(default, **kwargs)


def ApiPropertyOptional(
    default: Any = None,
    *,
    description: str | None = None,
    example: Any = None,
    examples: list[Any] | None = None,
    deprecated: bool | None = None,
    type: Any | None = None,
    enum: Any | None = None,
    is_array: bool = False,
    nullable: bool | None = None,
    one_of: list[Any] | None = None,
    any_of: list[Any] | None = None,
    all_of: list[Any] | None = None,
    discriminator: dict[str, Any] | None = None,
    **extra: Any,
) -> Any:
    return ApiProperty(
        default,
        description=description,
        example=example,
        examples=examples,
        deprecated=deprecated,
        type=type,
        enum=enum,
        is_array=is_array,
        nullable=nullable,
        one_of=one_of,
        any_of=any_of,
        all_of=all_of,
        discriminator=discriminator,
        **extra,
    )


def ApiHideProperty(default: Any = None) -> Any:
    return Field(default, exclude=True, json_schema_extra={"hidden": True})


def _set_route_option(handler: Any, key: str, value: Any) -> None:
    if value is None:
        return
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        route.options[key] = value
        return
    pending = dict(getattr(handler, "__fanest_pending_route_options__", {}))
    pending[key] = value
    setattr(handler, "__fanest_pending_route_options__", pending)


def _append_openapi_extra(target: Any, key: str, value: Any) -> None:
    extra = _openapi_extra(target)

    if key == "parameters":
        extra[key] = [*extra.get(key, []), value]
    else:
        extra[key] = value

    route = getattr(target, "__fanest_route__", None)
    if route is not None:
        route.options["openapi_extra"] = extra
    else:
        setattr(target, "__fanest_pending_openapi_extra__", extra)


def _merge_openapi_extra(target: Any, value: dict[str, Any]) -> None:
    extra = _openapi_extra(target)
    _deep_update(extra, value)
    route = getattr(target, "__fanest_route__", None)
    if route is not None:
        route.options["openapi_extra"] = extra
    else:
        setattr(target, "__fanest_pending_openapi_extra__", extra)


def _openapi_extra(target: Any) -> dict[str, Any]:
    route = getattr(target, "__fanest_route__", None)
    if route is not None:
        return deepcopy(route.options.get("openapi_extra", {}))
    return deepcopy(getattr(target, "__fanest_pending_openapi_extra__", {}))


def _response_metadata(handler: Any) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    pending = getattr(handler, "__fanest_pending_responses__", None)
    if pending:
        responses.update(pending)
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        responses.update(route.options.get("responses", {}))
    return responses


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "schema":
            target[key] = value
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _parameter(
    name: str,
    location: str,
    description: str | None,
    *,
    required: bool | None = None,
    schema: dict[str, Any] | None = None,
    type: Any | None = None,
    enum: list[Any] | None = None,
    example: Any = None,
    deprecated: bool | None = None,
) -> dict[str, Any]:
    parameter: dict[str, Any] = {
        "name": name,
        "in": location,
        "schema": schema or _schema_for_type(type) or {"type": "string"},
    }
    if description:
        parameter["description"] = description
    if enum is not None:
        parameter["schema"]["enum"] = enum
    if example is not None:
        parameter["example"] = example
    if deprecated is not None:
        parameter["deprecated"] = deprecated
    if required is not None:
        parameter["required"] = required
    elif location == "path":
        parameter["required"] = True
    return parameter


def _schema_for_type(type_: Any, *, is_array: bool = False) -> dict[str, Any] | None:
    if type_ is None:
        return None
    origin = get_origin(type_)
    args = get_args(type_)
    schema: dict[str, Any]
    if origin in {list, tuple, set} and args:
        schema = {"type": "array", "items": _schema_for_type(args[0]) or {}}
        is_array = False
    elif origin in {Literal}:
        values = list(args)
        schema = {"enum": values}
        if values:
            primitive = _primitive_openapi_type(type(values[0]))
            if primitive is not None:
                schema["type"] = primitive
    elif origin in {UnionType, Union}:
        none_type = type(None)
        variants = [arg for arg in args if arg is not none_type]
        if len(variants) == 1:
            schema = _schema_for_type(variants[0]) or {}
            if none_type in args:
                schema["nullable"] = True
        else:
            schema = {"anyOf": [_schema_for_type(arg) or {} for arg in variants]}
            if none_type in args:
                schema["nullable"] = True
    elif hasattr(type_, "model_json_schema"):
        schema = {"$ref": get_schema_path(type_)}
        if type_ not in _FANEST_EXTRA_MODELS:
            _FANEST_EXTRA_MODELS.append(type_)
    elif inspect.isclass(type_) and issubclass(type_, Enum):
        schema = {"type": "string", "enum": [item.value for item in type_]}
    elif primitive_type := _primitive_openapi_type(type_):
        schema = {"type": primitive_type}
    else:
        schema = {"type": "string"}
    if is_array:
        return {"type": "array", "items": schema}
    return schema


def _schema_ref_or_inline(model_or_schema: Any) -> dict[str, Any]:
    if isinstance(model_or_schema, dict):
        return model_or_schema
    if isinstance(model_or_schema, str):
        return {"$ref": model_or_schema} if model_or_schema.startswith("#/") else {"type": model_or_schema}
    return _schema_for_type(model_or_schema) or {"type": "string"}


def _schema_marker_or_inline(model_or_schema: Any) -> dict[str, Any]:
    if isinstance(model_or_schema, dict):
        return model_or_schema
    if isinstance(model_or_schema, str):
        return {"$ref": model_or_schema} if model_or_schema.startswith("#/") else {"type": model_or_schema}
    if inspect.isclass(model_or_schema):
        if model_or_schema not in _FANEST_EXTRA_MODELS:
            _FANEST_EXTRA_MODELS.append(model_or_schema)
        return {"x-fanest-ref": getattr(model_or_schema, "__fanest_schema_name__", model_or_schema.__name__)}
    return _schema_for_type(model_or_schema) or {"type": "string"}


def _primitive_openapi_type(value: Any) -> str | None:
    mapping = {
        str: "string",
        "string": "string",
        int: "integer",
        "integer": "integer",
        float: "number",
        "number": "number",
        bool: "boolean",
        "boolean": "boolean",
        bytes: "string",
    }
    return mapping.get(value)


def _enum_values(enum: Any) -> list[Any] | None:
    if inspect.isclass(enum) and issubclass(enum, Enum):
        return [item.value for item in enum]
    if isinstance(enum, list | tuple | set):
        return list(enum)
    return None
