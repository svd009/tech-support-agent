"""Contracts for API requests, tool arguments and model messages."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ServiceStatusInput(StrictInput):
    service_name: str = Field(min_length=1, max_length=100)


class ServiceStatusResponse(BaseModel):
    service: str
    status: Literal["operational", "degraded", "down", "unknown"]
    found: bool


class KnowledgeSearchInput(StrictInput):
    query: str = Field(min_length=1, max_length=300)


class TicketInput(StrictInput):
    title: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1, max_length=3000)


class TicketLookup(StrictInput):
    ticket_id: int = Field(gt=0)


class EscalationInput(TicketInput):
    category: Literal["password_reset", "account_access", "admin_install",
                      "device_compliance", "network_incident", "hardware_repair", "security_review",
                      "vpn_connectivity", "email_service", "service_outage", "other"] = "other"


class DeviceInput(StrictInput):
    device_id: str = Field(min_length=1, max_length=100)


class NoArguments(StrictInput):
    pass


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    employee_id: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class ChatInput(StrictInput):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)


class TicketUpdate(StrictInput):
    status: Literal["open", "in_progress", "resolved"]


class FunctionCall(BaseModel):
    name: str
    arguments: dict


class ToolCall(BaseModel):
    function: FunctionCall


class ModelMessage(BaseModel):
    role: Literal["assistant"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class PasswordChangeInput(LoginInput):
    new_password: str = Field(min_length=12, max_length=256)


class PasswordResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ResetLinkInput(StrictInput):
    employee_id: str = Field(min_length=1, max_length=100)
    identity_verified: Literal[True]


class RecoveryRequestInput(StrictInput):
    employee_id: str = Field(min_length=1, max_length=100)


class DirectoryResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
