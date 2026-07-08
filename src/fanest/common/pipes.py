from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from fanest.common.exceptions import BadRequestException


class ValidationPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        annotation = metadata.get("annotation")
        if annotation is None or annotation is Any:
            return value
        try:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if isinstance(value, annotation):
                    return value
                return annotation.model_validate(value)
            return TypeAdapter(annotation).validate_python(value)
        except ValidationError as exc:
            raise BadRequestException(exc.errors()) from exc


class ParseIntPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be an integer") from exc


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
            return float(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a float") from exc


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
            return value
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
