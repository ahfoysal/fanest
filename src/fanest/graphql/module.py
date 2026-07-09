import inspect
from typing import Any

from fanest import Body, Controller, Injectable, Module, Post


def Resolver(cls=None):
    # Accept both `@Resolver` and `@Resolver()` so the wrong form does not raise.
    if cls is None:
        return Resolver
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


def Subscription(name: str | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_graphql__", {"kind": "subscription", "name": name or handler.__name__})
        return handler

    return decorator


@Injectable()
class GraphQLSchema:
    def __init__(self):
        self.queries: dict[str, Any] = {}
        self.mutations: dict[str, Any] = {}
        self.subscriptions: dict[str, Any] = {}

    def register_resolver(self, resolver: Any) -> None:
        for _, handler in inspect.getmembers(resolver, predicate=callable):
            metadata = getattr(handler, "__fanest_graphql__", None)
            if metadata is None:
                continue
            if metadata["kind"] == "query":
                self.queries[metadata["name"]] = handler
            elif metadata["kind"] == "mutation":
                self.mutations[metadata["name"]] = handler
            else:
                self.subscriptions[metadata["name"]] = handler

    async def execute(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        stripped = document.lstrip()
        if stripped.startswith("subscription"):
            operation = "subscription"
        elif stripped.startswith("mutation"):
            operation = "mutation"
        else:
            operation = "query"
        names = self._operation_names(document)
        handlers = {
            "query": self.queries,
            "mutation": self.mutations,
            "subscription": self.subscriptions,
        }[operation]
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
        start = document.find("{")
        if start < 0:
            return []
        names: list[str] = []
        index = start + 1
        depth = 1
        while index < len(document) and depth > 0:
            char = document[index]
            if char == "{":
                depth += 1
                index += 1
                continue
            if char == "}":
                depth -= 1
                index += 1
                continue
            if depth != 1:
                index += 1
                continue
            if char.isspace() or char == ",":
                index += 1
                continue
            if not (char.isalpha() or char == "_"):
                index += 1
                continue
            first_name, index = self._read_name(document, index)
            cursor = self._skip_ws(document, index)
            name = first_name
            if cursor < len(document) and document[cursor] == ":":
                cursor = self._skip_ws(document, cursor + 1)
                if cursor < len(document) and (document[cursor].isalpha() or document[cursor] == "_"):
                    name, cursor = self._read_name(document, cursor)
            names.append(name)
            index = self._skip_selection(document, cursor)
        return names

    def _read_name(self, document: str, index: int) -> tuple[str, int]:
        start = index
        while index < len(document) and (document[index].isalnum() or document[index] == "_"):
            index += 1
        return document[start:index], index

    def _skip_ws(self, document: str, index: int) -> int:
        while index < len(document) and document[index].isspace():
            index += 1
        return index

    def _skip_selection(self, document: str, index: int) -> int:
        index = self._skip_ws(document, index)
        if index < len(document) and document[index] == "(":
            paren_depth = 1
            index += 1
            while index < len(document) and paren_depth > 0:
                if document[index] == "(":
                    paren_depth += 1
                elif document[index] == ")":
                    paren_depth -= 1
                index += 1
        index = self._skip_ws(document, index)
        if index < len(document) and document[index] == "{":
            brace_depth = 1
            index += 1
            while index < len(document) and brace_depth > 0:
                if document[index] == "{":
                    brace_depth += 1
                elif document[index] == "}":
                    brace_depth -= 1
                index += 1
        return index


class GraphQLModule:
    @staticmethod
    def for_root(
        *,
        resolvers: list[type],
        imports: list[Any] | None = None,
        path: str = "graphql",
    ) -> type:
        controller_path = path.strip("/")
        module_imports = imports or []

        @Controller(controller_path)
        class GraphQLController:
            def __init__(self, schema: GraphQLSchema):
                self.schema = schema

            @Post("/")
            async def execute(self, payload: dict[str, Any] = Body()):  # type: ignore[assignment]
                return await self.schema.execute(
                    payload.get("query", ""),
                    variables=payload.get("variables"),
                )

        @Module(
            imports=module_imports,
            controllers=[GraphQLController],
            providers=[GraphQLSchema, *resolvers],
            exports=[GraphQLSchema],
        )
        class DynamicGraphQLModule:
            pass

        return DynamicGraphQLModule
