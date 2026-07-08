import inspect
import re
from typing import Any

from fanest import Body, Controller, Injectable, Module, Post


def Resolver(cls):
    return Injectable()(cls)


def Query(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "query", "name": name or handler.__name__})
        return handler

    return decorator


def Mutation(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "mutation", "name": name or handler.__name__})
        return handler

    return decorator


@Injectable()
class GraphQLSchema:
    def __init__(self):
        self.queries: dict[str, Any] = {}
        self.mutations: dict[str, Any] = {}

    def register_resolver(self, resolver: Any) -> None:
        for _, handler in inspect.getmembers(resolver, predicate=callable):
            metadata = getattr(handler, "__fanest_graphql__", None)
            if metadata is None:
                continue
            if metadata["kind"] == "query":
                self.queries[metadata["name"]] = handler
            else:
                self.mutations[metadata["name"]] = handler

    async def execute(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        operation = "mutation" if document.lstrip().startswith("mutation") else "query"
        names = self._operation_names(document)
        handlers = self.mutations if operation == "mutation" else self.queries
        data: dict[str, Any] = {}
        for name in names:
            handler = handlers.get(name)
            if handler is None:
                return {"errors": [{"message": f"Unknown {operation} field: {name}"}]}
            result = handler(**variables)
            if inspect.isawaitable(result):
                result = await result
            data[name] = result
        return {"data": data}

    def _operation_names(self, document: str) -> list[str]:
        match = re.search(r"\{(?P<body>.*)\}", document, flags=re.DOTALL)
        if match is None:
            return []
        body = re.sub(r"\([^)]*\)", "", match.group("body"))
        return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)


class GraphQLModule:
    @staticmethod
    def for_root(*, resolvers: list[type], path: str = "graphql") -> type:
        controller_path = path.strip("/")

        @Controller(controller_path)
        class GraphQLController:
            def __init__(self, schema: GraphQLSchema):
                self.schema = schema

            @Post("/")
            async def execute(self, payload: dict[str, Any] = Body()):
                return await self.schema.execute(
                    payload.get("query", ""),
                    variables=payload.get("variables"),
                )

        @Module(controllers=[GraphQLController], providers=[GraphQLSchema, *resolvers], exports=[GraphQLSchema])
        class DynamicGraphQLModule:
            pass

        return DynamicGraphQLModule
