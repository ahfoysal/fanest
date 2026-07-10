"""Round-2 regressions for pipes, events, CQRS, serialization and mapped types."""

import asyncio

import pytest
from pydantic import BaseModel, model_validator

from fanest.common.exceptions import BadRequestException
from fanest.common.pipes import ParseArrayPipe, ParseIntPipe
from fanest.common.serialization import Exclude, Expose, SerializeOptions, serialize_value
from fanest.events import EventEmitter
from fanest.mapped_types import IntersectionType, OmitType, PartialType, PickType


# --------------------------------------------------------------------------- #
# Pipes
# --------------------------------------------------------------------------- #
def test_parse_int_pipe_matches_nestjs_numeric_string_rules():
    pipe = ParseIntPipe()
    assert pipe.transform("42", {}) == 42
    assert pipe.transform("-7", {}) == -7
    for invalid in ("1_000", " 42 ", "٤٢", "abc", "3.5", ""):
        with pytest.raises(BadRequestException):
            pipe.transform(invalid, {})


def test_parse_array_pipe_splits_single_element_list():
    pipe = ParseArrayPipe()
    # FastAPI resolves ?ids=1,2,3 on a list param to ['1,2,3'].
    assert pipe.transform(["1,2,3"], {}) == ["1", "2", "3"]
    assert pipe.transform(["1", "2"], {}) == ["1", "2"]
    assert pipe.transform("a,b", {}) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_once_and_on_fire_in_registration_order():
    emitter = EventEmitter()
    order: list[str] = []
    emitter.once("x", lambda p: order.append("A"))
    emitter.on("x", lambda p: order.append("B"))
    emitter.on("x", lambda p: order.append("C"))
    asyncio.run(emitter.emit("x"))
    assert order == ["A", "B", "C"]


def test_multi_level_wildcard_semantics():
    emitter = EventEmitter()
    hits: list[str] = []
    emitter.on("**", lambda p: hits.append("all"))
    emitter.on("order.**", lambda p: hits.append("order"))
    emitter.on("foo.*", lambda p: hits.append("single"))
    for event in ("boot", "order.created", "order.created.v1", "foo.bar", "foo.bar.baz"):
        asyncio.run(emitter.emit(event))
    assert hits.count("all") == 5  # ** matches every event
    assert hits.count("order") == 2  # order.** matches created and created.v1
    assert hits.count("single") == 1  # foo.* matches only foo.bar


def test_single_star_matches_only_one_segment():
    emitter = EventEmitter()
    hits: list[int] = []
    emitter.on("*", lambda p: hits.append(1))
    asyncio.run(emitter.emit("boot"))
    asyncio.run(emitter.emit("a.b"))
    assert hits == [1]


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def test_class_level_exclude_uses_exclude_by_default_strategy():
    @Exclude()
    @Expose("email")
    class Account(BaseModel):
        email: str
        password: str
        api_key: str

    account = Account(email="a@b.com", password="secret", api_key="sk-live")
    assert serialize_value(account, SerializeOptions()) == {"email": "a@b.com"}


def test_nested_exclude_is_applied_inside_envelopes():
    @Exclude("password")
    class User(BaseModel):
        email: str
        password: str

    class Wrapper(BaseModel):
        user: User
        ok: bool

    user = User(email="x@y.com", password="pw")
    assert serialize_value({"data": user}, SerializeOptions()) == {"data": {"email": "x@y.com"}}
    assert serialize_value(Wrapper(user=user, ok=True), SerializeOptions()) == {
        "user": {"email": "x@y.com"},
        "ok": True,
    }


# --------------------------------------------------------------------------- #
# Mapped types
# --------------------------------------------------------------------------- #
class _Span(BaseModel):
    start: int
    end: int

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("end must be > start")
        return self


@pytest.mark.parametrize(
    "mapped",
    [
        PartialType(_Span),
        PickType(_Span, ["start", "end"]),
        OmitType(_Span, []),
        IntersectionType(_Span, _Span),
    ],
)
def test_mapped_types_preserve_model_validators(mapped):
    with pytest.raises(Exception):
        mapped(start=5, end=1)
    assert mapped(start=1, end=5).end == 5


def test_partial_type_skips_cross_field_validator_when_field_missing():
    partial = PartialType(_Span)(start=5)
    assert partial.start == 5
    assert partial.end is None
