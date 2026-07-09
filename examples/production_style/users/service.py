from typing import Any

from fanest import ConflictException, Injectable, NotFoundException, UnauthorizedException
from fanest.auth import JwtService
from fanest.config import ConfigService
from fanest.events import EventEmitter
from fanest.queues import InjectQueue, QueueRef

from examples.production_style.users.dto import CreateUserDto, LoginDto, UpdateUserDto, UserView
from examples.production_style.users.repository import InMemoryUserRepository, UserRecord


@Injectable()
class UsersService:
    def __init__(
        self,
        repository: InMemoryUserRepository,
        jwt_service: JwtService,
        config: ConfigService,
        events: EventEmitter,
        notification_queue: QueueRef = InjectQueue("notifications"),
    ):
        self.repository = repository
        self.jwt_service = jwt_service
        self.config = config
        self.events = events
        self.notification_queue = notification_queue

    def list_users(self, search: str | None = None, include_disabled: bool = False) -> list[UserView]:
        return [self.to_view(user) for user in self.repository.search(search, include_disabled=include_disabled)]

    def get_user(self, user_id: int) -> UserView:
        user = self.repository.get(user_id)
        if user is None:
            raise NotFoundException(f"User {user_id} was not found")
        return self.to_view(user)

    async def create_user(self, dto: CreateUserDto) -> UserView:
        if self.repository.get_by_email(str(dto.email)) is not None:
            raise ConflictException("A user with that email already exists")
        user = self.repository.create(
            email=str(dto.email),
            name=dto.name.strip(),
            password=dto.password,
            roles=dto.roles,
        )
        await self.events.emit("user.created", self.event_payload(user))
        await self.notification_queue.add(
            "send_email",
            {"template": "welcome", "user": self.event_payload(user)},
            attempts=2,
            backoff={"type": "fixed", "delay": 0.01},
        )
        return self.to_view(user)

    async def update_user(self, user_id: int, dto: UpdateUserDto) -> UserView:
        updated = self.repository.update(user_id, name=dto.name, roles=dto.roles)
        if updated is None:
            raise NotFoundException(f"User {user_id} was not found")
        await self.events.emit("user.updated", self.event_payload(updated))
        return self.to_view(updated)

    def login(self, dto: LoginDto) -> dict[str, Any]:
        user = self.repository.get_by_email(str(dto.email))
        if user is None or user.disabled or not self.repository.verify_password(user, dto.password):
            raise UnauthorizedException("Invalid email or password")
        access_token = self.jwt_service.sign(
            {
                "sub": user.id,
                "email": user.email,
                "roles": user.roles,
            }
        )
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in_seconds": 3600,
        }

    def stats(self) -> dict[str, Any]:
        users = self.repository.all()
        return {
            "app": self.config.get("app_name"),
            "environment": self.config.get("app_env"),
            "users": len(users),
            "admins": len([user for user in users if "admin" in user.roles]),
        }

    def event_payload(self, user: UserRecord) -> dict[str, Any]:
        return self.to_view(user).model_dump()

    def to_view(self, user: UserRecord) -> UserView:
        return UserView(
            id=user.id,
            email=user.email,
            name=user.name,
            roles=user.roles,
            disabled=user.disabled,
        )
