from pydantic import BaseModel


class ExampleSettings(BaseModel):
    app_name: str = "FaNest Production Example"
    app_env: str = "local"
    jwt_secret: str = "local-development-secret-change-me"
    jwt_issuer: str = "fanest-production-example"
    jwt_audience: str = "fanest-users"
    graphql_admin_key: str = "local-dev-key"
    mail_from: str = "noreply@fanest.dev"


DEFAULT_CONFIG = ExampleSettings().model_dump()
