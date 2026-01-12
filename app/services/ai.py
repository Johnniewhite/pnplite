from typing import Optional

from openai import AsyncOpenAI
from typing import Dict, Any, Optional


class AIService:
    def __init__(self, api_key: str, db=None):
        self.client = AsyncOpenAI(api_key=api_key)
        self.db = db
        # Default system prompt - can be overridden by database config
        self._default_system_prompt = (
            "You are PNP Lite's WhatsApp assistant. PNP Lite is a group-buying community where members shop together "
            "to access wholesale prices and share delivery costs. "
            "Be concise, friendly, and natural—no numbered menus unless absolutely needed. "
            "Collect missing details (name, city: PH/Lagos Mainland/Lagos Island/Abuja, membership: lifetime 50k / monthly 5k / one-time 2k). "
            "If someone says 'Lagos', you MUST ask if they are on the Mainland or Island. "
            "Explain subscription plans clearly when asked: Lifetime (50k one-off), Monthly (5k/month), One-time (2k per access). "
            "Acknowledge payment proofs, help with pricing/order/referral questions, and keep replies short. "
            "If unsure, ask a simple clarifying question instead of a menu."
        )
    
    async def get_system_prompt(self) -> str:
        """Get system prompt from database config or return default."""
        if self.db:
            try:
                config = await self.db.config.find_one({"_id": "bot_system_prompt"})
                if config and config.get("value"):
                    return config["value"]
            except:
                pass
        return self._default_system_prompt
    
    @property
    def system_prompt(self) -> str:
        """Backward compatibility property - returns default for sync access."""
        return self._default_system_prompt

    async def faq_reply(self, user_message: str, context: Optional[str] = None) -> Optional[str]:
        try:
            system_prompt = await self.get_system_prompt()
            messages = [
                {"role": "system", "content": system_prompt},
            ]
            if context:
                messages.append({"role": "system", "content": f"Context: {context}"})
            messages.append({"role": "user", "content": user_message})
            
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
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
        Returns one of: catalog_search, cart_checkout, cart_add, cart_remove, cart_view, referral_link,
        order_help, cluster_create, cluster_join, menu_help, payment_confirmation, other.
        """
        try:
            context_str = ""
            if context:
                context_str = f"User context: in_cluster={context.get('in_cluster')}, has_personal_items={context.get('has_personal_items')}, payment_status={context.get('payment_status')}"

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an advanced intent classifier for PNP Lite, a WhatsApp grocery shopping bot. "
                        "Analyze the user's message and return EXACTLY ONE intent token.\n\n"
                        "Available intents:\n"
                        "- catalog_search: IMPORTANT - This should be your DEFAULT for product-related queries. Use when:\n"
                        "  * User mentions ANY food/grocery item (rice, oil, indomie, spaghetti, milk, bread, sugar, etc.)\n"
                        "  * User asks what's available, list items, browse catalog, products\n"
                        "  * User wants to see or find something to buy\n"
                        "  * Single word that could be a product name (e.g., 'oil', 'rice', 'butter')\n"
                        "  * ANY message that seems shopping-related\n"
                        "- cart_checkout: wants to finalize purchase, pay, or checkout\n"
                        "- cart_add: explicitly wants to ADD a product to cart (says 'add', 'add to cart')\n"
                        "- cart_remove: wants to remove/delete item from cart\n"
                        "- cart_view: asks to see cart contents, 'my cart', 'show cart'\n"
                        "- referral_link: wants their referral/invite link to share with friends\n"
                        "- menu_help: asks for menu, help, commands, how to use, what can I do\n"
                        "- payment_confirmation: confirms payment, sent payment proof, or asking about payment status\n"
                        "- order_help: questions about orders, delivery, tracking\n"
                        "- cluster_create: wants to create a group/cluster/shared cart\n"
                        "- cluster_join: wants to join a group/cluster\n"
                        "- cluster_view: asks about their clusters, groups they're in\n"
                        "- cluster_rename: wants to change cluster name\n"
                        "- other: ONLY for greetings (hi, hello), thank you, or clear non-shopping chat\n\n"
                        f"{context_str}\n"
                        "When in doubt between catalog_search and other, choose catalog_search.\n"
                        "Return ONLY the intent token, nothing else."
                    ),
                },
                {"role": "user", "content": user_message},
            ]
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=20,
                temperature=0.3,
            )
            token = completion.choices[0].message.content.strip().lower()
            allowed = {
                "catalog_search", "cart_checkout", "cart_add", "cart_remove", "cart_view",
                "referral_link", "order_help", "cluster_create", "cluster_join",
                "cluster_view", "cluster_rename", "menu_help", "payment_confirmation", "other"
            }
            return token if token in allowed else "other"
        except Exception as e:
            # Log error but don't crash
            print(f"AI intent classification error: {e}")
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
        Generate a natural, contextual response using available user information.
        """
        try:
            # Build rich context description
            cart_info = "empty"
            if context.get("cart_items"):
                items = [f"{it['name']} x{it['qty']}" for it in context["cart_items"]]
                cart_info = f"{', '.join(items)}"

            # Determine user status
            payment_status = "unpaid" if not context.get('paid') else "paid"
            membership_info = context.get('membership') or 'no membership'

            # Build cluster information
            cluster_info = "not in any cluster"
            if context.get('current_cluster'):
                cluster_info = f"currently in cluster '{context.get('current_cluster')}'"
            elif context.get('owned_clusters') or context.get('joined_clusters'):
                all_clusters = (context.get('owned_clusters') or []) + (context.get('joined_clusters') or [])
                cluster_info = f"member of: {', '.join(all_clusters)}"

            system_msg = (
                "You are PNP Lite's friendly WhatsApp shopping assistant. Respond naturally and helpfully.\n\n"
                f"**USER CONTEXT:**\n"
                f"- Name: {context.get('member_name', 'Unknown')}\n"
                f"- City: {context.get('member_city', 'Unknown')}\n"
                f"- Membership: {membership_info} ({payment_status})\n"
                f"- Cart: {cart_info}\n"
                f"- Clusters: {cluster_info}\n\n"
                "**GUIDELINES:**\n"
                "1. Be conversational, warm, and helpful\n"
                "2. If they greet you, greet back warmly and ask how you can help\n"
                "3. If they ask about products, encourage them to search or browse the catalog\n"
                "4. If they ask about orders/delivery, provide helpful information\n"
                "5. If they need their referral link, tell them to say 'referral link'\n"
                "6. If they seem confused, guide them on what they can do\n"
                "7. Keep responses concise (2-3 sentences max)\n"
                "8. Use their name occasionally to personalize\n"
                "9. If payment is unpaid, gently remind them when relevant\n"
                "10. NEVER invent product names or prices - direct them to search instead\n\n"
                "Respond naturally to their message based on the context above."
            )

            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=250,
                temperature=0.7,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI generate_response error: {e}")
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

