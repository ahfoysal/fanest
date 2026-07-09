import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.production_style.main import app


def test_production_style_example_imports_and_exercises_core_flows():
    with TestClient(app) as client:
        users_response = client.get("/api/users")
        login_response = client.post(
            "/api/auth/login",
            json={"email": "admin@fanest.dev", "password": "admin-password"},
        )
        token = login_response.json()["access_token"]
        admin_response = client.get(
            "/api/admin/users/stats",
            headers={"authorization": f"Bearer {token}"},
        )
        create_response = client.post(
            "/api/users",
            json={
                "email": "katherine@fanest.dev",
                "name": "Katherine Johnson",
                "password": "strong-password",
            },
        )
        graphql_response = client.post(
            "/api/graphql",
            json={"query": "{ users { id email name roles } }"},
        )
        ops_response = client.get("/api/ops/notifications")

    assert users_response.status_code == 200
    assert users_response.json()["data"][0]["email"] == "admin@fanest.dev"
    assert login_response.status_code == 200
    assert admin_response.json()["users"] == 2
    assert create_response.status_code == 200
    assert create_response.json()["email"] == "katherine@fanest.dev"
    assert graphql_response.status_code == 200
    assert graphql_response.json()["data"]["users"]
    assert ops_response.status_code == 200
    assert ops_response.json()["mail_outbox"] >= 1
