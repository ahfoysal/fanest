import asyncio

from fastapi.testclient import TestClient
import pytest
from typing import Any, cast

from fanest import FaNestFactory, Injectable, Module
from fanest.graphql import (
    Args,
    Field,
    GraphQLDataLoader,
    GraphQLModule,
    GraphQLSchema,
    InputType,
    Key,
    Mutation,
    ObjectType,
    Query,
    ResolveField,
    ResolveReference,
    Resolver,
    Subscription,
    UseGuards,
    UseInterceptors,
    UsePipes,
)


@Resolver
class UserResolver:
    users: list[str] = ["Ada"]

    @Query()
    async def users_count(self):
        return len(self.users)

    @Query()
    async def viewer(self):
        return {"id": "1", "name": self.users[0]}

    @Query()
    async def user(self, name: str):
        return {"name": name}

    @Mutation("create_user")
    async def create(self, name: str):
        self.users.append(name)
        return {"name": name}

    @Subscription("user_created")
    async def user_created(self):
        return self.users[-1]


@Module(imports=[GraphQLModule.for_root(resolvers=[UserResolver])])
class GraphQLAppModule:
    pass


def test_graphql_module_executes_queries_and_mutations():
    UserResolver.users = ["Ada"]
    with TestClient(FaNestFactory.create(GraphQLAppModule)) as client:
        query = client.post("/graphql", json={"query": "{ users_count }"})
        mutation = client.post(
            "/graphql",
            json={"query": "mutation { create_user }", "variables": {"name": "Grace"}},
        )
        second_query = client.post("/graphql", json={"query": "{ users_count }"})
        nested_query = client.post("/graphql", json={"query": "{ viewer { id name } }"})
        alias_query = client.post("/graphql", json={"query": '{ first: user(name: "Ada") { name } }'})
        variable_arg_query = client.post(
            "/graphql",
            json={"query": "query ($name: String!) { user(name: $name) { name } users_count }", "variables": {"name": "Lin"}},
        )
        subscription = client.post("/graphql", json={"query": "subscription { user_created }"})

    assert query.json() == {"data": {"users_count": 1}}
    assert mutation.json() == {"data": {"create_user": {"name": "Grace"}}}
    assert second_query.json() == {"data": {"users_count": 2}}
    assert nested_query.json() == {"data": {"viewer": {"id": "1", "name": "Ada"}}}
    assert alias_query.json() == {"data": {"first": {"name": "Ada"}}}
    assert variable_arg_query.json() == {
        "data": {"user": {"name": "Lin"}, "users_count": 2}
    }
    assert subscription.json() == {"data": {"user_created": "Grace"}}


@Injectable(scope="request")
@Resolver
class RequestScopedResolver:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @Query()
    async def resolver_id(self):
        return self.instance_id


@Module(imports=[GraphQLModule.for_root(resolvers=[RequestScopedResolver])])
class RequestScopedGraphQLModule:
    pass


def test_graphql_resolvers_resolve_inside_request_scope():
    RequestScopedResolver.created = 0

    with TestClient(FaNestFactory.create(RequestScopedGraphQLModule)) as client:
        first = client.post("/graphql", json={"query": "{ resolver_id }"})
        second = client.post("/graphql", json={"query": "{ resolver_id }"})

    assert first.json() == {"data": {"resolver_id": 1}}
    assert second.json() == {"data": {"resolver_id": 2}}


@Injectable()
class TaskRepository:
    def count(self) -> int:
        return 3


@Module(providers=[TaskRepository], exports=[TaskRepository])
class TaskFeatureModule:
    pass


@Resolver
class AnalyticsResolver:
    def __init__(self, tasks: TaskRepository):
        self.tasks = tasks

    @Query()
    async def task_count(self):
        return self.tasks.count()


@Module(
    imports=[
        GraphQLModule.for_root(
            imports=[TaskFeatureModule],
            resolvers=[AnalyticsResolver],
            path="analytics/graphql",
        )
    ]
)
class ImportedDependencyGraphQLModule:
    pass


