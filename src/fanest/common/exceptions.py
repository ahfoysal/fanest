from typing import Any

from fastapi import HTTPException


class FaNestHttpException(HTTPException):
    def __init__(self, status_code: int, detail: Any = None, headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class BadRequestException(FaNestHttpException):
    def __init__(self, detail: Any = "Bad Request"):
        super().__init__(400, detail)


class UnauthorizedException(FaNestHttpException):
    def __init__(self, detail: Any = "Unauthorized"):
        super().__init__(401, detail)


class ForbiddenException(FaNestHttpException):
    def __init__(self, detail: Any = "Forbidden"):
        super().__init__(403, detail)


class NotFoundException(FaNestHttpException):
    def __init__(self, detail: Any = "Not Found"):
        super().__init__(404, detail)


class ConflictException(FaNestHttpException):
    def __init__(self, detail: Any = "Conflict"):
        super().__init__(409, detail)


class InternalServerErrorException(FaNestHttpException):
    def __init__(self, detail: Any = "Internal Server Error"):
        super().__init__(500, detail)
