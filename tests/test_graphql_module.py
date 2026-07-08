from fastapi.testclient import TestClient

from fanest import FaNestFactory, Module
from fanest.graphql import GraphQLModule, Mutation, Query, Resolver


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

    assert query.json() == {"data": {"users_count": 1}}
    assert mutation.json() == {"data": {"create_user": {"name": "Grace"}}}
    assert second_query.json() == {"data": {"users_count": 2}}
