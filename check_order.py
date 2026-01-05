import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def check_order():
    client = AsyncIOMotorClient("mongodb+srv://pnpliteuser:pnplite2025@pnplite.e2lfreq.mongodb.net/pnplite")
    db = client.get_default_database()
    order = await db.orders.find_one({"slug": "ABJ-005"})
    print(f"Order: {order}")
    client.close()

if __name__ == "__main__":
    asyncio.run(check_order())
