from fastapi.testclient import TestClient

from fanest import FaNestFactory, Injectable, Module
from fanest.graphql import GraphQLModule, Mutation, Query, Resolver, Subscription


@Resolver
class UserResolver:
    users: list[str] = ["Ada"]

    @Query()
    async def users_count(self):
        return len(self.users)

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
        subscription = client.post("/graphql", json={"query": "subscription { user_created }"})

    assert query.json() == {"data": {"users_count": 1}}
    assert mutation.json() == {"data": {"create_user": {"name": "Grace"}}}
    assert second_query.json() == {"data": {"users_count": 2}}
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
