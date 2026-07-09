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


class NotImplementedException(FaNestHttpException):
    def __init__(self, detail: Any = "Not Implemented"):
        super().__init__(501, detail)


class MethodNotAllowedException(FaNestHttpException):
    def __init__(self, detail: Any = "Method Not Allowed"):
        super().__init__(405, detail)


class GoneException(FaNestHttpException):
    def __init__(self, detail: Any = "Gone"):
        super().__init__(410, detail)


class PayloadTooLargeException(FaNestHttpException):
    def __init__(self, detail: Any = "Payload Too Large"):
        super().__init__(413, detail)


class UnprocessableEntityException(FaNestHttpException):
    def __init__(self, detail: Any = "Unprocessable Entity"):
        super().__init__(422, detail)


class TooManyRequestsException(FaNestHttpException):
    def __init__(self, detail: Any = "Too Many Requests"):
        super().__init__(429, detail)


class RequestTimeoutException(FaNestHttpException):
    def __init__(self, detail: Any = "Request Timeout"):
        super().__init__(408, detail)


class UnsupportedMediaTypeException(FaNestHttpException):
    def __init__(self, detail: Any = "Unsupported Media Type"):
        super().__init__(415, detail)


class InternalServerErrorException(FaNestHttpException):
    def __init__(self, detail: Any = "Internal Server Error"):
        super().__init__(500, detail)


class ServiceUnavailableException(FaNestHttpException):
    def __init__(self, detail: Any = "Service Unavailable"):
        super().__init__(503, detail)


class GatewayTimeoutException(FaNestHttpException):
    def __init__(self, detail: Any = "Gateway Timeout"):
        super().__init__(504, detail)


def Catch(*exceptions: type[Exception]):
    def decorator(cls):
        setattr(cls, "__fanest_catch_exceptions__", exceptions or (Exception,))
        return cls

    return decorator


class BaseExceptionFilter:
    def catch(self, exc: Exception, context):
        raise exc
