from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from starlette.routing import BaseRoute
from fanest._version import __version__

from fanest.swagger.decorators import _FANEST_EXTRA_MODELS


class DocumentBuilder:
    def __init__(self) -> None:
        self._config: dict[str, Any] = {
            "title": "FaNest Application",
            "version": __version__,
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
        return self

    def add_basic_auth(self, name: str = "basic") -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = {
            "type": "http",
            "scheme": "basic",
        }
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
        return self

    def add_cookie_auth(
        self,
        cookie_name: str = "Authentication",
        *,
        name: str = "cookie",
    ) -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = {
            "type": "apiKey",
            "name": cookie_name,
            "in": "cookie",
        }
        return self

    def add_oauth2(
        self,
        *,
        name: str = "oauth2",
        flows: dict[str, Any] | None = None,
        scopes: dict[str, str] | None = None,
        authorization_url: str | None = None,
        token_url: str | None = None,
    ) -> "DocumentBuilder":
        resolved_flows = flows
        if resolved_flows is None:
            resolved_flows = {
                "authorizationCode": {
                    "authorizationUrl": authorization_url or "",
                    "tokenUrl": token_url or "",
                    "scopes": scopes or {},
                }
            }
        self._config["components"]["securitySchemes"][name] = {
            "type": "oauth2",
            "flows": resolved_flows,
        }
        return self

    def add_security(
        self,
        name: str,
        scheme: dict[str, Any],
        *,
        requirements: list[str] | None = None,
    ) -> "DocumentBuilder":
        self._config["components"]["securitySchemes"][name] = scheme
        if requirements is not None:
            self._config["security"].append({name: requirements})
        return self

    def add_global_security(
        self,
        name: str,
        scopes: list[str] | None = None,
    ) -> "DocumentBuilder":
        """Opt-in: apply a security requirement to every operation in the document.

        Defining a scheme (via ``add_bearer_auth``/``add_security``/etc.) only
        registers it under ``components.securitySchemes``; call this to make it a
        top-level (global) requirement, mirroring NestJS ``addSecurityRequirements``.
        """
        self._config["security"].append({name: scopes or []})
        return self

    def build(self) -> dict[str, Any]:
        return self._config


class SwaggerModule:
    _HTTP_METHODS = frozenset(
        {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    )

    @staticmethod
    def create_document(app: FastAPI, config: dict[str, Any] | None = None) -> dict[str, Any]:
        # app.openapi() returns FastAPI's process-cached schema dict; deep-copy
        # it before mutating so building two documents (public/admin) doesn't
        # corrupt the cache or leak one document's config into the other.
        schema = deepcopy(app.openapi())
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
        SwaggerModule._expand_fanest_schema_extensions(schema)
        SwaggerModule._add_extra_model_schemas(schema)
        SwaggerModule._strip_hidden_component_schemas(schema)
        SwaggerModule._merge_multipart_inline_schema_refs(schema)
        SwaggerModule._dedupe_operation_parameters(schema)
        return schema

    @staticmethod
    def _strip_hidden_component_schemas(schema: dict[str, Any]) -> None:
        # @ApiHideProperty fields must be dropped from every published model
        # schema (not just the extra models), and the internal "hidden" marker
        # must never leak into the OpenAPI JSON.
        schemas = schema.get("components", {}).get("schemas", {})
        if isinstance(schemas, dict):
            for model_schema in schemas.values():
                if isinstance(model_schema, dict):
                    SwaggerModule._strip_hidden_properties(model_schema)

    @staticmethod
    def setup(path: str, app: FastAPI, document: dict[str, Any]) -> None:
        docs_path = path.rstrip("/") or "/"
        schema_path = f"{docs_path}/openapi.json" if docs_path != "/" else "/openapi.json"
        app.openapi_schema = document

        def fanest_openapi() -> dict[str, Any]:
            return document

        app.openapi = fanest_openapi  # type: ignore[method-assign]
        SwaggerModule._remove_route(app, schema_path)
        SwaggerModule._remove_route(app, docs_path)
        SwaggerModule._remove_route(app, "/openapi.json")
        SwaggerModule._remove_route(app, "/docs")

        @app.get(schema_path, include_in_schema=False)
        async def openapi_schema():
            return document

        @app.get(docs_path, include_in_schema=False)
        async def swagger_ui():
            return get_swagger_ui_html(openapi_url=schema_path, title=document["info"]["title"])

        if schema_path != "/openapi.json":

            @app.get("/openapi.json", include_in_schema=False)
            async def default_openapi_schema():
                return document

        if docs_path != "/docs":

            @app.get("/docs", include_in_schema=False)
            async def default_swagger_ui():
                return get_swagger_ui_html(openapi_url="/openapi.json", title=document["info"]["title"])

    @staticmethod
    def generate_typescript_client(document: dict[str, Any], *, client_name: str = "ApiClient") -> str:
        lines = [
            f"export class {client_name} {{",
            "  constructor(private readonly baseUrl = '') {}",
            "",
        ]
        for path, methods in document.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in SwaggerModule._HTTP_METHODS or not isinstance(operation, dict):
                    continue
                operation_id = SwaggerModule._safe_method_name(
                    operation.get("operationId") or SwaggerModule._operation_name(method, path)
                )
                lines.extend(
                    [
                        f"  async {operation_id}(options: RequestInit = {{}}): Promise<Response> {{",
                        f"    return fetch(`${{this.baseUrl}}{path}`, {{ ...options, method: '{method.upper()}' }});",
                        "  }",
                        "",
                    ]
                )
        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _operation_name(method: str, path: str) -> str:
        suffix = "".join(part.title() for part in path.strip("/").replace("{", "").replace("}", "").split("/"))
        return f"{method.lower()}{suffix or 'Root'}"

    @staticmethod
    def _safe_method_name(operation_id: str) -> str:
        # OpenAPI operationIds may contain characters illegal in a TS method
        # name (hyphens, dots, spaces from a valid but non-identifier id).
        # Turn separators into camelCase and drop anything else so the emitted
        # client parses.
        cleaned: list[str] = []
        capitalize_next = False
        for index, char in enumerate(operation_id):
            if char.isalnum():
                cleaned.append(char.upper() if capitalize_next else char)
                capitalize_next = False
            else:
                capitalize_next = bool(cleaned)
        name = "".join(cleaned)
        if not name or not (name[0].isalpha() or name[0] == "_"):
            name = f"op{name}" if name else "operation"
        return name

    @staticmethod
    def _add_extra_model_schemas(schema: dict[str, Any]) -> None:
        if not _FANEST_EXTRA_MODELS:
            return
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        # ``_FANEST_EXTRA_MODELS`` is a process-global registry shared by every app
        # in the interpreter, so injecting all of it would leak unrelated models into
        # this document. Only add models that this document actually references
        # (transitively), computing references to a fixed point so a referenced model
        # can in turn pull in the models it depends on.
        pending: dict[str, type] = {}
        for model in _FANEST_EXTRA_MODELS:
            if not hasattr(model, "model_json_schema"):
                continue
            schema_name = getattr(model, "__fanest_schema_name__", model.__name__)
            pending.setdefault(schema_name, model)
        while pending:
            referenced = SwaggerModule._collect_schema_ref_names(schema)
            resolvable = [name for name in pending if name in referenced]
            if not resolvable:
                break
            for schema_name in resolvable:
                model = pending.pop(schema_name)
                try:
                    model_schema = model.model_json_schema(
                        ref_template="#/components/schemas/{model}"
                    )
                except Exception:
                    model_schema = SwaggerModule._fallback_model_schema(model)
                description = getattr(model, "__fanest_schema_description__", None)
                if description is not None:
                    model_schema["description"] = description
                SwaggerModule._expand_fanest_schema_extensions(model_schema)
                SwaggerModule._strip_hidden_properties(model_schema)
                schemas[schema_name] = model_schema

    @staticmethod
    def _collect_schema_ref_names(value: Any, found: set[str] | None = None) -> set[str]:
        if found is None:
            found = set()
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                found.add(ref.rsplit("/", 1)[-1])
            for item in value.values():
                SwaggerModule._collect_schema_ref_names(item, found)
        elif isinstance(value, list):
            for item in value:
                SwaggerModule._collect_schema_ref_names(item, found)
        return found

    @staticmethod
    def _fallback_model_schema(model: type) -> dict[str, Any]:
        schema: dict[str, Any] = {"title": getattr(model, "__name__", "Model"), "type": "object"}
        fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", {})
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, field in fields.items():
            annotation = getattr(field, "annotation", Any)
            properties[name] = SwaggerModule._schema_for_annotation(annotation)
            is_required = getattr(field, "is_required", None)
            if callable(is_required) and is_required():
                required.append(name)
        if properties:
            schema["properties"] = properties
        if required:
            schema["required"] = required
        return schema

    @staticmethod
    def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
        if annotation is str:
            return {"type": "string"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        if annotation is bool:
            return {"type": "boolean"}
        origin = getattr(annotation, "__origin__", None)
        args = getattr(annotation, "__args__", ())
        if origin is list and args:
            return {"type": "array", "items": SwaggerModule._schema_for_annotation(args[0])}
        return {"type": "object"}

    @staticmethod
    def _strip_hidden_properties(schema: dict[str, Any]) -> None:
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return
        hidden = [
            name
            for name, value in properties.items()
            if isinstance(value, dict) and value.get("hidden") is True
        ]
        for name in hidden:
            properties.pop(name, None)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [name for name in required if name not in hidden]

    @staticmethod
    def _expand_fanest_schema_extensions(value: Any) -> Any:
        if isinstance(value, list):
            for item in value:
                SwaggerModule._expand_fanest_schema_extensions(item)
            return value
        if not isinstance(value, dict):
            return value
        for openapi_key, fanest_key in (
            ("oneOf", "x-fanest-oneOf"),
            ("anyOf", "x-fanest-anyOf"),
            ("allOf", "x-fanest-allOf"),
        ):
            marker = value.pop(fanest_key, None)
            if isinstance(marker, list):
                value[openapi_key] = [
                    SwaggerModule._expand_fanest_schema_ref(item) for item in marker
                ]
        for item in value.values():
            SwaggerModule._expand_fanest_schema_extensions(item)
        return value

    @staticmethod
    def _expand_fanest_schema_ref(value: Any) -> Any:
        if isinstance(value, dict) and "x-fanest-ref" in value:
            return {"$ref": f"#/components/schemas/{value['x-fanest-ref']}"}
        SwaggerModule._expand_fanest_schema_extensions(value)
        return value

    @staticmethod
    def _dedupe_operation_parameters(schema: dict[str, Any]) -> None:
        for path_item in schema.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                parameters = operation.get("parameters")
                if not isinstance(parameters, list):
                    continue
                operation["parameters"] = SwaggerModule._dedupe_parameters(parameters)

    @staticmethod
    def _dedupe_parameters(parameters: list[Any]) -> list[Any]:
        selected: dict[tuple[str | None, str | None], Any] = {}
        order: list[tuple[str | None, str | None]] = []
        for parameter in parameters:
            if not isinstance(parameter, dict):
                key = (None, None)
            elif "$ref" in parameter:
                # Reference Object parameters have no in/name here; key them by
                # their $ref so distinct references don't all collide on (None, None).
                key = ("$ref", parameter["$ref"])
            else:
                key = (parameter.get("in"), parameter.get("name"))
            if key not in selected:
                order.append(key)
            selected[key] = parameter
        return [selected[key] for key in order]

    @staticmethod
    def _merge_multipart_inline_schema_refs(schema: dict[str, Any]) -> None:
        schemas = schema.get("components", {}).get("schemas", {})
        if not isinstance(schemas, dict):
            return
        for path_item in schema.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                content = operation.get("requestBody", {}).get("content", {})
                if not isinstance(content, dict):
                    continue
                multipart = content.get("multipart/form-data", {})
                if not isinstance(multipart, dict):
                    continue
                multipart_schema = multipart.get("schema")
                if not isinstance(multipart_schema, dict):
                    continue
                ref = multipart_schema.get("$ref")
                if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
                    continue
                schema_name = ref.rsplit("/", 1)[-1]
                target = schemas.get(schema_name)
                if isinstance(target, dict):
                    SwaggerModule._deep_merge_schema(target, multipart_schema)

    @staticmethod
    def _deep_merge_schema(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if key == "$ref":
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                SwaggerModule._deep_merge_schema(target[key], value)
                continue
            target[key] = value

    @staticmethod
    def _remove_route(app: FastAPI, path: str) -> None:
        app.router.routes = [
            route for route in app.router.routes if not SwaggerModule._route_matches_path(route, path)
        ]

    @staticmethod
    def _route_matches_path(route: BaseRoute, path: str) -> bool:
        return getattr(route, "path", None) == path