def test_graphql_dynamic_module_can_import_resolver_dependencies():
    with TestClient(FaNestFactory.create(ImportedDependencyGraphQLModule)) as client:
        response = client.post("/analytics/graphql", json={"query": "{ task_count }"})

    assert response.json() == {"data": {"task_count": 3}}


@Resolver()
class ComplexArgsResolver:
    @Query()
    async def search(self, filter: dict, tags: list[str], limit: int = 10, score: float = 1.0, active: bool = True):
        return {
            "filter": filter,
            "tags": tags,
            "limit": limit,
            "score": score,
            "active": active,
        }

    @Query()
    async def default_user(self, name: str):
        return {"name": name}

    @Query()
    async def unstable(self):
        raise RuntimeError("resolver exploded")


@Module(imports=[GraphQLModule.for_root(resolvers=[ComplexArgsResolver], path="complex/graphql")])
class ComplexGraphQLModule:
    pass


def test_graphql_parser_handles_complex_args_comments_directives_and_defaults():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        complex_query = client.post(
            "/complex/graphql",
            json={
                "query": """
                    query Search($tags: [String!]!) {
                        # nested selections should not become root fields
                        first: search(
                            filter: {owner: "Ada", meta: {archived: false}, rank: 2}
                            tags: $tags
                            limit: 5
                            score: 2.5
                            active: true
                        ) @include(if: true) {
                            filter { owner }
                            tags
                        }
                    }
                """,
                "variables": {"tags": ["python", "graphql"]},
            },
        )
        default_query = client.post(
            "/complex/graphql",
            json={"query": 'query WithDefault($name: String = "Grace") { default_user(name: $name) { name } }'},
        )

    assert complex_query.json() == {
        "data": {
            "first": {
                "filter": {"owner": "Ada"},
                "tags": ["python", "graphql"],
            }
        }
    }
    assert default_query.json() == {"data": {"default_user": {"name": "Grace"}}}


def test_graphql_execution_returns_structured_field_errors_without_aborting_siblings():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        response = client.post("/complex/graphql", json={"query": "{ unstable default_user(name: \"Ada\") { name } missing_field }"})

    assert response.json() == {
        "data": {
            "unstable": None,
            "default_user": {"name": "Ada"},
            "missing_field": None,
        },
        "errors": [
            {
                "message": "resolver exploded",
                "path": ["unstable"],
                "locations": [{"line": 1, "column": 3}],
            },
            {
                "message": "Unknown query field: missing_field",
                "path": ["missing_field"],
                "locations": [{"line": 1, "column": 47}],
            },
        ],
    }


def test_graphql_operation_name_selects_the_requested_operation():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        response = client.post(
            "/complex/graphql",
            json={
                "query": """
                    query First {
                        unstable
                    }

                    query Second($name: String!) {
                        selected: default_user(name: $name) { name }
                    }
                """,
                "variables": {"name": "Katherine"},
                "operationName": "Second",
            },
        )
        missing = client.post(
            "/complex/graphql",
            json={
                "query": "query First { default_user(name: \"Ada\") { name } }",
                "operationName": "Missing",
            },
        )

    assert response.json() == {
        "data": {"selected": {"name": "Katherine"}},
    }
    assert missing.json() == {"errors": [{"message": "Unknown operation: Missing"}]}


def test_graphql_expands_fragments_and_supports_root_meta_fields():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        response = client.post(
            "/complex/graphql",
            json={
                "query": """
                    query WithFragments($name: String!) {
                        __typename
                        meta: __schema { queryType { name } }
                        ...UserFields
                        ... on Query {
                            direct: default_user(name: "Direct") { name }
                        }
                    }

                    fragment UserFields on Query {
                        user: default_user(name: $name) { name }
                    }
                """,
                "variables": {"name": "Fragment"},
            },
        )

    assert response.json() == {
        "data": {
            "__typename": "Query",
            "meta": {
                "queryType": {"name": "Query"},
            },
            "user": {"name": "Fragment"},
            "direct": {"name": "Direct"},
        }
    }


