import dataclasses
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


def _child_options(options: SerializeOptions) -> SerializeOptions:
    # Nested objects apply only their own class metadata and the propagated
    # groups — not the top-level handler include/exclude field filters.
    return SerializeOptions(groups=options.groups, exclude_none=options.exclude_none)


def _has_serializer_metadata(cls: type) -> bool:
    return bool(
        getattr(cls, "__fanest_serializer_fields__", None)
        or getattr(cls, "__fanest_serialize_exclude__", None) is not None
        or getattr(cls, "__fanest_serialize_expose__", None) is not None
    )


def _needs_reserialize(value: Any) -> bool:
    """Whether a (possibly nested) value carries serializer metadata that must
    be re-applied after a parent ``model_dump`` / ``asdict`` flattened it."""
    if isinstance(value, (list, tuple)):
        return any(_needs_reserialize(item) for item in value)
    if isinstance(value, Mapping):
        return any(_needs_reserialize(item) for item in value.values())
    return not isinstance(value, type) and _has_serializer_metadata(type(value))


def serialize_value(value: Any, options: SerializeOptions) -> Any:
    if isinstance(value, BaseModel):
        data = value.model_dump(
            include=options.include,
            exclude=options.exclude,
            exclude_none=options.exclude_none,
        )
        # model_dump flattens nested models/dataclasses, discarding their
        # @Exclude/@Expose metadata — re-serialize any nested value that carries
        # serializer metadata so nested exclusions are honoured too.
        child = _child_options(options)
        for name in type(value).model_fields:
            if name in data:
                attribute = getattr(value, name, None)
                if _needs_reserialize(attribute):
                    data[name] = serialize_value(attribute, child)
        return _apply_serializer_metadata(data, value.__class__, options)
    if is_dataclass(value) and not isinstance(value, type):
        data = asdict(cast(Any, value))
        child = _child_options(options)
        for dataclass_field in dataclasses.fields(value):
            name = dataclass_field.name
            if name in data:
                attribute = getattr(value, name, None)
                if _needs_reserialize(attribute):
                    data[name] = serialize_value(attribute, child)
        return _apply_serializer_metadata(data, type(value), options)
    if isinstance(value, list):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, tuple):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, Mapping):
        child = _child_options(options)
        data = {
            key: serialize_value(item, child) if _needs_reserialize(item) else item
            for key, item in value.items()
        }
        return _apply_basic_options(data, options)
    if hasattr(value, "__dict__"):
        return _apply_serializer_metadata(dict(vars(value)), type(value), options)
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
    # Class-level @Exclude() enables class-transformer's "exclude by default"
    # strategy: only fields explicitly @Expose'd (in an active group) survive.
    class_exclude_groups = getattr(cls, "__fanest_serialize_exclude__", None)
    if class_exclude_groups is not None:
        exclude_active = not class_exclude_groups or bool(active_groups.intersection(class_exclude_groups))
        if exclude_active:
            kept: dict[str, Any] = {}
            for field, value in data.items():
                field_options = metadata.get(field, {})
                if not field_options.get("exposed"):
                    continue
                expose_groups = set(field_options.get("expose_groups") or ())
                if expose_groups and not active_groups.intersection(expose_groups):
                    continue
                kept[field] = value
            data = kept
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
