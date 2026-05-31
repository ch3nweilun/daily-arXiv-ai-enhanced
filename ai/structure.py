from pydantic import BaseModel, Field, field_validator
from typing import Any
import json
import re

class Structure(BaseModel):
    tldr: str = Field(description="generate a too long; didn't read summary")
    motivation: str = Field(description="describe the motivation in this paper")
    method: str = Field(description="method of this paper")
    result: str = Field(description="result of this paper")
    conclusion: str = Field(description="conclusion of this paper")


class FullTextStructure(BaseModel):
    summary: str = Field(description="full-paper summary")
    key_contributions: str = Field(description="key contributions of this paper")
    method_details: str = Field(description="important method details")
    limitations: str = Field(description="limitations or caveats")
    why_it_matters: str = Field(description="why this paper matters")

    @field_validator("*", mode="before")
    @classmethod
    def stringify_fields(cls, value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return str(value)
