from enum import Enum
import inspect
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from fanest.common.exceptions import BadRequestException, FaNestHttpException
from fanest.common.pydantic_compat import (
    BaseModel,
    ValidationError,
    pydantic_model_fields,
    pydantic_validate_model,
    pydantic_validate_type,
)


class ValidationPipe:
    def __init__(
        self,
        *,
        transform: bool = True,
        whitelist: bool = False,
        forbid_non_whitelisted: bool = False,
        forbid_unknown_values: bool = False,
        skip_missing_properties: bool = False,
        skip_null_properties: bool = False,
        stop_at_first_error: bool = False,
        disable_error_messages: bool = False,
        error_http_status_code: int = 400,
        exception_factory: Callable[[list[dict[str, Any]]], Exception] | None = None,
    ) -> None:
        self.transform_enabled = transform
        self.whitelist = whitelist
        self.forbid_non_whitelisted = forbid_non_whitelisted
        self.forbid_unknown_values = forbid_unknown_values
        self.skip_missing_properties = skip_missing_properties
        self.skip_null_properties = skip_null_properties
        self.stop_at_first_error = stop_at_first_error
        self.disable_error_messages = disable_error_messages
        self.error_http_status_code = error_http_status_code
        self.exception_factory = exception_factory

    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        annotation = metadata.get("annotation")
        # An unannotated parameter (e.g. NestJS-style ``q=Query("q")``) arrives
        # with an empty annotation; treat it like Any so TypeAdapter is never
        # asked to build a schema for ``inspect._empty`` (which 500s).
        if annotation is None or annotation is Any or annotation is inspect.Parameter.empty:
            if self.forbid_unknown_values and value is not None:
                self._raise_errors(
                    [{"type": "unknown_value", "loc": (), "msg": "Unknown value", "input": value}]
                )
            return value
        # FastAPI may have already parsed a Body DTO into a model instance before
        # this pipe runs; still honour forbid_non_whitelisted / whitelist against
        # any extra fields the model retained (extra="allow").
        if isinstance(value, BaseModel):
            return self._apply_options_to_model(value)
        try:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                self._check_forbidden_extra_fields(annotation, value)
                value = self._skip_null_fields(value)
                if isinstance(value, annotation):
                    return value
                validated = pydantic_validate_model(annotation, value)
                return (
                    validated
                    if self.transform_enabled
                    else self._strip_extra_fields(annotation, value)
                )
            validated = pydantic_validate_type(annotation, value)
            return validated if self.transform_enabled else value
        except ValidationError as exc:
            errors = self._filter_errors(_json_safe_errors(exc.errors()))
            if not errors and isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if (
                    self.transform_enabled
                    and hasattr(annotation, "model_construct")
                    and isinstance(value, dict)
                ):
                    return annotation.model_construct(**self._strip_extra_fields(annotation, value))
                return self._strip_extra_fields(annotation, value)
            self._raise_errors(errors, cause=exc)

    def _apply_options_to_model(self, model: BaseModel) -> Any:
        extra = getattr(model, "model_extra", None) or {}
        if self.forbid_non_whitelisted and extra:
            errors = [
                {
                    "type": "extra_forbidden",
                    "loc": (field,),
                    "msg": "Extra inputs are not permitted",
                    "input": item,
                }
                for field, item in sorted(extra.items())
            ]
            self._raise_errors(self._filter_errors(errors))
        if self.whitelist and extra:
            # Drop the fields not declared on the model (property whitelisting).
            declared = set(pydantic_model_fields(type(model)))
            kept = {key: item for key, item in model.__dict__.items() if key in declared}
            return type(model).model_construct(**kept)
        return model

    def _check_forbidden_extra_fields(self, annotation: type[BaseModel], value: Any) -> None:
        if not self.forbid_non_whitelisted or not isinstance(value, dict):
            return
        extra_fields = sorted(set(value) - set(pydantic_model_fields(annotation)))
        if extra_fields:
            errors = [
                {
                    "type": "extra_forbidden",
                    "loc": (field,),
                    "msg": "Extra inputs are not permitted",
                    "input": value[field],
                }
                for field in extra_fields
            ]
            self._raise_errors(self._filter_errors(errors))

    def _strip_extra_fields(self, annotation: type[BaseModel], value: Any) -> Any:
        if not self.whitelist or not isinstance(value, dict):
            return value
        return {key: value[key] for key in pydantic_model_fields(annotation) if key in value}

    def _skip_null_fields(self, value: Any) -> Any:
        if not self.skip_null_properties or not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items() if item is not None}

    def _filter_errors(self, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.skip_missing_properties:
            errors = [error for error in errors if error.get("type") != "missing"]
        if self.skip_null_properties:
            errors = [error for error in errors if error.get("input") is not None]
        if self.stop_at_first_error and errors:
            errors = errors[:1]
        if self.disable_error_messages:
            return [{"type": error.get("type"), "loc": error.get("loc")} for error in errors]
        return errors

    def _raise_errors(
        self,
        errors: list[dict[str, Any]],
        *,
        cause: Exception | None = None,
    ) -> None:
        if self.exception_factory is not None:
            raise self.exception_factory(errors) from cause
        exception: Exception
        if self.error_http_status_code == 400:
            exception = BadRequestException(errors)
        else:
            exception = FaNestHttpException(self.error_http_status_code, errors)
        raise exception from cause


def _json_safe_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    safe_errors: list[dict[str, Any]] = []
    for error in errors:
        safe_error = dict(error)
        if "ctx" in safe_error and isinstance(safe_error["ctx"], dict):
            safe_error["ctx"] = {
                str(key): _json_safe_error_value(value)
                for key, value in safe_error["ctx"].items()
            }
        safe_errors.append(safe_error)
    return safe_errors


def _json_safe_error_value(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe_error_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_json_safe_error_value(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _json_safe_error_value(item) for key, item in value.items()}
    return str(value)


_INT_STRING = re.compile(r"-?[0-9]+")


class ParseIntPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> int:
        if isinstance(value, bool):
            raise BadRequestException(f"{metadata.get('name', 'value')} must be an integer")
        if isinstance(value, int):
            return value
        # NestJS validates the raw string with /^-?\d+$/ (ASCII, anchored) and
        # rejects underscores, surrounding whitespace and non-ASCII digits that
        # Python's int() would otherwise accept.
        if isinstance(value, str) and _INT_STRING.fullmatch(value):
            return int(value)
        raise BadRequestException(f"{metadata.get('name', 'value')} must be an integer")


class ParseBoolPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise BadRequestException(f"{metadata.get('name', 'value')} must be a boolean")


class ParseFloatPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a float") from exc
        if not math.isfinite(parsed):
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a finite float")
        return parsed


class ParseUUIDPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a UUID") from exc


class ParseEnumPipe:
    def __init__(self, enum: type[Enum]):
        self.enum = enum

    def transform(self, value: Any, metadata: dict[str, Any]) -> Enum:
        try:
            return self.enum(value)
        except ValueError as exc:
            allowed = ", ".join(str(item.value) for item in self.enum)
            raise BadRequestException(
                f"{metadata.get('name', 'value')} must be one of: {allowed}"
            ) from exc


class ParseArrayPipe:
    def __init__(self, separator: str = ","):
        self.separator = separator

    def transform(self, value: Any, metadata: dict[str, Any]) -> list[Any]:
        if isinstance(value, list):
            # FastAPI resolves a list-annotated query param to a single-element
            # list holding the raw "1,2,3" string before pipes run — still split
            # each element on the separator so ?ids=1,2,3 yields ['1','2','3'].
            result: list[Any] = []
            for item in value:
                if isinstance(item, str) and self.separator in item:
                    result.extend(part for part in item.split(self.separator) if part != "")
                else:
                    result.append(item)
            return result
        if isinstance(value, str):
            return [item for item in value.split(self.separator) if item != ""]
        raise BadRequestException(f"{metadata.get('name', 'value')} must be an array")


class DefaultValuePipe:
    def __init__(self, default: Any):
        self.default = default

    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        if value is None:
            return self.default
        return value


class FileValidator:
    def is_valid(self, file: Any) -> bool:
        raise NotImplementedError

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} failed validation"


class MaxFileSizeValidator(FileValidator):
    def __init__(self, max_size: int):
        self.max_size = max_size

    def is_valid(self, file: Any) -> bool:
        size = getattr(file, "size", None)
        if size is None:
            raw_file = getattr(file, "file", None)
            if raw_file is not None:
                try:
                    position = raw_file.tell()
                    raw_file.seek(0, 2)
                    size = raw_file.tell()
                    raw_file.seek(position)
                except (AttributeError, OSError, TypeError, ValueError):
                    size = None
        return size is None or int(size) <= self.max_size

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} exceeds {self.max_size} bytes"


