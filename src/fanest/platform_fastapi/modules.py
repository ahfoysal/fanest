from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware


def enable_compression(app: FastAPI, *, minimum_size: int = 500) -> None:
    app.add_middleware(GZipMiddleware, minimum_size=minimum_size)


def serve_static(app: FastAPI, path: str, directory: str, *, name: str = "static") -> None:
    app.mount(path, StaticFiles(directory=directory), name=name)
