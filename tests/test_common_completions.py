from enum import Enum
from uuid import UUID

from fanest import (
    GoneException,
    ParseArrayPipe,
    ParseEnumPipe,
    ParseFloatPipe,
    ParseUUIDPipe,
    PayloadTooLargeException,
    ServiceUnavailableException,
    TooManyRequestsException,
    UnprocessableEntityException,
)


class Status(Enum):
    ACTIVE = "active"


def test_additional_parse_pipes():
    metadata = {"name": "value"}

    assert ParseFloatPipe().transform("1.5", metadata) == 1.5
    assert ParseUUIDPipe().transform("00000000-0000-0000-0000-000000000000", metadata) == UUID(
        "00000000-0000-0000-0000-000000000000"
    )
    assert ParseEnumPipe(Status).transform("active", metadata) is Status.ACTIVE
    assert ParseArrayPipe().transform("a,b", metadata) == ["a", "b"]


def test_additional_http_exceptions():
    assert GoneException().status_code == 410
    assert PayloadTooLargeException().status_code == 413
    assert UnprocessableEntityException().status_code == 422
    assert TooManyRequestsException().status_code == 429
    assert ServiceUnavailableException().status_code == 503