class FileTypeValidator(FileValidator):
    def __init__(self, file_type: str | re.Pattern[str] | Callable[[Any], bool]):
        self.file_type = file_type

    def is_valid(self, file: Any) -> bool:
        if callable(self.file_type):
            return bool(self.file_type(file))
        content_type = getattr(file, "content_type", None) or ""
        filename = getattr(file, "filename", None) or ""
        target = f"{content_type} {filename}"
        if isinstance(self.file_type, re.Pattern):
            return bool(self.file_type.search(target))
        return content_type == self.file_type or filename.endswith(self.file_type)

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} has an invalid file type"


class ParseFilePipe:
    def __init__(
        self,
        validators: Iterable[FileValidator] | None = None,
        *,
        file_is_required: bool = True,
    ) -> None:
        self.validators = list(validators or [])
        self.file_is_required = file_is_required

    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        if value is None:
            if self.file_is_required:
                raise BadRequestException(f"{metadata.get('name', 'file')} is required")
            return value
        files = value if isinstance(value, list) else [value]
        for file in files:
            for validator in self.validators:
                if not validator.is_valid(file):
                    raise BadRequestException(validator.build_error_message(file))
        return value


class ParseFilePipeBuilder:
    def __init__(self) -> None:
        self.validators: list[FileValidator] = []

    def add_max_size_validator(self, max_size: int | dict[str, int]) -> "ParseFilePipeBuilder":
        if isinstance(max_size, dict):
            max_size = max_size["max_size"] if "max_size" in max_size else max_size["maxSize"]
        self.validators.append(MaxFileSizeValidator(max_size))
        return self

    def add_file_type_validator(
        self,
        file_type: str | re.Pattern[str] | Callable[[Any], bool] | dict[str, Any],
    ) -> "ParseFilePipeBuilder":
        resolved: str | re.Pattern[str] | Callable[[Any], bool]
        if isinstance(file_type, dict):
            resolved = file_type["file_type"] if "file_type" in file_type else file_type["fileType"]
        else:
            resolved = file_type
        self.validators.append(FileTypeValidator(resolved))
        return self

    def build(self, *, file_is_required: bool = True) -> ParseFilePipe:
        return ParseFilePipe(self.validators, file_is_required=file_is_required)
