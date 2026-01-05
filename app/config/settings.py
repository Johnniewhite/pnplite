from __future__ import annotations

from functools import lru_cache
from typing import List, Optional, Union

from pydantic import Field, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_uri: str = Field(..., env="MONGO_URI")
    twilio_auth_token: str = Field(..., env="TWILIO_AUTH_TOKEN")
    twilio_account_sid: str = Field(..., env="TWILIO_ACCOUNT_SID")
    twilio_from_number: str = Field("whatsapp:+2348083265499", env="TWILIO_FROM_NUMBER")
    twilio_template_sid_broadcast: Optional[str] = Field(default=None, env="TWILIO_TEMPLATE_SID_BROADCAST")
    twilio_status_callback_url: Optional[str] = Field(default=None, env="TWILIO_STATUS_CALLBACK_URL")
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")
    ngrok_url: Optional[str] = Field(default=None, env="NGROK_URL")

    admin_numbers: Union[List[str], str] = Field(default_factory=list, env="ADMIN_NUMBERS")

    price_sheet_url: Optional[str] = Field(default=None, env="PRICE_SHEET_URL")
    admin_dash_password: Optional[str] = Field(default=None, env="ADMIN_DASH_PASSWORD")
    public_base_url: Optional[str] = Field(default=None, env="PUBLIC_BASE_URL")

    paystack_public_key: Optional[str] = Field(default=None, env="PAYSTACK_PUBLIC_KEY")
    paystack_secret_key: Optional[str] = Field(default=None, env="PAYSTACK_SECRET_KEY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @validator("admin_numbers", pre=True)
    def split_admin_numbers(cls, v):
        if not v:
            return []
        if isinstance(v, list):
            return v
        # Accept comma or semicolon separated numbers
        return [num.strip() for num in str(v).replace(";", ",").split(",") if num.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()
