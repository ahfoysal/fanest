from dataclasses import dataclass, replace
from typing import Iterable

from fanest import Injectable


@dataclass(frozen=True)
class UserRecord:
    id: int
    email: str
    name: str
    password_hash: str
    roles: list[str]
    disabled: bool = False


@Injectable()
class InMemoryUserRepository:
    def __init__(self):
        self._next_id = 3
        self._users: dict[int, UserRecord] = {
            1: UserRecord(
                id=1,
                email="admin@fanest.dev",
                name="Admin Ada",
                password_hash=self.hash_password("admin-password"),
                roles=["admin", "user"],
            ),
            2: UserRecord(
                id=2,
                email="grace@fanest.dev",
                name="Grace Hopper",
                password_hash=self.hash_password("grace-password"),
                roles=["user"],
            ),
        }

    def all(self) -> list[UserRecord]:
        return sorted(self._users.values(), key=lambda user: user.id)

    def search(self, term: str | None = None, *, include_disabled: bool = False) -> list[UserRecord]:
        users: Iterable[UserRecord] = self.all()
        if not include_disabled:
            users = [user for user in users if not user.disabled]
        if term:
            lowered = term.lower()
            users = [
                user
                for user in users
                if lowered in user.email.lower() or lowered in user.name.lower()
            ]
        return list(users)

    def get(self, user_id: int) -> UserRecord | None:
        return self._users.get(user_id)

    def get_by_email(self, email: str) -> UserRecord | None:
        normalized = email.lower()
        for user in self._users.values():
            if user.email.lower() == normalized:
                return user
        return None

    def create(self, *, email: str, name: str, password: str, roles: list[str]) -> UserRecord:
        user = UserRecord(
            id=self._next_id,
            email=email,
            name=name,
            password_hash=self.hash_password(password),
            roles=list(dict.fromkeys(roles or ["user"])),
        )
        self._next_id += 1
        self._users[user.id] = user
        return user

    def update(
        self,
        user_id: int,
        *,
        name: str | None = None,
        roles: list[str] | None = None,
        disabled: bool | None = None,
    ) -> UserRecord | None:
        user = self.get(user_id)
        if user is None:
            return None
        updated = replace(
            user,
            name=user.name if name is None else name,
            roles=user.roles if roles is None else list(dict.fromkeys(roles)),
            disabled=user.disabled if disabled is None else disabled,
        )
        self._users[user_id] = updated
        return updated

    def verify_password(self, user: UserRecord, password: str) -> bool:
        return user.password_hash == self.hash_password(password)

    @staticmethod
    def hash_password(password: str) -> str:
        return f"fake-sha256:{password[::-1]}"
