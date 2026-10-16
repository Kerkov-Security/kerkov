"""Front-end-neutral tool-approval types shared by the engine and both adapters."""

from typing import Optional

from pydantic import BaseModel


class ToolApprovalResult(BaseModel):
    approved: bool
    user_message: Optional[str] = None
