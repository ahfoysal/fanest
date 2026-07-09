from pydantic import BaseModel, EmailStr, Field


class CreateUserDto(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    roles: list[str] = Field(default_factory=lambda: ["user"])


class UpdateUserDto(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    roles: list[str] | None = None


class LoginDto(BaseModel):
    email: EmailStr
    password: str


class UserView(BaseModel):
    id: int
    email: EmailStr
    name: str
    roles: list[str]
    disabled: bool = False
