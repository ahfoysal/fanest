import argparse
import time
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module


def raw_fastapi_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


@Controller("health")
class HealthController:
    @Get("/")
    async def health(self):
        return {"ok": True}


@Module(controllers=[HealthController])
class HealthModule:
    pass


def fanest_app() -> FastAPI:
    return FaNestFactory.create(HealthModule)


def run_client_benchmark(name: str, factory: Callable[[], FastAPI], requests: int) -> float:
    client = TestClient(factory())
    start = time.perf_counter()
    for _ in range(requests):
        response = client.get("/health")
        response.raise_for_status()
    elapsed = time.perf_counter() - start
    rps = requests / elapsed
    print(f"{name}: {rps:,.0f} req/s ({elapsed:.3f}s for {requests:,} requests)")
    return rps


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare raw FastAPI and FaNest request overhead.")
    parser.add_argument("--requests", type=int, default=5000)
    args = parser.parse_args()

    raw = run_client_benchmark("raw-fastapi", raw_fastapi_app, args.requests)
    fanest = run_client_benchmark("fanest", fanest_app, args.requests)
    delta = ((raw - fanest) / raw) * 100 if raw else 0.0
    print(f"overhead: {delta:.2f}%")


if __name__ == "__main__":
    main()
