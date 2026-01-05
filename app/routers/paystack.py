import hmac
import hashlib
import json
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, Header
from fastapi.responses import JSONResponse

from app.config.db import mongo
from app.config.settings import Settings, get_settings
from app.services.ai import AIService
from app.services.whatsapp_service import WhatsAppService

router = APIRouter()

def get_service(settings: Settings = Depends(get_settings)) -> WhatsAppService:
    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")
    ai_service = AIService(settings.openai_api_key) if settings.openai_api_key else None
    return WhatsAppService(mongo.db, settings, ai_service=ai_service)

@router.post("/webhook")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(None),
    settings: Settings = Depends(get_settings),
    service: WhatsAppService = Depends(get_service),
):
    log_file = "uploads/paystack_webhook.log"
    with open(log_file, "a") as f:
        f.write(f"\n--- {datetime.utcnow().isoformat()} ---\n")
        
        if not x_paystack_signature:
            f.write("ERROR: Missing Paystack signature\n")
            raise HTTPException(status_code=400, detail="Missing Paystack signature")

        body = await request.body()
        f.write(f"Body: {body.decode('utf-8')[:500]}...\n")
        
        # Verify signature
        computed_signature = hmac.new(
            settings.paystack_secret_key.encode(),
            body,
            hashlib.sha512
        ).hexdigest()

        f.write(f"X-Paystack-Signature: {x_paystack_signature}\n")
        f.write(f"Computed Signature: {computed_signature}\n")

        if computed_signature != x_paystack_signature:
            f.write("ERROR: Signature mismatch!\n")
            raise HTTPException(status_code=400, detail="Invalid signature")

        try:
            data = json.loads(body)
            event = data.get("event")
            f.write(f"Event: {event}\n")
            
            if event == "charge.success":
                payload = data.get("data", {})
                metadata = payload.get("metadata", {})
                
                # Paystack sometimes stringifies metadata
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except:
                        f.write(f"ERROR: Could not parse stringified metadata: {metadata}\n")
                
                f.write(f"Metadata: {metadata}\n")
                event_type = metadata.get("type")
                phone = metadata.get("phone")
                amount_paid = payload.get("amount")
                customer = payload.get("customer") or {}
                metadata_name = metadata.get("full_name") or metadata.get("name")
                name_parts = [
                    customer.get("first_name") or "",
                    customer.get("last_name") or ""
                ]
                payer_name = (
                    metadata_name
                    or " ".join([p for p in name_parts if p]).strip()
                    or customer.get("name")
                    or customer.get("email")
                    or phone
                )
                reference = payload.get("reference")
                paid_at = payload.get("paid_at") or datetime.utcnow().isoformat()
                
                if event_type == "membership":
                    membership_type = metadata.get("membership_type")
                    f.write(f"Processing membership for {phone}: {membership_type}\n")
                    # Update member status
                    await mongo.db.members.update_one(
                        {"phone": phone},
                        {"$set": {"payment_status": "paid", "membership_type": membership_type}}
                    )
                    # Notify user
                    try:
                        await service.send_outbound(
                            phone, 
                            f"✅ Your {membership_type} membership has been activated! You can now start adding items to your cart."
                        )
                    except Exception as e:
                        f.write(f"ERROR: Failed to send outbound message: {e}\n")
                    
                elif event_type == "order":
                    order_slug = metadata.get("order_slug")
                    f.write(f"Processing order payment: {order_slug} for {phone}\n")
                    # Update order status
                    result = await mongo.db.orders.update_one(
                        {"slug": order_slug},
                        {"$set": {"status": "PAID"}}
                    )
                    f.write(f"Update result: matched={result.matched_count}, modified={result.modified_count}\n")
                    
                    if result.matched_count > 0:
                        # Notify user
                        try:
                            await service.send_outbound(
                                phone,
                                f"✅ Payment received for Order *{order_slug}*! We are now processing your delivery."
                            )
                        except Exception as e:
                            f.write(f"ERROR: Failed to send outbound message: {e}\n")
                        try:
                            await service.add_notification(
                                type="payment",
                                message=f"Order {order_slug} was paid",
                                metadata={
                                    "order_slug": order_slug,
                                    "phone": phone,
                                    "amount": f"₦{(amount_paid or 0)/100:,.0f}",
                                    "reference": reference,
                                },
                            )
                        except Exception as e:
                            f.write(f"ERROR: Failed to log payment notification: {e}\n")
                        try:
                            order_doc = await mongo.db.orders.find_one({"slug": order_slug})
                            if order_doc:
                                await service.award_referral_commission(order_doc)
                        except Exception as e:
                            f.write(f"ERROR: Failed to award referral commission: {e}\n")
                    else:
                        f.write(f"WARNING: No order found with slug {order_slug}\n")
                
                elif event_type == "cluster_order":
                    order_slug = metadata.get("order_slug")
                    cluster_id = metadata.get("cluster_id")
                    cluster_name = metadata.get("cluster_name")
                    owner_phone = metadata.get("owner_phone")
                    share_kobo = metadata.get("share_kobo") or amount_paid
                    paid_value = amount_paid or share_kobo or 0
                    f.write(f"Processing cluster payment for order {order_slug} from {phone}\n")
                    
                    order = await mongo.db.orders.find_one({"slug": order_slug})
                    if not order:
                        f.write(f"WARNING: Cluster order not found for slug {order_slug}\n")
                    else:
                        payments = order.get("cluster_payments", [])
                        updated = False
                        for p in payments:
                            if p.get("phone") == phone:
                                p.update(
                                    {
                                        "status": "PAID",
                                        "amount_kobo": amount_paid,
                                        "reference": reference,
                                        "paid_at": paid_at,
                                        "payer_name": payer_name,
                                    }
                                )
                                updated = True
                                break
                        if not updated:
                            payments.append(
                                {
                                    "phone": phone,
                                    "status": "PAID",
                                    "amount_kobo": amount_paid,
                                    "reference": reference,
                                    "paid_at": paid_at,
                                    "payer_name": payer_name,
                                }
                            )
                        
                        paid_amount = sum(p.get("amount_kobo", 0) or 0 for p in payments if p.get("status") == "PAID")
                        total_kobo_raw = metadata.get("total_kobo")
                        try:
                            total_kobo_target = int(total_kobo_raw)
                        except Exception:
                            total_kobo_target = int((order.get("total") or 0) * 100)
                        members = order.get("cluster_members") or []
                        paid_count = len([p for p in payments if p.get("status") == "PAID"])
                        expected_count = len(members)
                        all_paid = paid_amount >= total_kobo_target or (expected_count and paid_count >= expected_count)
                        
                        update_fields = {
                            "cluster_payments": payments,
                            "cluster_paid_amount_kobo": paid_amount,
                        }
                        if all_paid:
                            update_fields["status"] = "PAID"
                        await mongo.db.orders.update_one({"slug": order_slug}, {"$set": update_fields})
                        
                        # Dashboard notification
                        try:
                            await service.add_notification(
                                type="payment",
                                message=f"Cluster payment from {payer_name} for {cluster_name or order_slug}",
                                metadata={
                                    "order_slug": order_slug,
                                    "cluster": cluster_name or cluster_id,
                                    "phone": phone,
                                    "amount": f"₦{paid_value/100:,.0f}",
                                    "reference": reference,
                                },
                            )
                        except Exception as e:
                            f.write(f"ERROR: Failed to log cluster payment notification: {e}\n")

                        # Notify payer
                        try:
                            await service.send_outbound(
                                phone,
                                f"✅ Payment received for {cluster_name or 'cluster cart'} (Order *{order_slug}*). Thanks!"
                            )
                        except Exception as e:
                            f.write(f"ERROR: Failed to notify payer: {e}\n")

                        # Notify owner with full name if available
                        owner_phone = owner_phone or order.get("cluster_owner_phone")
                        try:
                            owner_label = cluster_name or "your cluster cart"
                            owner_msg = f"{payer_name} just paid ₦{paid_value/100:,.0f} towards {owner_label} (Order {order_slug})."
                            if owner_phone:
                                await service.send_outbound(owner_phone, owner_msg)
                        except Exception as e:
                            f.write(f"ERROR: Failed to notify cluster owner: {e}\n")

                        # Notify other members generically
                        members = members or []
                        for m in members:
                            if m in {phone, owner_phone}:
                                continue
                            try:
                                await service.send_outbound(
                                    m,
                                    f"Someone in your {cluster_name or 'cluster'} has paid towards the cart. We'll update you when it's fully paid."
                                )
                            except Exception as e:
                                f.write(f"ERROR: Failed to notify cluster member {m}: {e}\n")

                        if all_paid:
                            try:
                                await service.add_notification(
                                    type="payment",
                                    message=f"Cluster order {order_slug} is fully paid",
                                    metadata={"order_slug": order_slug, "cluster": cluster_name or cluster_id},
                                )
                            except Exception as e:
                                f.write(f"ERROR: Failed to log cluster completion: {e}\n")
                            try:
                                if owner_phone:
                                    await service.send_outbound(
                                        owner_phone,
                                        f"🎉 All payments received for {cluster_name or 'your cluster cart'} (Order *{order_slug}*). We'll start processing now."
                                    )
                            except Exception as e:
                                f.write(f"ERROR: Failed to notify owner about completion: {e}\n")
                            try:
                                order_doc = await mongo.db.orders.find_one({"slug": order_slug})
                                if order_doc:
                                    await service.award_referral_commission(order_doc)
                            except Exception as e:
                                f.write(f"ERROR: Failed to award referral commission: {e}\n")

        except Exception as e:
            f.write(f"CRITICAL ERROR: {str(e)}\n")
            import traceback
            f.write(traceback.format_exc())

    return JSONResponse(content={"status": "success"}, status_code=200)
