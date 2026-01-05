import aiohttp
from typing import Dict, Any, Optional
from app.config.settings import Settings

class PaystackService:
    def __init__(self, settings: Settings):
        self.secret_key = settings.paystack_secret_key
        self.base_url = "https://api.paystack.co"
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    async def initialize_transaction(self, email: str, amount_kobo: int, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Initialize a transaction with Paystack.
        amount_kobo: Amount in kobo (e.g. 10000 for NGN 100)
        """
        url = f"{self.base_url}/transaction/initialize"
        payload = {
            "email": email,
            "amount": amount_kobo,
            "metadata": metadata,
            # We can also add a callback_url here if needed, but we'll rely on webhooks mostly.
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data")
                else:
                    text = await resp.text()
                    print(f"Paystack initialization failed: {resp.status} - {text}")
                    return None

    async def verify_transaction(self, reference: str) -> Optional[Dict[str, Any]]:
        """
        Verify a transaction with Paystack using its reference.
        """
        url = f"{self.base_url}/transaction/verify/{reference}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data")
                else:
                    return None
