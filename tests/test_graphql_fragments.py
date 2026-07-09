from dataclasses import dataclass
from fastapi.testclient import TestClient
from fanest import FaNestFactory, Module
from fanest.graphql import GraphQLModule, Resolver, Query, ObjectType


@ObjectType("User")
@dataclass
class User:
    id: str
    name: str

@Resolver
class UserResolver:
    @Query()
    async def user(self) -> User:
        return User(id="1", name="Ada")


@Module(imports=[GraphQLModule.for_root(resolvers=[UserResolver])])
class GraphQLAppModule:
    pass


def test_graphql_fragments():
    with TestClient(FaNestFactory.create(GraphQLAppModule)) as client:
        query = """
        query {
            user {
                ...UserFragment
            }
        }
        fragment UserFragment on User {
            id
            name
        }
        """
        response = client.post("/graphql", json={"query": query})
        assert response.json() == {"data": {"user": {"id": "1", "name": "Ada"}}}