def test_graphql_handles_fragments_before_operation_and_spread_directives():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        response = client.post(
            "/complex/graphql",
            json={
                "query": """
                    fragment UserFields on Query {
                        shown: default_user(name: "Shown") { name }
                    }

                    query WithLeadingFragment($show: Boolean!, $skip: Boolean!) {
                        ...UserFields @include(if: $show)
                        ...SkippedFields @skip(if: $skip)
                        ... on Query @include(if: true) {
                            inline: default_user(name: "Inline") { name }
                        }
                        ... on Query @include(if: false) {
                            hidden: default_user(name: "Hidden") { name }
                        }
                    }

                    fragment SkippedFields on Query {
                        skipped: default_user(name: "Skipped") { name }
                    }
                """,
                "variables": {"show": True, "skip": True},
            },
        )

    assert response.json() == {
        "data": {
            "shown": {"name": "Shown"},
            "inline": {"name": "Inline"},
        }
    }


def test_graphql_fragment_errors_are_structured_parse_errors():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        unknown = client.post(
            "/complex/graphql",
            json={"query": "query Broken { ...MissingFragment }"},
        )
        circular = client.post(
            "/complex/graphql",
            json={
                "query": """
                    query Broken {
                        ...LoopA
                    }
                    fragment LoopA on Query {
                        ...LoopB
                    }
                    fragment LoopB on Query {
                        ...LoopA
                    }
                """
            },
        )

    assert unknown.json() == {"errors": [{"message": "Unknown fragment: MissingFragment"}]}
    assert circular.json() == {"errors": [{"message": "Circular fragment reference: LoopA"}]}


@Resolver
class ProfileResolver:
    @Query()
    async def profile(self):
        return {"id": "p1"}


@Resolver
class SettingsResolver:
    @Query()
    async def settings(self):
        return {"theme": "dark"}


@Module(imports=[GraphQLModule.for_root(resolvers=[ProfileResolver], path="profile/graphql")])
class ProfileGraphQLModule:
    pass


@Module(imports=[GraphQLModule.for_root(resolvers=[SettingsResolver], path="settings/graphql")])
class SettingsGraphQLModule:
    pass


@Module(imports=[ProfileGraphQLModule, SettingsGraphQLModule])
class MultipleGraphQLModulesApp:
    pass


def test_multiple_graphql_modules_keep_separate_schemas_and_routes():
    with TestClient(FaNestFactory.create(MultipleGraphQLModulesApp)) as client:
        profile = client.post("/profile/graphql", json={"query": "{ profile { id } }"})
        settings = client.post("/settings/graphql", json={"query": "{ settings { theme } }"})
        wrong_schema = client.post("/profile/graphql", json={"query": "{ settings { theme } }"})

    assert profile.json() == {"data": {"profile": {"id": "p1"}}}
    assert settings.json() == {"data": {"settings": {"theme": "dark"}}}
    assert wrong_schema.json() == {
        "data": {"settings": None},
        "errors": [
            {
                "message": "Unknown query field: settings",
                "path": ["settings"],
                "locations": [{"line": 1, "column": 3}],
            }
        ],
    }


@Resolver
class DuplicateAResolver:
    @Query("duplicate")
    async def first_duplicate(self):
        return "first"


@Resolver
class DuplicateBResolver:
    @Query("duplicate")
    async def second_duplicate(self):
        return "second"


@Module(imports=[GraphQLModule.for_root(resolvers=[DuplicateAResolver, DuplicateBResolver], path="duplicate/graphql")])
class DuplicateGraphQLModule:
    pass


def test_duplicate_graphql_fields_fail_during_startup():
    with pytest.raises(ValueError, match="Duplicate GraphQL query field registered: duplicate"):
        with TestClient(FaNestFactory.create(DuplicateGraphQLModule)):
            pass


