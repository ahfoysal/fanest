import functools
from copy import deepcopy
from typing import Any, cast, get_args, get_origin

from pydantic import BaseModel, create_model, field_validator


def PartialType(model: type[BaseModel]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    graphql_fields: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        annotation = _optional(field.annotation)
        field_info = deepcopy(field)
        field_info.default = None
        field_info.default_factory = None
        fields[name] = (annotation, field_info)
        graphql_fields[name] = annotation
    mapped = cast(
        type[BaseModel],
        create_model(
            f"Partial{model.__name__}",
            __base__=BaseModel,
            __validators__=_field_validators_for(model, list(model.model_fields), optional=True),
            **cast(Any, fields),
        ),
    )
    return _copy_graphql_metadata(model, mapped, graphql_fields, f"Partial{model.__name__}")


def PickType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    graphql_fields: dict[str, Any] = {}
    for name in fields:
        field = model.model_fields[name]
        model_fields[name] = (field.annotation, deepcopy(field))
        graphql_fields[name] = field.annotation
    mapped = cast(
        type[BaseModel],
        create_model(
            f"Pick{model.__name__}",
            __base__=BaseModel,
            __validators__=_field_validators_for(model, fields),
            **cast(Any, model_fields),
        ),
    )
    return _copy_graphql_metadata(model, mapped, graphql_fields, f"Pick{model.__name__}")


def OmitType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    selected = [name for name in model.model_fields if name not in fields]
    return PickType(model, selected)


def IntersectionType(left: type[BaseModel], right: type[BaseModel]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    graphql_fields: dict[str, Any] = {}
    for model in [left, right]:
        for name, field in model.model_fields.items():
            model_fields[name] = (field.annotation, deepcopy(field))
            graphql_fields[name] = field.annotation
    mapped = cast(
        type[BaseModel],
        create_model(
            f"{left.__name__}{right.__name__}Intersection",
            __base__=BaseModel,
            __validators__={
                **{
                    f"left_{key}": value
                    for key, value in _field_validators_for(left, list(left.model_fields)).items()
                },
                **{
                    f"right_{key}": value
                    for key, value in _field_validators_for(right, list(right.model_fields)).items()
                },
            },
            **cast(Any, model_fields),
        ),
    )
    return _copy_graphql_metadata(
        left,
        mapped,
        graphql_fields,
        f"{left.__name__}{right.__name__}Intersection",
        secondary=right,
    )


def _optional(annotation: Any) -> Any:
    if get_origin(annotation) is type(None):
        return annotation
    args = get_args(annotation)
    if type(None) in args:
        return annotation
    return annotation | None


def _field_validators_for(
    model: type[BaseModel], selected_fields: list[str], *, optional: bool = False
) -> dict[str, Any]:
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is None:
        return {}
    selected = set(selected_fields)
    validators: dict[str, Any] = {}
    for name, decorator in decorators.field_validators.items():
        declared = decorator.info.fields
        if "*" in declared:
            fields: tuple[str, ...] = ("*",)
        else:
            fields = tuple(field for field in declared if field in selected)
        if not fields:
            continue
        function = getattr(decorator.func, "__func__", decorator.func)
        if optional:
            function = _optional_validator(function)
        validators[name] = field_validator(
            *fields,
            mode=decorator.info.mode,
            check_fields=False,
            json_schema_input_type=decorator.info.json_schema_input_type,
        )(classmethod(function))
    return validators


def _optional_validator(function: Any) -> Any:
    @functools.wraps(function)
    def wrapper(cls: Any, value: Any, *args: Any, **kwargs: Any) -> Any:
        if value is None:
            return None
        return function(cls, value, *args, **kwargs)

    return wrapper


def _copy_graphql_metadata(
    source: type[BaseModel],
    target: type[BaseModel],
    fields: dict[str, Any],
    name: str,
    *,
    secondary: type[BaseModel] | None = None,
) -> type[BaseModel]:
    metadata = getattr(source, "__fanest_graphql_type__", None)
    secondary_metadata = getattr(secondary, "__fanest_graphql_type__", None) if secondary is not None else None
    if metadata is None and secondary_metadata is None:
        return target
    owner = metadata or secondary_metadata
    metadata_type = cast(Any, type(owner))
    directives = list(getattr(metadata, "directives", ()))
    field_directives = dict(getattr(metadata, "field_directives", {}))
    if secondary_metadata is not None:
        directives.extend(getattr(secondary_metadata, "directives", ()))
        field_directives.update(getattr(secondary_metadata, "field_directives", {}))
    setattr(
        target,
        "__fanest_graphql_type__",
        metadata_type(
            name=name,
            kind=getattr(owner, "kind", "input"),
            fields=fields,
            federation=dict(getattr(owner, "federation", {})),
            directives=tuple(directives),
            field_directives={
                field_name: value
                for field_name, value in field_directives.items()
                if field_name in fields
            },
        ),
    )
    return target
