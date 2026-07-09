import time
from typing import Any
from uuid import uuid4

from fanest import BadRequestException, Catch, Injectable, NotFoundException
from fanest.common.pydantic_compat import pydantic_dump_model
from fastapi import HTTPException


@Injectable()
class RequestIdService:
    def new(self) -> str:
        return f"req_{uuid4().hex[:12]}"


class TimingInterceptor:
    async def intercept(self, context, call_next):
        started = time.perf_counter()
        result = await call_next()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"data": result, "meta": {"elapsed_ms": elapsed_ms}}


class GraphQLTimingInterceptor:
    async def intercept(self, context, call_next):
        return await call_next()


@Catch(NotFoundException, BadRequestException)
class ApiProblemFilter:
    def catch(self, exc: Exception, context):
        if isinstance(exc, HTTPException):
            return {
                "error": {
                    "status_code": exc.status_code,
                    "message": exc.detail,
                    "request_id": getattr(context.request.state, "request_id", None),
                }
            }
        raise exc


class RequestIdInterceptor:
    def __init__(self, request_ids: RequestIdService):
        self.request_ids = request_ids

    async def intercept(self, context, call_next):
        if context.request is not None and not hasattr(context.request.state, "request_id"):
            context.request.state.request_id = self.request_ids.new()
        return await call_next()


def json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return pydantic_dump_model(value)
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    return value