def test_graphql_parse_errors_are_returned_as_errors():
    with TestClient(FaNestFactory.create(ComplexGraphQLModule)) as client:
        missing_variable = client.post("/complex/graphql", json={"query": "{ default_user(name: $name) { name } }"})
        invalid_document = client.post("/complex/graphql", json={"query": "{ default_user(name: \"Ada\" { name }"})

    assert missing_variable.json() == {"errors": [{"message": "Variable $name was not provided"}]}
    assert invalid_document.json() == {"errors": [{"message": "Unclosed {"}]}


@Resolver
class DeepShapeResolver:
    @Query()
    async def people(self):
        return [
            {
                "__typename": "Person",
                "id": "1",
                "name": "Ada",
                "password": "hidden",
                "profile": {"city": "London", "rank": 1, "secret": "x"},
            },
            {
                "__typename": "Person",
                "id": "2",
                "name": "Grace",
                "password": "hidden",
                "profile": {"city": "Arlington", "rank": 2, "secret": "y"},
            },
        ]

    @Mutation()
    async def rename(self, name: str = "Default"):
        return {"id": "1", "name": name, "ignored": True}


@Module(imports=[GraphQLModule.for_root(resolvers=[DeepShapeResolver], path="deep/graphql")])
class DeepGraphQLModule:
    pass


def test_graphql_shapes_nested_lists_aliases_directives_and_fragments():
    with TestClient(FaNestFactory.create(DeepGraphQLModule)) as client:
        response = client.post(
            "/deep/graphql",
            json={
                "query": """
                    query Deep($includeRank: Boolean! = true) {
                        people {
                            __typename
                            userId: id
                            profile {
                                city
                                rank @include(if: $includeRank)
                                secret @skip(if: true)
                            }
                            ...NameFields
                        }
                    }

                    fragment NameFields on Person {
                        displayName: name
                    }
                """
            },
        )

    assert response.json() == {
        "data": {
            "people": [
                {
                    "__typename": "Person",
                    "userId": "1",
                    "profile": {"city": "London", "rank": 1},
                    "displayName": "Ada",
                },
                {
                    "__typename": "Person",
                    "userId": "2",
                    "profile": {"city": "Arlington", "rank": 2},
                    "displayName": "Grace",
                },
            ]
        }
    }


def test_graphql_mutation_uses_variable_defaults_and_shapes_result():
    with TestClient(FaNestFactory.create(DeepGraphQLModule)) as client:
        response = client.post(
            "/deep/graphql",
            json={
                "query": 'mutation Rename($name: String = "Katherine") { rename(name: $name) { name } }',
            },
        )

    assert response.json() == {"data": {"rename": {"name": "Katherine"}}}


def test_graphql_requires_operation_name_for_multi_operation_documents():
    with TestClient(FaNestFactory.create(DeepGraphQLModule)) as client:
        response = client.post(
            "/deep/graphql",
            json={"query": "query One { people { id } } query Two { people { name } }"},
        )

    assert response.json() == {
        "errors": [{"message": "Operation name is required when a document contains multiple operations"}]
    }


def test_graphql_supports_deeper_introspection_selection():
    with TestClient(FaNestFactory.create(DeepGraphQLModule)) as client:
        response = client.post(
            "/deep/graphql",
            json={
                "query": """
                    {
                        schema: __schema {
                            types { name kind }
                            directives { name }
                        }
                        stringType: __type(name: "String") { name kind fields }
                    }
                """
            },
        )

    body = response.json()
    assert {"name": "Query", "kind": "OBJECT"} in body["data"]["schema"]["types"]
    assert {"name": "include"} in body["data"]["schema"]["directives"]
    assert body["data"]["stringType"] == {"name": "String", "kind": "SCALAR", "fields": None}


class GraphQLUpperPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name" and isinstance(value, str):
            return value.upper()
        return value


class GraphQLAllowGuard:
    def can_activate(self, context):
        return context.kwargs.get("allowed", True)


class GraphQLEnvelopeInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        if isinstance(result, dict):
            return {**result, "intercepted": True}
        return result


@ObjectType("CodeFirstUser")
@Resolver
class CodeFirstUserResolver:
    @Field(str)
    def name(self):
        return ""


@InputType("RenameUserInput")
class RenameUserInput:
    @Field(str)
    def name(self):
        return ""


@Resolver
class GraphQLParityResolver:
    @Query()
    async def account(self):
        return {"id": "a1", "name": "Ada"}

    @ResolveField("display")
    async def display_name(self, parent):
        return f"{parent['name']} Lovelace"

    @Query()
    @UseGuards(GraphQLAllowGuard)
    async def guarded(self, allowed: bool):
        return {"allowed": allowed}

    @Query()
    @UsePipes(GraphQLUpperPipe)
    @UseInterceptors(GraphQLEnvelopeInterceptor)
    async def piped(self, name: str = cast(Any, Args("value"))):
        return {"name": name}

    @Subscription(
        "updates",
        filter=lambda payload, variables, args: payload["kind"] == args["kind"],
        resolve=lambda payload, variables, args: payload["message"],
    )
    async def updates(self, kind: str):
        return {"kind": "system", "message": f"{kind}:ready"}


@Module(
    imports=[
        GraphQLModule.for_root(
            resolvers=[GraphQLParityResolver, CodeFirstUserResolver],
            path="parity/graphql",
        )
    ]
)
class GraphQLParityModule:
    pass


def test_graphql_field_resolvers_args_pipes_guards_and_interceptors():
    with TestClient(FaNestFactory.create(GraphQLParityModule)) as client:
        field_resolver = client.post(
            "/parity/graphql",
            json={"query": "{ account { id display } }"},
        )
        guarded_ok = client.post(
            "/parity/graphql",
            json={"query": "{ guarded(allowed: true) { allowed } }"},
        )
        guarded_denied = client.post(
            "/parity/graphql",
            json={"query": "{ guarded(allowed: false) { allowed } }"},
        )
        piped = client.post(
            "/parity/graphql",
            json={"query": '{ piped(value: "ada") { name intercepted } }'},
        )

    assert field_resolver.json() == {
        "data": {"account": {"id": "a1", "display": "Ada Lovelace"}}
    }
    assert guarded_ok.json() == {"data": {"guarded": {"allowed": True}}}
    assert guarded_denied.json() == {
        "data": {"guarded": None},
        "errors": [
            {
                "message": "Forbidden",
                "path": ["guarded"],
                "locations": [{"line": 1, "column": 3}],
            }
        ],
    }
    assert piped.json() == {"data": {"piped": {"name": "ADA", "intercepted": True}}}


def test_graphql_subscription_filter_and_resolve_hooks():
    with TestClient(FaNestFactory.create(GraphQLParityModule)) as client:
        included = client.post(
            "/parity/graphql",
            json={"query": 'subscription { updates(kind: "system") }'},
        )
        filtered = client.post(
            "/parity/graphql",
            json={"query": 'subscription { updates(kind: "other") }'},
        )

    assert included.json() == {"data": {"updates": "system:ready"}}
    assert filtered.json() == {"data": {"updates": None}}


def test_graphql_code_first_metadata_helpers_are_introspectable():
    schema = GraphQLModule.for_root(
        resolvers=[GraphQLParityResolver, CodeFirstUserResolver],
        path="metadata/graphql",
    )

    @Module(imports=[schema], providers=[RenameUserInput])
    class MetadataModule:
        pass

    with TestClient(FaNestFactory.create(MetadataModule)) as client:
        response = client.post(
            "/metadata/graphql",
            json={"query": '{ user: __type(name: "CodeFirstUser") { name kind fields { name } } }'},
        )

    assert response.json() == {
        "data": {
            "user": {
                "name": "CodeFirstUser",
                "kind": "OBJECT",
                "fields": [{"name": "name"}],
            }
        }
    }


