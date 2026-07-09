from enum import Enum
from uuid import UUID

from pydantic import BaseModel, field_validator

from fanest import (
    BadRequestException,
    GoneException,
    ParseArrayPipe,
    ParseEnumPipe,
    ParseFloatPipe,
    ParseUUIDPipe,
    PayloadTooLargeException,
    ServiceUnavailableException,
    TooManyRequestsException,
    UnprocessableEntityException,
    ValidationPipe,
)


class Status(Enum):
    ACTIVE = "active"


class ProfileDto(BaseModel):
    name: str


class StrictDto(BaseModel):
    value: int

    @field_validator("value")
    @classmethod
    def reject_even(cls, value: int) -> int:
        if value % 2 == 0:
            raise ValueError("even values are not allowed")
        return value


def test_additional_parse_pipes():
    metadata = {"name": "value"}

    assert ParseFloatPipe().transform("1.5", metadata) == 1.5
    assert ParseUUIDPipe().transform("00000000-0000-0000-0000-000000000000", metadata) == UUID(
        "00000000-0000-0000-0000-000000000000"
    )
    assert ParseEnumPipe(Status).transform("active", metadata) is Status.ACTIVE
    assert ParseArrayPipe().transform("a,b", metadata) == ["a", "b"]

    for value in ["inf", "-inf", "nan"]:
        try:
            ParseFloatPipe().transform(value, metadata)
        except BadRequestException:
            pass
        else:  # pragma: no cover - clearer failure than a bare assert
            raise AssertionError(f"{value} should not be accepted as a finite float")


def test_additional_http_exceptions():
    assert GoneException().status_code == 410
    assert PayloadTooLargeException().status_code == 413
    assert UnprocessableEntityException().status_code == 422
    assert TooManyRequestsException().status_code == 429
    assert ServiceUnavailableException().status_code == 503


def test_validation_pipe_supports_whitelist_and_forbid_non_whitelisted():
    metadata = {"annotation": ProfileDto, "name": "body"}

    stripped = ValidationPipe(transform=False, whitelist=True).transform(
        {"name": "Ada", "role": "admin"},
        metadata,
    )
    assert stripped == {"name": "Ada"}

    try:
        ValidationPipe(forbid_non_whitelisted=True).transform(
            {"name": "Ada", "role": "admin"},
            metadata,
        )
    except BadRequestException as exc:
        assert exc.detail[0]["loc"] == ("role",)
        assert exc.detail[0]["type"] == "extra_forbidden"
    else:  # pragma: no cover - clearer failure than a bare assert
        raise AssertionError("extra fields should be rejected")


def test_validation_pipe_errors_are_json_safe():
    try:
        ValidationPipe().transform({"value": 2}, {"annotation": StrictDto, "name": "body"})
    except BadRequestException as exc:
        error = exc.detail[0]
        assert error["type"] == "value_error"
        assert isinstance(error["ctx"]["error"], str)
        assert error["ctx"]["error"] == "even values are not allowed"
    else:  # pragma: no cover - clearer failure than a bare assert
        raise AssertionError("invalid DTO should be rejected")
