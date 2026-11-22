from typing import TypedDict

from pydantic import BaseModel


class SettleUpGroup(BaseModel):
    name: str
    id: str
    currency: str | None = None


class SettleUpMember(TypedDict):
    """A group member as cached and served by ``SettleUpClient``."""

    id: str
    name: str | None
