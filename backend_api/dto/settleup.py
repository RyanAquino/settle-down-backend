from pydantic import BaseModel


class SettleUpGroup(BaseModel):
    name: str
    id: str
    currency: str | None = None
