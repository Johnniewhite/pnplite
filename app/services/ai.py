from typing import Optional

from openai import AsyncOpenAI
from typing import Dict, Any, Optional


class AIService:
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

        # Constrained system prompt to keep replies on-brand and menu-focused
        self.system_prompt = (
            "You are PNP Lite's WhatsApp assistant. Be concise, friendly, and natural—no numbered menus unless absolutely needed. "
            "Collect missing details (name, city: PH/Lagos Mainland/Lagos Island/Abuja, membership: lifetime 50k / monthly 5k / one-time 2k). "
            "If someone says 'Lagos', you MUST ask if they are on the Mainland or Island. "
            "Acknowledge payment proofs, help with pricing/order/referral questions, and keep replies short. "
            "If unsure, ask a simple clarifying question instead of a menu."
        )

    async def faq_reply(self, user_message: str, context: Optional[str] = None) -> Optional[str]:
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "system", "content": f"Context: {context}"} if context else None,
                    {"role": "user", "content": user_message},
                ],
                max_tokens=180,
                temperature=0.4,
            )
            return completion.choices[0].message.content.strip()
        except Exception:
            # Fallback handled by caller
            return None

    async def classify_intent(self, user_message: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Classify user intent including cart interactions and custom clusters.
        Returns one of: catalog_search, cart_checkout, cart_add, cart_remove, cart_view, referral_link, order_help, cluster_create, cluster_join, other.
        """
        try:
            messages = [
                {
                    "role": "system",
                    "content": "You are an intent classifier for a commerce bot. "
                    "Return exactly one token from {catalog_search, cart_checkout, cart_add, cart_remove, cart_view, referral_link, order_help, cluster_create, cluster_join, cluster_view, cluster_rename, other}. "
                    "catalog_search: user asks for products, what do you have, list items, browse, or wants a specific product. "
                    "cart_checkout: wants to finalize or checkout. "
                    "cart_add: wants to add specific product(s) to cart. "
                    "cart_remove: wants to remove/delete item from cart. "
                    "cart_view: asks what is in cart, show cart, my items, 'other' cart, or mentions a specific cluster name to see its items. "
                    "referral_link: wants their referral or invite link, wants to refer a friend. "
                    "order_help: general buying/price/order questions. "
                    "cluster_create: user wants to create a group, cluster, or shared cart with friends. "
                    "cluster_view: user asks about their clusters, groups they are in, or cluster status. "
                    "cluster_rename: user wants to change the name of their cluster. "
                    "other: casual chat or anything else. "
                    f"Context: {context}",
                },
                {"role": "user", "content": user_message},
            ]
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=6,
                temperature=0,
            )
            token = completion.choices[0].message.content.strip().lower()
            allowed = {"catalog_search", "cart_checkout", "cart_add", "cart_remove", "cart_view", "referral_link", "order_help", "cluster_create", "cluster_join", "cluster_view", "cluster_rename", "other"}
            return token if token in allowed else "other"
        except Exception:
            return None

    async def extract_cluster_details(self, user_message: str) -> Optional[Dict[str, Any]]:
        """
        Extract cluster name and max people.
        """
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Extract cluster details. Return JSON: { \"name\": \"string name\", \"max_people\": number, \"new_name\": \"string\" }. "
                        "For creation: extract the intended name and max_people. Default max_people is 5. If name is missing, use null. "
                        "For renaming: extract the DESIRED new name into 'new_name'. Be inclusive of descriptive phrases (e.g., 'MegaCluster for rice' -> 'MegaCluster for rice')."
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=100,
                temperature=0,
                response_format={"type": "json_object"},
            )
            import json
            data = json.loads(completion.choices[0].message.content)
            return data
        except Exception:
            return None

    async def extract_cart_action(self, user_message: str) -> Optional[list[Dict[str, Any]]]:
        """
        Extract action details: [{action: 'remove'|'add', item: 'rice', qty: 2}, ...]
        Returns a list of actions.
        """
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Extract cart action details. Return JSON: { \"actions\": [{ \"action\": \"remove\"|\"add\"|\"view\", \"item\": \"string name\", \"qty\": number, \"target\": \"personal\"|\"cluster\", \"cluster_name\": \"string\" }] }. "
                        "Default qty is 1. Item name should be short keywords. "
                        "Target: 'personal' if user says 'my cart', 'personal', or 'my'. "
                        "Target: 'cluster' if they mention 'cluster', 'group', 'shared', or a specific cluster name. "
                        "If they mention a specific name like 'Shineshine' or 'MegaCluster', put that in 'cluster_name'. "
                        "If they just say 'see cart' and are in a cluster, default to 'cluster' UNLESS they specify 'my'. "
                        "IMPORTANT: If the action is just viewing (no add/remove), use action='view' and item=null."
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=150,
                temperature=0,
                response_format={"type": "json_object"},
            )
            import json
            data = json.loads(completion.choices[0].message.content)
            return data.get("actions", [])
        except Exception:
            return None

    async def generate_response(self, user_message: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Generate a natural response using available context (cart, user info).
        """
        try:
            cart_info = "Cart is empty."
            if context.get("cart_items"):
                items = [f"{it['name']} x{it['qty']}" for it in context["cart_items"]]
                cart_info = f"Cart contains: {', '.join(items)}."
            
            system_msg = (
                f"{self.system_prompt} "
                f"User Info: {context.get('member_name')}, {context.get('member_city')}. "
                f"Membership: {context.get('membership') or 'None'} (Paid: {context.get('paid')}). "
                f"Cart: [{cart_info}]. "
                f"Clusters Owned: {', '.join(context.get('owned_clusters') or []) or 'None'}. "
                f"Clusters Joined: {', '.join(context.get('joined_clusters') or []) or 'None'}. "
                f"Active Cart: {'In cluster: ' + context.get('current_cluster') if context.get('current_cluster') else 'Personal Cart'}. "
                "If they ask for their referral link, providing this format: https://wa.me/[bot_phone]?text=I%20was%20referred%20by%20[Name]. "
                "If they ask about the cart, summarize it naturally. "
                "If they ask about their clusters, summarize what they are in. "
                "If they have membership/paid, do NOT ask for it again. "
                "If they specifically ask for products and none are provided in context, advise them to use the 'Menu' or 'Search' features, but do NOT invent products."
            )

            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=200,
                temperature=0.5,
            )
            return completion.choices[0].message.content.strip()
        except Exception:
            return None

    async def extract_product_query(self, user_message: str) -> Optional[str]:
        """
        Extract a short product query (e.g., 'rice', 'bag of rice') for search.
        """
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Extract the product(s) the user wants in a few words. "
                        "Return only the product phrase (e.g., 'bag of rice'). If unsure, return an empty string.",
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=12,
                temperature=0,
            )
            q = completion.choices[0].message.content.strip()
            # If the user asks broadly "what products do you have", returning "products" or "have" is bad.
            # Return empty string to trigger full catalog view.
            stop_words = ["products", "what", "items", "stock", "have", "sell", "do you have", "available", "list", "show", "menu", "option", "options"]
            if q.lower() in stop_words or any(x in q.lower() for x in ["what products", "list products"]):
                return ""
            return q or None
            q = q.split("\n")[0].strip()
            return q or None
        except Exception:
            return None

    async def extract_name(self, user_message: str) -> Optional[str]:
        """
        Ask the model to return only a probable name. It should strip prefixes like
        "call me", "my name is", etc. Returns None on failure.
        """
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Extract only the person's name from the user's message. "
                        "Do not include extra words. If unsure, return just the best guess name.",
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=10,
                temperature=0.2,
            )
            name = completion.choices[0].message.content.strip()
            # Safety: keep to a short token (no commas/newlines)
            name = name.split("\n")[0].strip(",.! ")
            return name
        except Exception:
            return None

    async def extract_city(self, user_message: str, allowed: Optional[list[str]] = None) -> Optional[str]:
        try:
            allowed_list = allowed or ["PH", "Port Harcourt", "Lagos Mainland", "Lagos Island", "Abuja"]
            prompt = (
                "Extract the city from the user's message. "
                f"Allowed options: {', '.join(allowed_list)}. "
                "If the user says 'Lagos' without specifying, return an empty string so I can ask for clarification. "
                "Return exactly one of those values (e.g., 'PH', 'Lagos Mainland', or 'Abuja'). "
                "If unsure, return an empty string."
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=10,
                temperature=0.2,
            )
            city = completion.choices[0].message.content.strip()
            city = city.split("\n")[0].strip(",.! ")
            if city:
                return city
            return None
        except Exception:
            return None

    async def extract_membership(self, user_message: str) -> Optional[str]:
        """
        Normalize membership to one of: lifetime, monthly, onetime.
        """
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Map the user's reply to one of: lifetime, monthly, onetime. "
                        "Consider hints like '50k' -> lifetime, '5k' -> monthly, '2k' or 'one time' -> onetime. "
                        "Return only the keyword.",
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=5,
                temperature=0,
            )
            choice = completion.choices[0].message.content.strip().lower()
            if any(k in choice for k in ["life", "50"]):
                return "lifetime"
            if any(k in choice for k in ["month", "5k"]):
                return "monthly"
            if any(k in choice for k in ["one", "once", "2k"]):
                return "onetime"
            return None
        except Exception:
            return None

