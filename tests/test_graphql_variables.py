import asyncio
from fastapi.testclient import TestClient
from fanest import FaNestFactory, Module
from fanest.graphql import GraphQLModule, Resolver, Query, ObjectType, Args

@Resolver
class UserResolver:
    @Query()
    async def user(self, name: str = Args(default="Guest")) -> str:
        return name

@Module(imports=[GraphQLModule.for_root(resolvers=[UserResolver])])
class GraphQLAppModule:
    pass

def test_graphql_variables_defaults():
    with TestClient(FaNestFactory.create(GraphQLAppModule)) as client:
        query = """
        query ($name: String = "Admin") {
            user(name: $name)
        }
        """
        response = client.post("/graphql", json={"query": query})
        assert response.json() == {"data": {"user": "Admin"}}

        # Don't pass the variable, it should use the query-defined default "Admin", 
        # or if that is missing, the resolver's default "Guest"
        query2 = """
        query {
            user
        }
        """
        response2 = client.post("/graphql", json={"query": query2})
        assert response2.json() == {"data": {"user": "Guest"}}

