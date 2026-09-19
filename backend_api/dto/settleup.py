from pydantic import BaseModel


class SettleUpGroup(BaseModel):
    name: str
    id: str
