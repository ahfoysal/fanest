#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION_TEST = "tests/test_live_external_services_certification.py"
SERVICE_GATES = {
    "Redis": "FANEST_LIVE_REDIS_URL",
    "Mongo": "FANEST_LIVE_MONGO_URL",
    "Postgres/SQLAlchemy": "FANEST_LIVE_POSTGRES_URL",
    "SMTP": "FANEST_LIVE_SMTP_HOST",
    "NATS": "FANEST_LIVE_NATS_URL",
    "RabbitMQ": "FANEST_LIVE_RABBITMQ_URL",
    "Kafka": "FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS",
    "gRPC": "FANEST_LIVE_GRPC_TARGET",
}


def main() -> int:
    print("FaNest live external services certification")
    print("Unset service env vars are expected to produce pytest skips.")
    for service, env_var in SERVICE_GATES.items():
        status = "enabled" if os.getenv(env_var) else "skipped"
        print(f"- {service}: {status} ({env_var})")

    command = [
        sys.executable,
        "-m",
        "pytest",
        CERTIFICATION_TEST,
        "-ra",
        "-q",
    ]
    print("\n$", " ".join(command))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
