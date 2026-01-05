import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.services.paystack import PaystackService
from app.config.settings import Settings
import os

async def manual_verify():
    # Load settings manually for simplicity in script
    settings = Settings()
    settings.paystack_secret_key = "sk_test_78aef21664e5ccd4fefaea2a8d64529a8555e5b2" # Directly from .env
    
    ps = PaystackService(settings)
    ref = "rjzo2hsou3"
    
    print(f"Verifying reference: {ref}")
    result = await ps.verify_transaction(ref)
    
    if result and result.get("status") == "success":
        print("Success! Updating database...")
        metadata = result.get("metadata", {})
        order_slug = metadata.get("order_slug")
        
        if order_slug:
            client = AsyncIOMotorClient("mongodb+srv://pnpliteuser:pnplite2025@pnplite.e2lfreq.mongodb.net/pnplite")
            db = client.get_default_database()
            update = await db.orders.update_one(
                {"slug": order_slug},
                {"$set": {"status": "PAID", "payment_ref": ref}}
            )
            print(f"Update result: matched={update.matched_count}, modified={update.modified_count}")
            
            from app.services.whatsapp_service import WhatsAppService
            from app.services.ai import AIService
            ai = AIService(settings.openai_api_key)
            service = WhatsAppService(db, settings, ai_service=ai)
            
            phone = metadata.get("phone")
            if phone:
                print(f"Sending notification to {phone}")
                await service.send_outbound(
                    phone,
                    f"✅ Payment received for Order *{order_slug}*! We are now processing your delivery."
                )
            client.close()
        else:
            print(f"No order_slug found in metadata: {metadata}")
    else:
        print(f"Verification failed or status not success: {result}")

if __name__ == "__main__":
    asyncio.run(manual_verify())
