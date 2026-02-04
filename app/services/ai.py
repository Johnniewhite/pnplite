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
                context_parts = []
                if context.get('in_cluster'):
                    context_parts.append(f"in_cluster={context.get('in_cluster')}")
                if context.get('has_personal_items'):
                    context_parts.append(f"has_personal_items={context.get('has_personal_items')}")
                if context.get('payment_status'):
                    context_parts.append(f"payment_status={context.get('payment_status')}")
                if context.get('in_cart_action_state'):
                    context_parts.append("IMPORTANT: User is in cart action state - they were just shown a product and asked if they want to add it. Responses like 'add', 'yes', 'ok' should be classified as 'cart_add'.")
                if context.get('has_product_selected'):
                    context_parts.append(f"User has selected product: {context.get('product_name', '')}")
                context_str = f"User context: {', '.join(context_parts)}" if context_parts else ""

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an intent classifier for PNP Lite, a WhatsApp grocery shopping bot.\n"
                        "Return EXACTLY ONE intent token.\n\n"
                        "INTENTS:\n"
                        "- catalog_search: DEFAULT for ANY product/shopping question. Use for:\n"
                        "  * 'what products do you have?', 'what do you sell?', 'show me products'\n"
                        "  * 'what's available?', 'list items', 'browse catalog'\n"
                        "  * ANY food/grocery item: rice, oil, indomie, milk, bread, etc.\n"
                        "  * Single words that could be products: 'oil', 'rice', 'butter'\n"
                        "  * Questions about products: 'do you have rice?', 'how much is oil?'\n"
                        "- cart_checkout: 'checkout', 'pay', 'finalize order', 'proceed to payment'\n"
                        "- cart_add: 'add to cart', 'yes' (after product shown), 'add it', 'I want this'\n"
                        "- cart_remove: 'remove', 'delete from cart', 'take out'\n"
                        "- cart_view: 'my cart', 'show cart', 'what's in my cart'\n"
                        "- referral_link: 'referral link', 'invite link', 'share link'\n"
                        "- menu_help: 'help', 'menu', 'what can you do', 'commands', user seems confused\n"
                        "- payment_confirmation: 'I paid', 'payment sent', payment proof, payment status\n"
                        "- order_help: 'my order', 'delivery status', 'track order', 'where is my order'\n"
                        "- cluster_create: 'create group', 'create cluster', 'start a group'\n"
                        "- cluster_join: 'join group', 'join cluster'\n"
                        "- cluster_view: 'my groups', 'my clusters'\n"
                        "- cluster_rename: 'rename cluster', 'change group name'\n"
                        "- other: ONLY for pure greetings (hi, hello) or thank you messages\n\n"
                        f"{context_str}\n"
                        "IMPORTANT: When in doubt, choose 'catalog_search'. Never return 'other' for product questions.\n"
                        "Return ONLY the intent token."
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
                "3. If they ask about products, encourage them to search or browse the catalog by typing 'rice', 'oil', etc.\n"
                "4. If they ask about orders/delivery, provide helpful information\n"
                "5. If they need their referral link, tell them to say 'referral link'\n"
                "6. If they seem confused, suggest: 'Type MENU to see what I can do'\n"
                "7. Keep responses concise (2-3 sentences max)\n"
                "8. Use their name occasionally to personalize\n"
                "9. If payment is unpaid, gently remind them when relevant\n"
                "10. NEVER invent product names or prices - direct them to search instead\n"
                "11. ALWAYS explain HOW to use a feature if you mention it (e.g., 'To view your cart, just type Cart').\n\n"
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
        Extract a short product query (e.g., 'rice', 'bag of rice', 'Indomie') for search.
        Handles questions like "Do you have Indomie?" -> "Indomie"
        """
        try:
            prompt = (
                "Extract the product name or product query from the user's message. "
                "Return ONLY the product name/phrase, nothing else.\n\n"
                "Examples:\n"
                "- User: 'Do you have Indomie?' → Return: 'Indomie'\n"
                "- User: 'Do you sell rice?' → Return: 'rice'\n"
                "- User: 'I need oil' → Return: 'oil'\n"
                "- User: 'Do you have big bull rice?' → Return: 'big bull rice'\n"
                "- User: 'What products do you have?' → Return empty string\n"
                "- User: 'Show me products' → Return empty string\n\n"
                "If the message is a question about a specific product, extract just the product name. "
                "If it's a general question about products/catalog, return an empty string. "
                "Do not include question words, punctuation, or extra text - just the product name/phrase."
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=15,
                temperature=0.1,
            )
            q = completion.choices[0].message.content.strip()
            # Remove quotes if AI adds them
            q = q.strip('"\'')
            # Remove newlines
            q = q.split("\n")[0].strip()
            # Trust AI to return empty string for general queries based on prompt instructions
            # No keyword filtering - AI handles this intelligently
            return q or None
        except Exception:
            return None

    async def extract_name(self, user_message: str) -> Optional[str]:
        """
        Extract just the person's name from conversational input.
        Handles: "call me John", "my name is Sarah", "I'm Mike actually", "John please", etc.
        """
        try:
            prompt = (
                "You are a name extractor. Extract ONLY the person's actual name from the message.\n\n"
                "RULES:\n"
                "- Remove ALL filler words: 'actually', 'please', 'thanks', 'just', 'simply', etc.\n"
                "- Remove ALL prefixes: 'call me', 'my name is', 'I am', 'I'm', 'you can call me', 'it's', etc.\n"
                "- Remove ALL suffixes: 'please', 'thanks', 'actually', 'though', etc.\n"
                "- Return ONLY the name itself - nothing else\n"
                "- If multiple names given, return just the first/primary name\n"
                "- Capitalize properly (e.g., 'john' → 'John')\n\n"
                "EXAMPLES:\n"
                "Input: 'call me John actually' → Output: 'John'\n"
                "Input: 'my name is Sarah please' → Output: 'Sarah'\n"
                "Input: 'I'm Mike' → Output: 'Mike'\n"
                "Input: 'You can call me Ada' → Output: 'Ada'\n"
                "Input: 'John' → Output: 'John'\n"
                "Input: 'its chioma' → Output: 'Chioma'\n"
                "Input: 'Emeka is my name' → Output: 'Emeka'\n"
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Extract name from: {user_message}"},
                ],
                max_tokens=15,
                temperature=0,
            )
            name = completion.choices[0].message.content.strip()
            # Clean up any quotes or extra formatting
            name = name.replace('"', '').replace("'", '').split("\n")[0].strip(",.! ")
            print(f"DEBUG: AI name extraction - Input: '{user_message}' → Output: '{name}'")
            return name if name else None
        except Exception as e:
            print(f"AI name extraction error: {e}")
            return None

    async def extract_city(self, user_message: str, allowed: Optional[list[str]] = None) -> Optional[str]:
        """
        Extract city from natural language. Uses AI to understand any phrasing.
        Returns: "PH", "Lagos", "Abuja", or None
        """
        try:
            allowed_list = allowed or ["PH", "Lagos", "Abuja"]
            prompt = (
                "You are a Nigerian city extractor. Your job is to identify which city the user is referring to.\n\n"
                f"VALID OUTPUTS: {', '.join(allowed_list)}\n\n"
                "UNDERSTAND CONTEXT: Users may say things like:\n"
                "- Direct: 'Abuja', 'PH', 'Lagos'\n"
                "- Conversational: 'I am in Abuja', 'I'm from PH', 'I live in Lagos'\n"
                "- Partial: 'Lago', 'Abuj', 'Port Harcourt', 'Harcourt'\n"
                "- Typos: 'Laogs', 'Abja', 'Port hacourt'\n"
                "- Slang: 'Naija capital' (Abuja), 'garden city' (PH), 'eko' (Lagos)\n\n"
                "MAPPING:\n"
                "- Port Harcourt, PH, Ph, garden city, rivers → 'PH'\n"
                "- Lagos, Lag, Eko, Mainland, Island, Lekki, VI, Ikeja → 'Lagos'\n"
                "- Abuja, FCT, capital, Abj → 'Abuja'\n\n"
                "OUTPUT: Return ONLY one of: PH, Lagos, Abuja\n"
                "If you cannot determine the city, return empty string.\n"
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=10,
                temperature=0,
            )
            city = completion.choices[0].message.content.strip()
            city = city.replace('"', '').replace("'", '').split("\n")[0].strip(",.! ")

            print(f"DEBUG: AI city extraction - Input: '{user_message}' → Output: '{city}'")

            # Validate against allowed list
            if city and city in allowed_list:
                return city

            # Case-insensitive match
            city_lower = city.lower() if city else ""
            for allowed_city in allowed_list:
                if city_lower == allowed_city.lower():
                    return allowed_city

            return None
        except Exception as e:
            print(f"AI city extraction error: {e}")
            return None

    async def extract_membership(self, user_message: str) -> Optional[str]:
        """
        Extract membership plan from natural language using AI.
        Returns: "lifetime", "monthly", or "onetime"
        """
        try:
            prompt = (
                "You are a membership plan extractor. Identify which subscription plan the user wants.\n\n"
                "VALID OUTPUTS: lifetime, monthly, onetime\n\n"
                "UNDERSTAND CONTEXT: Users may say things like:\n"
                "- Direct: 'lifetime', 'monthly', 'one-time'\n"
                "- Conversational: 'I want lifetime', 'give me monthly', 'the 50k one'\n"
                "- Price-based: '50k', '5k', '2k', '50000', '5000', '2000'\n"
                "- Partial: 'life', 'month', 'once', 'one time'\n"
                "- Preference: 'the first one', 'the cheap one' (onetime), 'the expensive one' (lifetime)\n"
                "- Nigerian style: 'the forever one' (lifetime), 'pay once' (could be lifetime or onetime based on context)\n\n"
                "MAPPING:\n"
                "- lifetime, life, 50k, 50000, 50, forever, permanent → 'lifetime'\n"
                "- monthly, month, 5k, 5000, 5, per month, every month → 'monthly'\n"
                "- onetime, one-time, one time, once, 2k, 2000, 2, single, trial → 'onetime'\n\n"
                "OUTPUT: Return ONLY one of: lifetime, monthly, onetime\n"
                "If you cannot determine the plan, return empty string.\n"
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=10,
                temperature=0,
            )
            choice = completion.choices[0].message.content.strip()
            choice = choice.replace('"', '').replace("'", '').split("\n")[0].strip(",.! ").lower()

            print(f"DEBUG: AI membership extraction - Input: '{user_message}' → Output: '{choice}'")

            if choice in ["lifetime", "monthly", "onetime"]:
                return choice

            return None
        except Exception as e:
            print(f"AI membership extraction error: {e}")
            return None

    async def extract_lagos_area(self, user_message: str) -> Optional[str]:
        """
        Extract Lagos area (Mainland or Island) from natural language.
        Returns: "Lagos Mainland" or "Lagos Island"
        """
        try:
            prompt = (
                "You are determining if a user in Lagos is on the Mainland or Island.\n\n"
                "VALID OUTPUTS: Lagos Mainland, Lagos Island\n\n"
                "UNDERSTAND CONTEXT: Users may say:\n"
                "- Direct: 'mainland', 'island', '1', '2'\n"
                "- Locations: 'Lekki', 'VI', 'Victoria Island', 'Ikoyi' → Island\n"
                "- Locations: 'Ikeja', 'Yaba', 'Surulere', 'Ogba', 'Maryland' → Mainland\n"
                "- Conversational: 'I'm on the mainland', 'island side', 'I stay in Lekki'\n\n"
                "MAPPING:\n"
                "- mainland, main, ikeja, yaba, surulere, ogba, maryland, festac, oshodi → 'Lagos Mainland'\n"
                "- island, vi, victoria island, ikoyi, lekki, ajah, banana island → 'Lagos Island'\n\n"
                "OUTPUT: Return ONLY one of: Lagos Mainland, Lagos Island\n"
                "If you cannot determine, return empty string.\n"
            )
            completion = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=15,
                temperature=0,
            )
            area = completion.choices[0].message.content.strip()
            area = area.replace('"', '').replace("'", '').split("\n")[0].strip(",.! ")

            print(f"DEBUG: AI Lagos area extraction - Input: '{user_message}' → Output: '{area}'")

            if area in ["Lagos Mainland", "Lagos Island"]:
                return area

            # Normalize common variations
            area_lower = area.lower()
            if "mainland" in area_lower:
                return "Lagos Mainland"
            if "island" in area_lower:
                return "Lagos Island"

            return None
        except Exception as e:
            print(f"AI Lagos area extraction error: {e}")
            return None

