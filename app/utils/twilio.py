from typing import Optional
import os
from urllib.parse import urljoin

from fastapi import Header, HTTPException, Request, status
from twilio.request_validator import RequestValidator


def get_request_validator(auth_token: str) -> RequestValidator:
    return RequestValidator(auth_token)


async def verify_twilio_signature(
    request: Request,
    settings_auth_token: str,
    x_twilio_signature: Optional[str] = Header(None, convert_underscores=False),
):
    sig = request.headers.get("x-twilio-signature") or x_twilio_signature
    if not sig:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Twilio-Signature header",
        )

    # Twilio sends the full URL; reconstruct using host + path + query
    # Note: In some deployments behind proxies you may need to account for forwarded headers.
    url = str(request.url)
    form_data = dict(await request.form())
    validator = get_request_validator(settings_auth_token)
    if not validator.validate(url, form_data, str(sig)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Twilio signature",
        )
