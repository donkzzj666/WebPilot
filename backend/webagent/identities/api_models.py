"""Metadata-only login requests; credentials belong in the managed website."""
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    site_id: str = Field(min_length=1, max_length=100)
    expected_account: str = Field(min_length=1, max_length=200)
    expected_identity_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator('site_id', 'expected_account', 'expected_identity_ref')
    @classmethod
    def clean_metadata(cls, value):
        if value is not None and (value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Invalid login metadata')
        return value


class LoginCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: int = Field(ge=0, le=2**53 - 1)
