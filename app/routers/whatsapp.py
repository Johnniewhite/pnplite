from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from twilio.twiml.messaging_response import MessagingResponse

from app.config.db import mongo
from app.config.settings import Settings, get_settings
from app.services.ai import AIService
from app.services.whatsapp_service import WhatsAppService
from app.utils.twilio import verify_twilio_signature
from app.models.schemas import MessageDirection
from app.models.broadcast import MessageStatusLog


router = APIRouter()


def get_service(settings: Settings = Depends(get_settings)) -> WhatsAppService:
    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")
    ai_service = AIService(settings.openai_api_key, db=mongo.db) if settings.openai_api_key else None
    return WhatsAppService(mongo.db, settings, ai_service=ai_service)


@router.post("/webhook", response_class=PlainTextResponse)
async def whatsapp_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    service: WhatsAppService = Depends(get_service),
):
    # Validate signature
    await verify_twilio_signature(request, settings.twilio_auth_token)

    form = await request.form()
    body = (form.get("Body") or "").strip()
    # Handle button clicks - ButtonText contains the button label
    button_text = form.get("ButtonText", "").strip()
    button_payload = form.get("ButtonPayload", "").strip()
    
    # If button was clicked, use button text as the message unless we have a payload
    if button_text:
        body = button_text
    
    from_phone = (form.get("From") or "").replace("whatsapp:", "")
    # Capture first media attachment if present
    try:
        num_media = int(form.get("NumMedia") or "0")
    except ValueError:
        num_media = 0
    media_url = form.get("MediaUrl0") if num_media > 0 else None

    original_replied_sid = form.get("OriginalRepliedMessageSid", "").strip() or form.get("Context", {}).get("MessageId") # Fallback to Context logic if parsed manually, but form usually has it flattened? Twilio flattens it.
    
    # Check Context if flattened not available
    if not original_replied_sid and form.get("Context"):
        import json
        try:
            ctx = json.loads(form.get("Context"))
            original_replied_sid = ctx.get("MessageId") or ctx.get("id")
        except:
            pass

    reply_text, next_state, state_before, intent, ai_used, button_actions = await service.handle_inbound(
        from_phone, body, media_url=media_url, button_payload=button_payload, context_id=original_replied_sid
    )

    await service.log_message(
        phone=from_phone,
        direction=MessageDirection.inbound,
        body=body or ("[media]" if media_url else ""),
        state_before=state_before,
        state_after=next_state,
        intent=intent,
        media_url=media_url,
    )

    resp = MessagingResponse()

    # Send the reply text via TwiML (simple text response, no templates)
    if reply_text and reply_text.strip():
        if settings.twilio_status_callback_url:
            resp.message(reply_text, status_callback=settings.twilio_status_callback_url)
        else:
            resp.message(reply_text)

    await service.log_message(
        phone=from_phone,
        direction=MessageDirection.outbound,
        body=reply_text,
        state_before=next_state,
        state_after=next_state,
        intent=intent,
        ai_used=ai_used,
    )

    # Twilio expects XML string
    return Response(str(resp), media_type="text/xml")


@router.post("/status", response_class=PlainTextResponse)
async def whatsapp_status_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    await verify_twilio_signature(request, settings.twilio_auth_token)
    form = await request.form()
    message_sid = form.get("MessageSid")
    status = form.get("MessageStatus")
    to = form.get("To")
    error_code = form.get("ErrorCode")
    error_message = form.get("ErrorMessage")

    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")

    log = MessageStatusLog(
        message_sid=message_sid or "",
        status=status or "",
        to=to,
        error_code=error_code,
        error_message=error_message,
        raw=dict(form),
    )
    await mongo.db.message_status.insert_one(log.dict())
    return PlainTextResponse("ok")