@Key("id")
@ObjectType("FederatedProduct")
@Resolver
class FederatedProductResolver:
    @Field(str)
    def name(self):
        return "unused"

    @ResolveReference
    async def resolve_reference(self, reference: dict[str, Any]):
        return {
            "__typename": "FederatedProduct",
            "id": reference["id"],
            "name": f"Product {reference['id']}",
        }


def test_graphql_federation_service_and_entities_are_executable():
    schema = GraphQLModule.for_federation(
        resolvers=[FederatedProductResolver],
        path="federation/graphql",
        schema='type FederatedProduct @key(fields: "id") { id: ID! name: String }',
        websocket=False,
    )

    @Module(imports=[schema])
    class FederationModule:
        pass

    with TestClient(FaNestFactory.create(FederationModule)) as client:
        service = client.post("/federation/graphql", json={"query": "{ _service { sdl } }"})
        entities = client.post(
            "/federation/graphql",
            json={
                "query": """
                    query($representations: [_Any!]!) {
                      _entities(representations: $representations) {
                        ... on FederatedProduct { __typename id name }
                      }
                    }
                """,
                "variables": {
                    "representations": [{"__typename": "FederatedProduct", "id": "42"}],
                },
            },
        )

    assert '@key(fields: "id")' in service.json()["data"]["_service"]["sdl"]
    assert entities.json() == {
        "data": {
            "_entities": [
                {
                    "__typename": "FederatedProduct",
                    "id": "42",
                    "name": "Product 42",
                }
            ]
        }
    }


class AsyncProfile:
    async def city(self):
        return "London"


class AsyncPerson:
    name = "Ada"

    async def profile(self):
        return AsyncProfile()


@Resolver
class AsyncNestedFieldResolver:
    @Query()
    async def async_person(self):
        return AsyncPerson()

    @Query()
    async def async_people(self):
        return [AsyncPerson()]


@Module(imports=[GraphQLModule.for_root(resolvers=[AsyncNestedFieldResolver], path="async-fields/graphql")])
class AsyncNestedGraphQLModule:
    pass


def test_graphql_awaits_async_nested_object_fields_and_list_items():
    with TestClient(FaNestFactory.create(AsyncNestedGraphQLModule)) as client:
        response = client.post(
            "/async-fields/graphql",
            json={"query": "{ async_person { name profile { city } } async_people { profile { city } } }"},
        )

    assert response.json() == {
        "data": {
            "async_person": {"name": "Ada", "profile": {"city": "London"}},
            "async_people": [{"profile": {"city": "London"}}],
        }
    }


def test_graphql_rejects_unknown_or_incomplete_directives():
    with TestClient(FaNestFactory.create(DeepGraphQLModule)) as client:
        unknown = client.post(
            "/deep/graphql",
            json={"query": "{ people @defer { id } }"},
        )
        missing_if = client.post(
            "/deep/graphql",
            json={"query": "{ people @include { id } }"},
        )

    assert unknown.json() == {"errors": [{"message": "Unknown directive: defer"}]}
    assert missing_if.json() == {
        "errors": [{"message": "Directive @include requires an if argument"}]
    }


@Resolver
class SdlBookResolver:
    @Query()
    async def book(self):
        return {"id": "b1", "title": "FaNest Deep Work"}


SDL_SCHEMA = """
type Book {
  id: ID!
  title: String
}

input BookInput {
  title: String!
}
"""


@Module(
    imports=[
        GraphQLModule.for_schema(
            SDL_SCHEMA,
            resolvers=[SdlBookResolver],
            path="sdl/graphql",
            websocket=False,
        )
    ]
)
class SdlGraphQLModule:
    pass


