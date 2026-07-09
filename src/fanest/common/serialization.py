from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from pydantic import BaseModel


class SerializeOptions:
    def __init__(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        exclude_none: bool = False,
        groups: set[str] | None = None,
        exclude_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.include = include
        self.exclude = exclude
        self.exclude_none = exclude_none
        self.groups = groups
        self.exclude_prefixes = exclude_prefixes


def Serialize(
    *,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    exclude_none: bool = False,
    groups: set[str] | None = None,
    exclude_prefixes: tuple[str, ...] = (),
):
    def decorator(handler):
        setattr(
            handler,
            "__fanest_serialize__",
            SerializeOptions(
                include=include,
                exclude=exclude,
                exclude_none=exclude_none,
                groups=groups,
                exclude_prefixes=exclude_prefixes,
            ),
        )
        return handler

    return decorator


def Exclude(*fields: str, groups: set[str] | None = None):
    def decorator(target):
        if fields:
            metadata = _serializer_metadata(target)
            for field in fields:
                field_options = metadata.setdefault(field, {})
                field_options["excluded"] = True
                field_options["exclude_groups"] = set(groups or ())
            return target
        setattr(target, "__fanest_serialize_exclude__", set(groups or ()))
        return target

    return decorator


def Expose(*fields: str, groups: set[str] | None = None, name: str | None = None):
    def decorator(target):
        if fields:
            metadata = _serializer_metadata(target)
            for field in fields:
                field_options = metadata.setdefault(field, {})
                field_options["exposed"] = True
                field_options["expose_groups"] = set(groups or ())
                if name is not None:
                    field_options["name"] = name
            return target
        setattr(target, "__fanest_serialize_expose__", {"groups": set(groups or ()), "name": name})
        return target

    return decorator


class ClassSerializerInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        options = getattr(context.handler, "__fanest_serialize__", SerializeOptions())
        return serialize_value(result, options)


def serialize_value(value: Any, options: SerializeOptions) -> Any:
    if isinstance(value, BaseModel):
        data = value.model_dump(
            include=options.include,
            exclude=options.exclude,
            exclude_none=options.exclude_none,
        )
        return _apply_serializer_metadata(data, value.__class__, options)
    if is_dataclass(value):
        return _apply_serializer_metadata(asdict(cast(Any, value)), type(value), options)
    if isinstance(value, list):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, tuple):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, Mapping):
        data = dict(value)
        return _apply_basic_options(data, options)
    if hasattr(value, "__dict__"):
        return _apply_serializer_metadata(dict(vars(value)), value.__class__, options)
    return value


def _serializer_metadata(target: Any) -> dict[str, dict[str, Any]]:
    metadata = dict(getattr(target, "__fanest_serializer_fields__", {}))
    setattr(target, "__fanest_serializer_fields__", metadata)
    return metadata


def _apply_basic_options(data: dict[str, Any], options: SerializeOptions) -> dict[str, Any]:
    if options.include is not None:
        data = {key: data[key] for key in options.include if key in data}
    if options.exclude is not None:
        data = {key: val for key, val in data.items() if key not in options.exclude}
    if options.exclude_prefixes:
        data = {
            key: val
            for key, val in data.items()
            if not any(str(key).startswith(prefix) for prefix in options.exclude_prefixes)
        }
    if options.exclude_none:
        data = {key: val for key, val in data.items() if val is not None}
    return data


def _apply_serializer_metadata(
    data: dict[str, Any],
    cls: type,
    options: SerializeOptions,
) -> dict[str, Any]:
    metadata = getattr(cls, "__fanest_serializer_fields__", {})
    active_groups = options.groups or set()
    for field, field_options in metadata.items():
        if field not in data:
            continue
        expose_groups = set(field_options.get("expose_groups") or ())
        exclude_groups = set(field_options.get("exclude_groups") or ())
        if (
            field_options.get("exposed")
            and expose_groups
            and not active_groups.intersection(expose_groups)
        ):
            data.pop(field, None)
            continue
        if field_options.get("excluded") and (
            not exclude_groups or active_groups.intersection(exclude_groups)
        ):
            data.pop(field, None)
            continue
        alias = field_options.get("name")
        if alias:
            data[alias] = data.pop(field)
    return _apply_basic_options(data, options)
