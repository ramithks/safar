from pydantic import BaseModel
from typing import Optional


class CreateGroupRequest(BaseModel):
    name: str
    destination: Optional[str] = None
    trip_id: Optional[str] = None
    display_name: str                     # how the creator appears in chat


class JoinGroupRequest(BaseModel):
    display_name: str


class SendMessageRequest(BaseModel):
    sender_name: str
    text: str
    type: str = "text"                    # "text" | "poll" | "ai"
    poll_data: Optional[dict] = None      # { question, options: [{id, label, votes}] }


class VotePollRequest(BaseModel):
    option_id: str
    voter_name: str


class AddExpenseRequest(BaseModel):
    title: str
    amount: float
    paid_by: str
    split_between: list[str] = []
    category: str = "Other"
    date: str = ""


class AiAssistRequest(BaseModel):
    query: str
