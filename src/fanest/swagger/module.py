from typing import Any

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html


class DocumentBuilder:
    def __init__(self) -> None:
        self._config: dict[str, Any] = {
            "title": "FaNest Application",
            "version": "0.1.0",
            "description": None,
            "servers": [],
            "tags": [],
            "components": {"securitySchemes": {}},
            "security": [],
        }

    def set_title(self, title: str) -> "DocumentBuilder":
        self._config["title"] = title
        return self

    def set_description(self, description: str) -> "DocumentBuilder":
        self._config["description"] = description
        return self

    def set_version(self, version: str) -> "DocumentBuilder":
        self._config["version"] = version
        return self

    def add_server(self, url: str, description: str | None = None) -> "DocumentBuilder":
        server: dict[str, Any] = {"url": url}
        if description:
            server["description"] = description
        self._config["servers"].append(server)
        return self

    def add_tag(self, name: str, description: str | None = None) -> "DocumentBuilder":
        tag: dict[str, Any] = {"name": name}
        if description:
            tag["description"] = description
        self._config["tags"].append(tag)
        return self

    def add_bearer_auth(self, name: str = "bearer") -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        self._config["security"].append({name: []})
        return self

    def add_basic_auth(self, name: str = "basic") -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = {
            "type": "http",
            "scheme": "basic",
        }
        self._config["security"].append({name: []})
        return self

    def add_api_key(
        self,
        *,
        name: str = "api_key",
        header_name: str = "x-api-key",
        location: str = "header",
    ) -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = {
            "type": "apiKey",
            "name": header_name,
            "in": location,
        }
        self._config["security"].append({name: []})
        return self

    def build(self) -> dict[str, Any]:
        return self._config


class SwaggerModule:
    @staticmethod
    def create_document(app: FastAPI, config: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = app.openapi()
        config = config or {}
        info = schema.setdefault("info", {})
        for key in ["title", "version", "description"]:
            if config.get(key):
                info[key] = config[key]
        if config.get("servers"):
            schema["servers"] = config["servers"]
        if config.get("tags"):
            schema["tags"] = config["tags"]
        components = config.get("components")
        if components:
            schema_components = schema.setdefault("components", {})
            for key, value in components.items():
                if isinstance(value, dict):
                    schema_components.setdefault(key, {}).update(value)
                else:
                    schema_components[key] = value
        if config.get("security"):
            schema["security"] = config["security"]
        return schema

    @staticmethod
    def setup(path: str, app: FastAPI, document: dict[str, Any]) -> None:
        schema_path = f"{path.rstrip('/')}/openapi.json"

        @app.get(schema_path, include_in_schema=False)
        async def openapi_schema():
            return document

        @app.get(path, include_in_schema=False)
        async def swagger_ui():
            return get_swagger_ui_html(openapi_url=schema_path, title=document["info"]["title"])