def test_graphql_sdl_first_schema_compiler_feeds_introspection_and_execution():
    with TestClient(FaNestFactory.create(SdlGraphQLModule)) as client:
        query = client.post("/sdl/graphql", json={"query": "{ book { id title } }"})
        introspection = client.post(
            "/sdl/graphql",
            json={"query": '{ bookType: __type(name: "Book") { name kind fields { name type { name kind } } } }'},
        )

    assert query.json() == {"data": {"book": {"id": "b1", "title": "FaNest Deep Work"}}}
    assert introspection.json() == {
        "data": {
            "bookType": {
                "name": "Book",
                "kind": "OBJECT",
                "fields": [
                    {"name": "id", "type": {"name": "ID", "kind": "TYPE"}},
                    {"name": "title", "type": {"name": "String", "kind": "TYPE"}},
                ],
            }
        }
    }


@Resolver
class LoaderResolver:
    batch_calls: list[list[str]] = []

    def __init__(self):
        async def batch(keys: list[str]):
            type(self).batch_calls.append(keys)
            await asyncio.sleep(0)
            return [{"id": key, "name": key.upper()} for key in keys]

        self.loader = GraphQLDataLoader(batch)

    @Query()
    async def loaded_users(self):
        return await asyncio.gather(
            self.loader.load("ada"),
            self.loader.load("grace"),
            self.loader.load("ada"),
        )


@Module(imports=[GraphQLModule.for_root(resolvers=[LoaderResolver], path="loader/graphql", websocket=False)])
class LoaderGraphQLModule:
    pass


def test_graphql_dataloader_batches_and_caches_keys_inside_resolver():
    LoaderResolver.batch_calls = []

    with TestClient(FaNestFactory.create(LoaderGraphQLModule)) as client:
        response = client.post("/loader/graphql", json={"query": "{ loaded_users { id name } }"})

    assert response.json() == {
        "data": {
            "loaded_users": [
                {"id": "ada", "name": "ADA"},
                {"id": "grace", "name": "GRACE"},
                {"id": "ada", "name": "ADA"},
            ]
        }
    }
    assert LoaderResolver.batch_calls == [["ada", "grace"]]


@Resolver
class StreamingSubscriptionResolver:
    @Subscription("ticks", resolve=lambda payload, variables, args: {"value": payload["value"]})
    async def ticks(self):
        for value in [1, 2]:
            yield {"value": value}


@Module(imports=[GraphQLModule.for_root(resolvers=[StreamingSubscriptionResolver], path="ws-graphql")])
class WebSocketGraphQLModule:
    pass


def test_graphql_websocket_subscription_protocol_streams_next_and_complete_frames():
    with TestClient(FaNestFactory.create(WebSocketGraphQLModule)) as client:
        with client.websocket_connect("/ws-graphql/ws") as websocket:
            websocket.send_json({"event": "connection_init", "data": {}})
            assert websocket.receive_json() == {"type": "connection_ack"}
            websocket.send_json(
                {
                    "event": "subscribe",
                    "data": {
                        "id": "sub-1",
                        "payload": {"query": "subscription { ticks { value } }"},
                    },
                }
            )
            assert websocket.receive_json() == {
                "type": "next",
                "id": "sub-1",
                "payload": {"data": {"ticks": {"value": 1}}},
            }
            assert websocket.receive_json() == {
                "type": "next",
                "id": "sub-1",
                "payload": {"data": {"ticks": {"value": 2}}},
            }
            assert websocket.receive_json() == {"type": "complete", "id": "sub-1"}


def test_graphql_validator_reports_response_key_conflicts():
    schema = GraphQLSchema()
    schema.register_resolver(ComplexArgsResolver())

    assert schema.validate('{ same: default_user(name: "Ada") { name } same: unstable }') == [
        {
            "message": "Field conflict for response key 'same'",
            "path": ["same"],
            "locations": [{"line": 1, "column": 44}],
        }
    ]


def test_graphql_validator_reports_unknown_fields_without_running_resolvers():
    schema = GraphQLSchema()
    schema.register_resolver(ComplexArgsResolver())

    assert schema.validate("{ missing_field }") == [
        {
            "message": "Unknown query field: missing_field",
            "path": ["missing_field"],
            "locations": [{"line": 1, "column": 3}],
        }
    ]
