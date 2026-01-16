from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

import re
from motor.motor_asyncio import AsyncIOMotorDatabase
from twilio.rest import Client

from app.models.schemas import MessageLog, MessageDirection, Order, OrderItem, OrderStatus
from app.models.broadcast import BroadcastLog, MessageStatusLog
from app.services.ai import AIService
from app.services.paystack import PaystackService
from app.config.settings import Settings
from urllib.parse import urlparse, urlunparse


class WhatsAppService:
    # Content Template SIDs for WhatsApp buttons
    CONTENT_SIDS = {
        "add_to_cart": "HX624af982b4c1b8e300aa567a2f1f16e6",
        "cart_actions": "HX537784f666f3a09e41ffb7aead2fecf4",
        "cart_confirmation": "HXf228f027c53705a3913318f7a95a25f7",
        "product_selection": "HX29817098467929f6250a368a64f3276e",
    }
    
    def __init__(self, db: AsyncIOMotorDatabase, settings: Settings, ai_service: Optional[AIService] = None):
        self.db = db
        self.settings = settings
        # Pass db to AI service if available for dynamic prompt loading
        if ai_service and hasattr(ai_service, 'db'):
            ai_service.db = db
        self.ai_service = ai_service
        self.paystack = PaystackService(settings)
        self.twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    async def upsert_member_state(self, phone: str, updates: Dict[str, Any]):
        await self.db.members.update_one({"phone": phone}, {"$set": updates}, upsert=True)

    async def add_notification(self, type: str, message: str, metadata: Optional[Dict[str, Any]] = None):
        """Log a system notification for the admin dashboard."""
        notification = {
            "type": type,
            "message": message,
            "metadata": metadata or {},
            "ts": datetime.utcnow().timestamp(),
            "is_read": False
        }
        await self.db.notifications.insert_one(notification)

    async def log_message(
        self,
        phone: str,
        direction: MessageDirection,
        body: str,
        intent: str | None = None,
        state_before: str | None = None,
        state_after: str | None = None,
        ai_used: bool = False,
        media_url: str | None = None,
    ):
        log = MessageLog(
            phone=phone,
            direction=direction,
            body=body,
            intent=intent,
            state_before=state_before,
            state_after=state_after,
            ai_used=ai_used,
            media_url=media_url,
        )
        await self.db.messages.insert_one(log.dict())

    def normalize_name(self, text: str) -> str:
        raw = text.strip()
        lowered = raw.lower()
        prefixes = ["my name is", "name is", "i am", "i'm", "call me", "you can call me"]
        for p in prefixes:
            if lowered.startswith(p):
                raw = raw[len(p) :].strip()
                break
        raw = raw.strip(",.! ")
        return raw.title() if raw else text

    def _status_callback(self) -> Optional[str]:
        cb = self.settings.twilio_status_callback_url
        if cb and isinstance(cb, str) and cb.lower() != "none" and cb.startswith("http"):
            return cb
        return None

    def _public_base_url(self) -> Optional[str]:
        if getattr(self.settings, "ngrok_url", None):
            return self.settings.ngrok_url.rstrip("/")
        if self.settings.public_base_url:
            return self.settings.public_base_url.rstrip("/")
        return None

    def _normalize_media_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return url
        base = self._public_base_url()
        if not base:
            return url
        parsed = urlparse(url)
        if "localhost" in parsed.netloc or parsed.hostname in {"127.0.0.1"}:
            new_base = urlparse(base)
            parsed = parsed._replace(scheme=new_base.scheme, netloc=new_base.netloc)
            return urlunparse(parsed)
        return url

    def _get_content_sid_for_buttons(self, button_actions: List[Dict[str, str]]) -> Optional[str]:
        """
        Determine which Content Template to use based on button actions.
        Returns the Content SID if a matching template is found.
        """
        if not button_actions:
            return None
        
        # Extract button texts to identify the template
        button_texts = [action.get("content", "").lower() for action in button_actions]
        button_texts_str = " ".join(button_texts)
        button_set = set(button_texts)
        
        # Match to template based on button combinations (exact matches first)
        # Template: add_to_cart - "Add to Cart", "Checkout", "View Details"
        if {"add to cart", "checkout", "view details"}.issubset(button_set):
            return self.CONTENT_SIDS["add_to_cart"]
        
        # Template: cart_actions - "Checkout", "Add More", "Remove Item"
        if {"checkout", "add more", "remove item"}.issubset(button_set):
            return self.CONTENT_SIDS["cart_actions"]
        
        # Template: cart_confirmation - "View Cart", "Checkout", "Continue Shopping"
        if {"view cart", "checkout", "continue shopping"}.issubset(button_set):
            return self.CONTENT_SIDS["cart_confirmation"]
        
        # Template: product_selection - "Add First", "View Cart", "Search More"
        if {"add first", "view cart", "search more"}.issubset(button_set):
            return self.CONTENT_SIDS["product_selection"]
        
        # Fallback: match by key buttons
        if "add to cart" in button_texts_str and "view details" in button_texts_str:
            return self.CONTENT_SIDS["add_to_cart"]
        elif "add more" in button_texts_str and "remove item" in button_texts_str:
            return self.CONTENT_SIDS["cart_actions"]
        elif "continue shopping" in button_texts_str:
            return self.CONTENT_SIDS["cart_confirmation"]
        elif "add first" in button_texts_str and "search more" in button_texts_str:
            return self.CONTENT_SIDS["product_selection"]
        
        return None

    def _city_key(self, value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = value.lower().replace(" ", "").replace("-", "").replace("_", "")
        # Handle common city variations - be very flexible for PH
        if normalized in ["ph", "portharcourt", "porth", "portharc", "port", "harcourt"]:
            return "ph"
        if normalized in ["lagosmainland", "mainland", "lagosmain"]:
            return "lagosmainland"
        if normalized in ["lagosisland", "island", "lagosisl"]:
            return "lagosisland"
        if normalized.startswith("lagos"):
            return "lagos"
        if normalized in ["abuja", "abj", "fct"]:
            return "abuja"
        return normalized

    def _product_visible_for_city(self, product: Dict[str, Any], member_city: Optional[str]) -> bool:
        clusters = product.get("clusters") or []
        # If product has no city clusters configured, show to everyone
        if not clusters:
            return True
        # If member has no city set, show product (they'll see it during onboarding)
        if not member_city:
            return True
        
        city_key = self._city_key(member_city)
        for c in clusters:
            cluster_city_key = self._city_key(c.get("city"))
            # Exact match
            if cluster_city_key == city_key:
                return True
            # Handle Lagos sub clusters matching Lagos (Mainland/Island both match "lagos")
            if city_key == "lagos" and cluster_city_key.startswith("lagos"):
                return True
            if cluster_city_key == "lagos" and city_key.startswith("lagos"):
                return True
            # Handle PH variations - be more flexible
            if city_key == "ph" and (cluster_city_key == "ph" or "ph" in cluster_city_key):
                return True
            if cluster_city_key == "ph" and (city_key == "ph" or "ph" in city_key):
                return True
        # If product has clusters configured but no match found, don't show it
        # This respects the city configuration in the product
        return False

    def _is_valid_payment_ref(self, text: str) -> bool:
        """
        Guard against casual greetings being stored as payment refs.
        Accept only if it has a digit and is at least 5 chars.
        """
        if not text:
            return False
        lowered = text.strip().lower()
        banned = {"hi", "hello", "hey", "yes", "ok", "okay", "paid", "pay"}
        if lowered in banned:
            return False
        if len(text.strip()) < 5:
            return False
        return any(ch.isdigit() for ch in text)

    def _looks_like_phone(self, value: Optional[str]) -> bool:
        if not value:
            return False
        digits = "".join([c for c in value if c.isdigit()])
        return len(digits) >= 7

    def _split_amount_evenly(self, total_kobo: int, members: List[str]) -> List[Tuple[str, int]]:
        """
        Split an amount across members, distributing remainder to earliest members.
        Keeps sum of splits equal to total.
        """
        if not members or total_kobo <= 0:
            return []
        count = len(members)
        base = total_kobo // count
        remainder = total_kobo % count
        splits: List[Tuple[str, int]] = []
        for idx, m in enumerate(members):
            share = base + (1 if idx < remainder else 0)
            splits.append((m, share))
        return splits

    def _slug_prefix(self, city: Optional[str]) -> str:
        if not city:
            return "GEN"
        key = city.lower()
        if "lagos" in key:
            return "LAG"
        if "abuja" in key:
            return "ABJ"
        if "ph" in key or "harcourt" in key:
            return "PH"
        return "GEN"

    async def send_outbound(self, phone: str, body: str, media_url: Optional[str] = None) -> str:
        """
        Send a single WhatsApp message to a phone number (without status callback).
        """
        to_phone = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        params = {
            "from_": self.settings.twilio_from_number,
            "to": to_phone,
            "body": body,
        }
        if media_url:
            params["media_url"] = [media_url]
        cb = self._status_callback()
        if cb:
            params["status_callback"] = cb
        resp = self.twilio.messages.create(**params)
        # Log outbound
        await self.log_message(
            phone=phone.replace("whatsapp:", ""),
            direction=MessageDirection.outbound,
            body=body,
            intent="admin_send",
            state_before=None,
            state_after="idle",
            media_url=media_url,
        )
        return resp.sid

    async def send_content_template(self, phone: str, content_sid: str, content_variables: Optional[Dict[str, str]] = None) -> str:
        """
        Send a WhatsApp Content Template with buttons via REST API.
        """
        to_phone = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        params = {
            "from_": self.settings.twilio_from_number,
            "to": to_phone,
            "content_sid": content_sid,
        }
        if content_variables:
            import json
            params["content_variables"] = json.dumps(content_variables)
        cb = self._status_callback()
        if cb:
            params["status_callback"] = cb
        resp = self.twilio.messages.create(**params)
        # Log outbound
        await self.log_message(
            phone=phone.replace("whatsapp:", ""),
            direction=MessageDirection.outbound,
            body=f"[Content Template: {content_sid}]",
            intent="template_send",
            state_before=None,
            state_after="idle",
        )
        return resp.sid

    async def send_catalog_cards(self, phone: str, products: List[dict], limit: int = 3):
        """
        Send a few rich catalog cards (image + caption) so users see products visually on WhatsApp.
        """
        to_phone = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        for prod in products[:limit]:
            img = self._normalize_media_url(prod.get("image_url"))
            caption = prod.get("name", "Product")
            price = prod.get("price")
            sku = prod.get("sku", "")
            body_parts = [caption]
            if price:
                body_parts.append(f"₦{price}")
            if sku:
                body_parts.append(f"SKU: {sku}")
            body = " • ".join(body_parts)
            params = {
                "from_": self.settings.twilio_from_number,
                "to": to_phone,
                "body": body,
            }
            if img:
                params["media_url"] = [img]
            cb = self._status_callback()
            if cb:
                params["status_callback"] = cb
            try:
                resp = self.twilio.messages.create(**params)
                await self.log_message(
                    phone=phone.replace("whatsapp:", ""),
                    direction=MessageDirection.outbound,
                    body=body,
                    intent="catalogue_card",
                    state_after="idle",
                    media_url=img,
                )
            except Exception:
                # Best-effort; skip failures
                continue

    async def broadcast_all_conversed(self, body: str, media_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Broadcast to all unique phone numbers that have messaged with the bot (from messages collection).
        Fixed to handle images properly by sending media_url as a list when present.
        """
        phones = await self.db.messages.distinct("phone")
        sent = 0
        errors = 0
        sids: List[str] = []
        for phone in phones:
            to_phone = phone if str(phone).startswith("whatsapp:") else f"whatsapp:{phone}"
            try:
                params = {
                    "from_": self.settings.twilio_from_number,
                    "to": to_phone,
                }
                # Always set body first
                if body:
                    params["body"] = body
                
                # Add media if provided - Twilio requires media_url as a list
                if media_url:
                    # Normalize the media URL
                    normalized_media = self._normalize_media_url(media_url)
                    params["media_url"] = [normalized_media] if normalized_media else []
                
                cb = self._status_callback()
                if cb:
                    params["status_callback"] = cb
                
                resp = self.twilio.messages.create(**params)
                sids.append(resp.sid)
                sent += 1
                await self.log_message(
                    phone=str(phone),
                    direction=MessageDirection.outbound,
                    body=body or ("[media]" if media_url else ""),
                    intent="admin_broadcast_all",
                    state_after="idle",
                    media_url=media_url,
                )
            except Exception as e:
                print(f"Broadcast error for {phone}: {e}")
                errors += 1
                continue

        log = BroadcastLog(
            city="all_conversed",
            message=body,
            template_sid=None,
            sent_count=sent,
            error_count=errors,
            message_sids=sids,
        )
        await self.db.broadcasts.insert_one(log.dict())
        return {"sent": sent, "errors": errors, "count": len(phones)}

    async def get_member(self, phone: str) -> Dict[str, Any]:
        return await self.db.members.find_one({"phone": phone}) or {}
    
    async def get_custom_cluster(self, cluster_id: str) -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try:
            return await self.db.custom_clusters.find_one({"_id": ObjectId(cluster_id)})
        except:
            return None

    async def save_custom_cluster(self, cluster: Dict[str, Any]):
        if "_id" in cluster:
            oid = cluster["_id"]
            data = {k: v for k, v in cluster.items() if k != "_id"}
            await self.db.custom_clusters.update_one({"_id": oid}, {"$set": data}, upsert=True)
        else:
            await self.db.custom_clusters.insert_one(cluster)

    async def get_user_clusters(self, phone: str) -> List[Dict[str, Any]]:
        cursor = self.db.custom_clusters.find({
            "$or": [
                {"owner_phone": phone},
                {"members": phone}
            ],
            "is_active": True
        })
        return await cursor.to_list(length=20)

    async def get_cart(self, phone: str, force_personal: bool = False) -> Dict[str, Any]:
        member = await self.get_member(phone)
        cluster_id = member.get("current_cluster_id")
        
        if cluster_id and not force_personal:
            cluster = await self.get_custom_cluster(cluster_id)
            if cluster:
                return {
                    "phone": phone, 
                    "cluster_id": cluster_id, 
                    "cluster_name": cluster.get("name"),
                    "items": cluster.get("items") or [], 
                    "updated_at": cluster.get("created_at")
                }
        
        cart = await self.db.carts.find_one({"phone": phone}) or {"phone": phone, "items": [], "updated_at": datetime.utcnow()}
        return cart

    async def save_cart(self, cart: Dict[str, Any], force_personal: bool = False):
        cluster_id = cart.get("cluster_id")
        if cluster_id and not force_personal:
            cluster = await self.get_custom_cluster(cluster_id)
            if cluster:
                cluster["items"] = cart["items"]
                await self.save_custom_cluster(cluster)
                return

        cart["updated_at"] = datetime.utcnow()
        await self.db.carts.update_one({"phone": cart["phone"]}, {"$set": cart}, upsert=True)

    async def get_price_sheet_url(self) -> Optional[str]:
        cfg = await self.db.config.find_one({"_id": "price_sheet"}) or {}
        return cfg.get("url") or self.settings.price_sheet_url

    async def search_products(self, query: str, member_city: Optional[str]) -> List[Dict[str, Any]]:
        # Use full query for search - let MongoDB regex handle matching intelligently
        # No keyword filtering - trust AI to extract meaningful queries
        query_clean = query.strip() if query else ""
        
        if not query_clean:
            # Broad search (featured) - show all available products
            criteria = {"$or": [{"in_stock": True}, {"in_stock": {"$exists": False}}]}
        else:
            # Use full query as regex pattern for flexible matching
            # This allows natural language queries to work better
            regex = {"$regex": query_clean, "$options": "i"}
            criteria = {
                "$and": [
                    {"$or": [{"in_stock": True}, {"in_stock": {"$exists": False}}]},
                    {"$or": [{"name": regex}, {"sku": regex}]}
                ]
            }

        products = await self.db.products.find(criteria).sort("name", 1).to_list(length=50)
        
        # Filter products by city visibility - only show products that match user's city
        # If product has no city clusters configured, show to everyone
        # If product has city clusters configured, only show if user's city matches
        filtered_products = []
        for p in products:
            if self._product_visible_for_city(p, member_city):
                filtered_products.append(p)
        
        return filtered_products
    
    async def get_product_categories(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get products grouped by category based on common keywords."""
        all_products = await self.db.products.find({
            "$or": [{"in_stock": True}, {"in_stock": {"$exists": False}}]
        }).to_list(length=1000)
        
        categories = {
            "rice": [],
            "oil": [],
            "fish": [],
            "meat": [],
            "poultry": [],
            "vegetables": [],
            "household": [],
            "other": []
        }
        
        category_keywords = {
            "rice": ["rice", "ofada", "basmati", "local"],
            "oil": ["oil", "vegetable", "palm", "groundnut"],
            "fish": ["fish", "tilapia", "mackerel", "titus"],
            "meat": ["meat", "beef", "goat", "mutton"],
            "poultry": ["chicken", "turkey", "duck", "egg"],
            "vegetables": ["tomato", "onion", "pepper", "potato", "vegetable"],
            "household": ["detergent", "soap", "tissue", "toilet", "household"]
        }
        
        for product in all_products:
            name_lower = (product.get("name") or "").lower()
            sku_lower = (product.get("sku") or "").lower()
            text = f"{name_lower} {sku_lower}"
            
            categorized = False
            for cat, keywords in category_keywords.items():
                if any(kw in text for kw in keywords):
                    categories[cat].append(product)
                    categorized = True
                    break
            
            if not categorized:
                categories["other"].append(product)
        
        return categories

    async def set_price_sheet_url(self, url: str):
        await self.db.config.update_one(
            {"_id": "price_sheet"},
            {"$set": {"url": url}},
            upsert=True,
        )

    async def create_order_from_text(self, phone: str, text: str) -> str:
        member = await self.get_member(phone)
        items = self.parse_order_text(text)
        city = member.get("city")
        prefix = self._slug_prefix(city)
        count = await self.db.orders.count_documents({"city": city}) + 1
        slug = f"{prefix}-{count:03d}"
        order = Order(
            member_phone=phone,
            raw_text=text,
            items=items,
            total=None,
            city=city,
            slug=slug,
            status="WAITING_PAYMENT",
        )
        result = await self.db.orders.insert_one(order.dict())
        return str(result.inserted_id)

    async def create_order_from_cart(self, phone: str) -> Tuple[Optional[str], float]:
        member = await self.get_member(phone)
        cluster_id = member.get("current_cluster_id")
        cluster = None
        cluster_members: List[str] = []
        cluster_owner_phone: Optional[str] = None
        cluster_name: Optional[str] = None
        
        if cluster_id:
            cluster = await self.get_custom_cluster(cluster_id)
            if cluster:
                cluster_members = cluster.get("members", [])
                cluster_owner_phone = cluster.get("owner_phone")
                cluster_name = cluster.get("name")
                if phone not in cluster_members:
                    cluster_members.append(phone)
            if cluster and cluster.get("owner_phone") != phone:
                # Return a special flag or handle restriction in handle_inbound
                return "RESTRICTED", 0.0

        cart = await self.get_cart(phone)
        items_data = cart.get("items") or []
        if not items_data:
            return None, 0.0
        
        items: List[OrderItem] = []
        subtotal = 0.0
        for it in items_data:
            qty = int(it.get("qty") or 1)
            price_raw = it.get("price")
            # Clean price string to float
            price_val = 0.0
            if price_raw:
                try:
                    price_val = float(str(price_raw).replace(",", "").replace("₦", "").strip())
                except ValueError:
                    pass
            subtotal += price_val * qty
            items.append(OrderItem(sku=it.get("name") or it.get("sku") or "Item", qty=qty))
            
        delivery_fee = 4500.0
        total = subtotal + delivery_fee
        
        city = member.get("city")
        prefix = self._slug_prefix(city)
        count = await self.db.orders.count_documents({"city": city}) + 1
        slug = f"{prefix}-{count:03d}"
        
        order = Order(
            member_phone=phone,
            items=items,
            raw_text=None,
            total=total,
            city=city,
            address=member.get("address"),
            slug=slug,
            status=OrderStatus.waiting_payment,
            cluster_id=cluster_id,
            cluster_name=cluster_name or cart.get("cluster_name"),
            cluster_owner_phone=cluster_owner_phone,
            cluster_members=cluster_members,
        )
        if cluster_id:
            order.raw_text = f"Custom Cluster Order: {cluster_name or cart.get('cluster_name')}"

        result = await self.db.orders.insert_one(order.dict())
        order_id = str(result.inserted_id)
        
        # NOTIFICATION: New Order
        order_meta = {"order_id": order_id, "phone": phone, "total": total, "slug": slug}
        if cluster_id:
            order_meta.update(
                {
                    "cluster_id": cluster_id,
                    "cluster_name": cluster_name or cart.get("cluster_name"),
                }
            )
        await self.add_notification(
            type="order",
            message=f"New order from {phone} (₦{total:,.0f})",
            metadata=order_meta,
        )

        # clear cart
        if cluster_id:
            target_cluster = cluster or await self.get_custom_cluster(cluster_id)
            if target_cluster:
                target_cluster["items"] = []
                await self.save_custom_cluster(target_cluster)
        else:
            await self.save_cart({"phone": phone, "items": [], "updated_at": datetime.utcnow()})
            
        return (slug, total)

    async def initiate_cluster_payment_links(self, order_slug: str, total_val: float, cluster: Dict[str, Any], owner: Dict[str, Any]) -> str:
        """
        Generate Paystack links for each cluster member and push them via WhatsApp.
        Returns a summary message for the owner.
        """
        cluster_name = cluster.get("name") or "Cluster"
        owner_phone = cluster.get("owner_phone")
        owner_address = (owner or {}).get("address")
        
        members = cluster.get("members") or []
        if owner_phone and owner_phone not in members:
            members = [owner_phone] + members
        if not members and owner_phone:
            members = [owner_phone]
        
        # De-duplicate while preserving order
        clean_members: List[str] = []
        seen = set()
        for m in members:
            if not m or m in seen:
                continue
            clean_members.append(m)
            seen.add(m)
            
        total_kobo = int(total_val * 100)
        splits = self._split_amount_evenly(total_kobo, clean_members)
        
        payments_payload: List[Dict[str, Any]] = []
        failures: List[str] = []
        for phone, share in splits:
            metadata = {
                "type": "cluster_order",
                "order_slug": order_slug,
                "phone": phone,
                "cluster_id": str(cluster.get("_id") or cluster.get("id") or cluster.get("cluster_id") or ""),
                "cluster_name": cluster_name,
                "owner_phone": owner_phone,
                "share_kobo": share,
                "total_kobo": total_kobo,
            }
            
            pay_link = None
            try:
                pay_resp = await self.paystack.initialize_transaction(
                    email=f"{phone}@pnplite.ng",
                    amount_kobo=share,
                    metadata=metadata
                )
                pay_link = pay_resp.get("authorization_url") if pay_resp else None
            except Exception as e:
                print(f"Paystack link generation failed for {phone}: {e}")
                pay_link = None
            
            payments_payload.append(
                {
                    "phone": phone,
                    "amount_kobo": share,
                    "status": "pending" if pay_link else "error",
                    "pay_link": pay_link,
                }
            )

            if pay_link:
                try:
                    msg_lines = [
                        f"Cluster checkout for *{cluster_name}* (Order *{order_slug}*).",
                        f"Please pay your share of *₦{share/100:,.0f}* here: {pay_link}",
                    ]
                    if owner_address:
                        msg_lines.append(f"Delivery address on file: {owner_address}")
                    else:
                        msg_lines.append("We still need a delivery address. Please reply here with the correct address.")
                    msg_lines.append("We'll let the owner know once you pay.")
                    await self.send_outbound(phone, "\n".join(msg_lines))
                except Exception as e:
                    print(f"Failed to send cluster pay link to {phone}: {e}")
            else:
                failures.append(phone)
        
        await self.db.orders.update_one(
            {"slug": order_slug},
            {
                "$set": {
                    "cluster_payments": payments_payload,
                    "cluster_members": clean_members,
                    "cluster_owner_phone": owner_phone,
                    "cluster_name": cluster_name,
                    "cluster_paid_amount_kobo": 0,
                }
            },
        )

        lines = [
            f"Order *{order_slug}* created for cluster *{cluster_name}*.",
            f"Total: ₦{total_val:,.0f}. Split into {len(clean_members)} payment link(s).",
        ]
        if failures:
            lines.append(f"⚠️ Could not generate links for: {', '.join(failures)}. You may need to retry manually.")
        else:
            lines.append("Payment links have been sent to all cluster members. You'll get updates as people pay.")
        if not owner_address:
            lines.append("Reminder: we still need a delivery address. Reply with it here if this is your order.")
        return "\n".join(lines)

    def parse_order_text(self, text: str) -> List[OrderItem]:
        """
        Lightweight parser: splits on commas; matches "<sku words> <size?> x<qty?>"
        Defaults qty to 1 if missing.
        """
        items: List[OrderItem] = []
        parts = [p.strip() for p in text.split(",") if p.strip()]
        pattern = re.compile(r"(?P<sku>[A-Za-z0-9 ]+?)(?:\s+(?P<size>[0-9]+[A-Za-z]+))?\s*(?:x(?P<qty>[0-9]+))?$")
        for part in parts:
            m = pattern.match(part)
            if not m:
                continue
            sku = m.group("sku").strip()
            size = m.group("size") or ""
            qty = int(m.group("qty") or 1)
            label = f"{sku} {size}".strip()
            items.append(OrderItem(sku=label, qty=qty))
        return items

    async def add_item_to_cart(self, phone: str, product: Dict[str, Any], qty: int = 1, force_personal: bool = False):
        cart = await self.get_cart(phone, force_personal=force_personal)
        items = cart.get("items", [])
        # de-dupe by sku
        updated = False
        for it in items:
            if it.get("sku") == product.get("sku"):
                it["qty"] = it.get("qty", 1) + qty
                updated = True
                break
        if not updated:
            items.append(
                {
                    "sku": product.get("sku"),
                    "name": product.get("name"),
                    "qty": qty,
                    "price": product.get("price"),
                }
            )
        cart["items"] = items
        await self.save_cart(cart, force_personal=force_personal)
    
    async def remove_item_from_cart(self, phone: str, item_query: str, force_personal: bool = False):
        cart = await self.get_cart(phone, force_personal=force_personal)
        items = cart.get("items", [])
        if not items:
            return
            
        # Use full query for matching - no keyword filtering
        # Let the full query match against item names for better accuracy
        query_lower = item_query.lower().strip()

        new_items = []
        removed = False
        
        for it in items:
            name_lower = (it.get("name") or "").lower()
            # Check if query is contained in item name (flexible matching)
            match = query_lower in name_lower or name_lower in query_lower
            
            if match and not removed:
                removed = True
                continue
            new_items.append(it)
            
        if removed:
            cart["items"] = new_items
            await self.save_cart(cart, force_personal=force_personal)
        return removed

    def render_cart_summary(self, cart: Dict[str, Any], with_instructions: bool = True) -> str:
        items = cart.get("items") or []
        cluster_name = cart.get("cluster_name")
        
        if not items:
            return f"Your {'cluster ' if cluster_name else ''}cart is empty."
        
        title = f"*Shared Cluster Cart: {cluster_name}*" if cluster_name else "*Your Cart:*"
        lines = [title]
        subtotal = 0.0
        
        for it in items:
            name = it.get("name") or it.get("sku")
            qty = it.get("qty") or 1
            price_raw = it.get("price")
            price_str = "N/A"
            if price_raw:
                try:
                    # Clean and calc
                    p_val = float(str(price_raw).replace(",", "").replace("₦", "").strip())
                    row_total = p_val * qty
                    subtotal += row_total
                    price_str = f"₦{row_total:,.0f}"
                except:
                    price_str = str(price_raw)
            
            lines.append(f"• {name} x{qty} — {price_str}")
            
        delivery = 4500
        total = subtotal + delivery
        
        lines.append(f"\nSubtotal: ₦{subtotal:,.0f}")
        lines.append(f"Delivery Fee: ₦{delivery:,.0f}")
        lines.append(f"*Total: ₦{total:,.0f}*")
        
        if with_instructions:
            lines.append("\nReply CHECKOUT to place the order, or tell me what to add/remove.")
            
        return "\n".join(lines)

    async def _download_media(self, media_url: str) -> Optional[str]:
        """
        Download media from Twilio (or any URL) to local uploads/ folder
        and return the local public URL.
        """
        if not media_url:
            return None
            
        try:
            import aiohttp
            import shutil
            import uuid
            from pathlib import Path
            
            # Use basic auth if it's a Twilio URL
            auth = None
            if "twilio.com" in media_url:
                auth = aiohttp.BasicAuth(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
            
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.get(media_url) as resp:
                    if resp.status == 200:
                        ext = "jpg" # default
                        ct = resp.headers.get("Content-Type", "")
                        if "png" in ct: ext = "png"
                        elif "pdf" in ct: ext = "pdf"
                        elif "jpeg" in ct: ext = "jpg"
                        
                        fname = f"proof_{uuid.uuid4().hex}.{ext}"
                        upload_dir = Path("uploads")
                        upload_dir.mkdir(exist_ok=True)
                        dest = upload_dir / fname
                        
                        content = await resp.read()
                        dest.write_bytes(content)
                        
                        base = self._public_base_url()
                        if base:
                            return f"{base}/uploads/{fname}"
                        return f"/uploads/{fname}"
            return media_url
        except Exception as e:
            print(f"Media download failed: {e}")
            return media_url

    async def apply_payment_proof(self, phone: str, ref: str) -> str:
        # Download if it looks like a URL
        final_ref = ref
        if ref.startswith("http"):
            local_url = await self._download_media(ref)
            if local_url:
                final_ref = local_url

        # Tag latest order as awaiting review
        latest = await self.db.orders.find_one({"member_phone": phone}, sort=[("created_at", -1)])
        if latest:
            order_slug = latest.get("slug") or str(latest["_id"])
            await self.db.orders.update_one(
                {"_id": latest["_id"]},
                {"$set": {"payment_ref": final_ref, "status": "PAID"}},
            )
            # NOTIFICATION: Payment Proof
            await self.add_notification(
                type="payment",
                message=f"Payment proof received from {phone}" + (f" for {order_slug}" if order_slug else ""),
                metadata={"phone": phone, "order_slug": order_slug, "ref": final_ref}
            )

            return f"Payment proof received for order {order_slug}. We'll confirm shortly."
        await self.upsert_member_state(
            phone,
            {"payment_ref": final_ref, "payment_status": "pending_review", "state": "idle"},
        )
        # NOTIFICATION: Payment Proof (for member without recent order)
        await self.add_notification(
            type="payment",
            message=f"Payment proof received from {phone} (no recent order)",
            metadata={"phone": phone, "ref": final_ref}
        )
        return "Payment proof received. We'll confirm shortly."

    async def award_referral_commission(self, order: Dict[str, Any]):
        """
        Award a one-time commission (2% of the first paid order) to the referrer, identified by phone.
        Only pays if the referrer is a paid subscriber.
        """
        member_phone = order.get("member_phone")
        order_slug = order.get("slug")
        total = order.get("total")
        if not member_phone or not order_slug or not total:
            return
        
        member = await self.get_member(member_phone)
        if not member:
            return
        referrer_phone = member.get("referred_by")
        if not self._looks_like_phone(referrer_phone):
            return
        referrer = await self.get_member(referrer_phone)
        if not referrer or referrer.get("payment_status") != "paid":
            return
        
        # Only the first paid order counts
        paid_count = await self.db.orders.count_documents({"member_phone": member_phone, "status": "PAID"})
        if paid_count > 1:
            return
        
        # Avoid duplicate commission for same order
        existing = await self.db.commissions.find_one({"referred_phone": member_phone, "order_slug": order_slug})
        if existing:
            return
        
        amount = float(total or 0) * 0.02
        commission = {
            "referrer_phone": referrer_phone,
            "referred_phone": member_phone,
            "order_slug": order_slug,
            "amount": amount,
            "status": "pending",
            "created_at": datetime.utcnow(),
        }
        await self.db.commissions.insert_one(commission)
        
        await self.add_notification(
            type="commission",
            message=f"Commission ₦{amount:,.0f} for referrer {referrer_phone}",
            metadata={
                "order_slug": order_slug,
                "referrer": referrer_phone,
                "referred": member_phone,
                "amount": f"₦{amount:,.0f}"
            }
        )
        
        ref_name = member.get("name") or member_phone
        try:
            await self.send_outbound(
                referrer_phone,
                f"🎉 You earned ₦{amount:,.0f} commission from {ref_name}'s first order ({order_slug}). We'll pay into your bank shortly."
            )
        except Exception as e:
            print(f"Failed to notify referrer {referrer_phone}: {e}")

    async def broadcast_message(self, city: str, message: str) -> str:
        query = {}
        if city.lower() != "all":
            query["city"] = {"$regex": f"^{city}$", "$options": "i"}
        # Send only to paid members to reduce noise
        query["payment_status"] = "paid"
        cursor = self.db.members.find(query, {"phone": 1})
        recipients = [m async for m in cursor]
        sent = 0
        errors = 0
        sids: List[str] = []
        for rec in recipients:
            to_phone = f"whatsapp:{rec['phone']}"
            try:
                params = {
                    "from_": self.settings.twilio_from_number,
                    "to": to_phone,
                }
                if self.settings.twilio_template_sid_broadcast:
                    params["content_sid"] = self.settings.twilio_template_sid_broadcast
                    params["content_variables"] = '{"1":"' + message.replace('"', '\\"') + '"}'
                else:
                    params["body"] = message
                cb = self._status_callback()
                if cb:
                    params["status_callback"] = cb

                resp = self.twilio.messages.create(**params)
                sids.append(resp.sid)
                sent += 1
            except Exception:
                errors += 1
                continue

        log = BroadcastLog(
            city=city,
            message=message,
            template_sid=self.settings.twilio_template_sid_broadcast,
            sent_count=sent,
            error_count=errors,
            message_sids=sids,
        )
        await self.db.broadcasts.insert_one(log.dict())
        return f"Broadcast queued to {sent} recipients in {city}. Errors: {errors}"

    def is_admin(self, phone: str) -> bool:
        return phone in self.settings.admin_numbers

    async def handle_admin_command(self, phone: str, body: str) -> Tuple[str, str]:
        parts = body.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/set_price_sheet":
            if not arg:
                return ("Usage: /set_price_sheet <url>", "idle")
            await self.set_price_sheet_url(arg)
            return ("Price sheet updated.", "idle")

        if cmd == "/orders":
            pipeline = [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
            agg = await self.db.orders.aggregate(pipeline).to_list(length=None)
            summary = ", ".join([f"{d['_id']}: {d['count']}" for d in agg]) or "no orders yet"
            return (f"Order summary: {summary}", "idle")

        if cmd == "/members":
            total = await self.db.members.count_documents({})
            paid = await self.db.members.count_documents({"payment_status": "paid"})
            return (f"Members: total={total}, paid={paid}", "idle")

        if cmd == "/mark_paid":
            target = arg or phone
            await self.upsert_member_state(target, {"payment_status": "paid", "state": "idle"})
            return (f"Marked {target} as paid.", "idle")

        if cmd == "/broadcast":
            # Expected format: /broadcast <city|all> <message>
            args = arg.split(maxsplit=1)
            if len(args) < 2:
                return ("Usage: /broadcast <city|all> <message>", "idle")
            city, msg = args[0], args[1]
            result = await self.broadcast_message(city, msg)
            return (result, "idle")

        return ("Unknown admin command.", "idle")

    async def handle_inbound(
        self, phone: str, body: str, media_url: Optional[str] = None
    ) -> Tuple[str, str, str | None, str | None, bool, List[Dict[str, str]]]:
        """
        Returns: (reply_text, next_state, state_before, intent, ai_used, button_actions)
        button_actions: List of dicts with 'action' and 'content' keys for WhatsApp buttons
        """
        body_clean = body.strip()
        intent = None
        ai_used = False
        button_actions: List[Dict[str, str]] = []  # Default: no buttons

        # Admin commands
        if self.is_admin(phone) and body_clean.startswith("/"):
            reply, next_state = await self.handle_admin_command(phone, body_clean)
            return (reply, next_state, None, "admin_command", ai_used, button_actions)

        member = await self.get_member(phone)
        state_before = member.get("state")

        # New user onboarding with friendly intro
        if not member:
            # New Member logic
            member = {
                "phone": phone,
                "join_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "state": "idle",
                "payment_status": "unpaid",
            }
            await self.db.members.insert_one(member)
            
            # NOTIFICATION: New Member
            await self.add_notification(
                type="member",
                message=f"New member joined: {phone}",
                metadata={"phone": phone}
            )
            
            await self.upsert_member_state(phone, {"phone": phone, "state": "awaiting_name"})
            intro_message = (
                "Welcome to PNP Lite! 🎉\n\n"
                "I'm your WhatsApp shopping assistant. PNP Lite is a group-buying community that gives you access to wholesale prices through coordinated bulk purchasing.\n\n"
                "*How it works:*\n"
                "• Shop together with friends in clusters to unlock wholesale prices\n"
                "• Enjoy doorstep delivery\n"
                "• Get amazing discounts on groceries and essentials\n"
                "• Zero stress, no haggling needed\n\n"
                "To get started, I'll need a few details. What should I call you?"
            )
            return (
                intro_message,
                "awaiting_name",
                state_before,
                "onboard",
                ai_used,
                button_actions,
            )

        state = member.get("state")
        lower = body_clean.lower()

        # Proactive Cluster Switching
        if state == "idle" and "JOIN_CLUSTER_" not in body_clean:
            user_clusters = await self.get_user_clusters(phone)
            for uc in user_clusters:
                c_name = uc["name"].lower()
                # If the message contains the cluster name specifically (not just part of another word)
                if re.search(rf"\b{re.escape(c_name)}\b", lower):
                    current_cid = member.get("current_cluster_id")
                    if current_cid != str(uc["_id"]):
                        await self.upsert_member_state(phone, {"current_cluster_id": str(uc["_id"])})
                        # We don't return here, we just switch context and let the intent handling continue
                        # but we can optionally add a note or just let the cart view reflect it.
                        pass

        # Referral handling
        if "referred by" in lower:
            ref_name = body_clean.split("referred by", 1)[1].strip().strip(".")
            await self.db.members.update_one({"phone": phone}, {"$set": {"referred_by": ref_name}})

        # Custom Cluster States
        if state == "awaiting_cluster_name":
            await self.upsert_member_state(phone, {"state": "awaiting_cluster_limit", "temp_cluster_name": body_clean})
            return (
                f"Got it! '{body_clean}'. Now, what is the maximum number of people allowed in this cluster? (e.g. 5)",
                "awaiting_cluster_limit",
                state_before,
                "cluster_limit",
                ai_used,
            )

        if state == "awaiting_cluster_limit":
            try:
                limit = int(re.search(r"\d+", body_clean).group())
            except:
                limit = 5
            
            cluster_name = member.get("temp_cluster_name") or "My Cluster"
            cluster_data = {
                "name": cluster_name,
                "owner_phone": phone,
                "max_people": limit,
                "members": [phone],
                "items": [],
                "created_at": datetime.utcnow(),
                "is_active": True
            }
            result = await self.db.custom_clusters.insert_one(cluster_data)
            cluster_id = str(result.inserted_id)
            
            await self.upsert_member_state(phone, {
                "state": "idle", 
                "current_cluster_id": cluster_id,
                "temp_cluster_name": None
            })
            
            bot_num = self.settings.twilio_from_number.replace("whatsapp:", "").replace("+", "")
            join_link = f"https://wa.me/{bot_num}?text=JOIN_CLUSTER_{cluster_id}"
            
            return (
                f"✅ Cluster '{cluster_name}' created with a limit of {limit} people!\n\n"
                f"Share this link with your friends to join: {join_link}\n\n"
                "Anyone in the cluster can add items to the shared cart, but only you can checkout.",
                "idle",
                state_before,
                "cluster_created",
                ai_used,
            )

        # Handle onboarding states
        if state == "awaiting_name":
            name = self.normalize_name(body_clean)
            if self.ai_service:
                extracted = await self.ai_service.extract_name(body_clean)
                if extracted:
                    name = extracted
                    ai_used = True
            await self.upsert_member_state(phone, {"name": name, "state": "awaiting_city"})
            return (
                f"Thanks, {name}! Which city are you in? (PH / Lagos / Abuja)",
                "awaiting_city",
                state_before,
                "city",
                ai_used,
            )

        if state == "awaiting_city":
            city_value = None
            # Use AI for city extraction
            if self.ai_service:
                try:
                    extracted_city = await self.ai_service.extract_city(body_clean, allowed=["PH", "Port Harcourt", "Lagos Mainland", "Lagos Island", "Abuja"])
                    if extracted_city:
                        city_value = extracted_city
                        ai_used = True
                    # Special handling: if user says "Lagos" and AI returns empty, default to Lagos Mainland
                    elif body_clean.lower().strip() in ["lagos", "lag"]:
                        city_value = "Lagos Mainland"
                        ai_used = True
                except Exception as e:
                    print(f"AI city extraction error: {e}")
            
            # If AI extraction fails, try simple fallback matching
            if not city_value:
                body_lower = body_clean.lower().strip()
                # Simple fallback matching for common inputs
                if body_lower in ["ph", "port harcourt", "portharcourt", "harcourt"]:
                    city_value = "PH"
                elif body_lower in ["lagos", "lag"]:
                    city_value = "Lagos Mainland"  # Default to Mainland
                elif body_lower in ["abuja", "abj"]:
                    city_value = "Abuja"
                elif "mainland" in body_lower and "lagos" in body_lower:
                    city_value = "Lagos Mainland"
                elif "island" in body_lower and "lagos" in body_lower:
                    city_value = "Lagos Island"
            
            # If still no city value, ask user to clarify
            if not city_value:
                return (
                    "I didn't catch that. Which city are you in? Please reply with: PH, Lagos, or Abuja",
                    "awaiting_city",
                    state_before,
                    "city",
                    ai_used,
                )

            await self.upsert_member_state(phone, {"city": city_value, "state": "awaiting_membership"})
            friendly_name = member.get("name") or ""
            prefix = f"Great, {friendly_name}! " if friendly_name else "Great! "
            membership_explanation = (
                f"{prefix}Now, let's set up your subscription:\n\n"
                "*Subscription Plans:*\n"
                "• *Lifetime* - ₦50,000 (One-time payment, lifetime access)\n"
                "• *Monthly* - ₦5,000 (Renewable monthly subscription)\n"
                "• *One-time* - ₦2,000 (Single purchase access)\n\n"
                "All plans give you access to:\n"
                "✓ Wholesale pricing through group-buying\n"
                "✓ Priority delivery\n"
                "✓ Referral bonuses (₦1,000 per referral)\n"
                "✓ Access to exclusive deals and seasonal bundles\n\n"
                "Which plan works for you? (Reply: Lifetime / Monthly / One-time)"
            )
            return (
                membership_explanation,
                "awaiting_membership",
                state_before,
                "membership",
                ai_used,
            )

        if state == "awaiting_membership":
            membership = None
            # Use AI for membership extraction
            if self.ai_service:
                try:
                    extracted_membership = await self.ai_service.extract_membership(body_clean)
                    if extracted_membership:
                        membership = extracted_membership
                        ai_used = True
                except Exception as e:
                    print(f"AI membership extraction error: {e}")
            
            # If AI extraction fails, ask user to clarify
            if not membership:
                return (
                    "I can set you up with Lifetime (₦50k), Monthly (₦5k), or One-time (₦2k). Which do you want?",
                    "awaiting_membership",
                    state_before,
                    "membership",
                    ai_used,
                )
            await self.upsert_member_state(
                phone,
                {"membership_type": membership, "state": "awaiting_payment_proof", "payment_status": "pending_review"},
            )
            
            # Initialize Paystack Transaction
            amounts = {"lifetime": 5000000, "monthly": 500000, "onetime": 200000}
            amount = amounts.get(membership, 200000)
            
            metadata = {
                "type": "membership",
                "phone": phone,
                "membership_type": membership
            }
            
            pay_link = await self.paystack.initialize_transaction(
                email=f"{phone}@pnplite.ng", # Virtual email for Paystack
                amount_kobo=amount,
                metadata=metadata
            )

            if pay_link and pay_link.get("authorization_url"):
                url = pay_link["authorization_url"]
                # Update state to idle in database since payment is automated
                await self.upsert_member_state(phone, {"state": "idle"})
                return (
                    f"Great choice! Please use this link to complete your {membership} membership payment: {url}\n\n"
                    "Once paid, your account will be activated automatically!",
                    "idle", # Direct to idle as payment is automated
                    state_before,
                    "membership_paystack",
                    True
                )

            return (
                "Sorry, I couldn't generate a payment link right now. Please try again in a moment or type MENU.",
                "idle",
                state_before,
                "membership_paystack_fail",
                ai_used,
            )

        if state == "awaiting_payment_proof":
            # This state is now mostly a fallback if they don't click the link or need help
            return (
                "Please use the Paystack link above to complete your payment. If you're having trouble, let me know!",
                "idle",
                state_before,
                "payment_reminder",
                ai_used,
            )

        if state == "awaiting_order":
            order_id = await self.create_order_from_text(phone, body_clean)
            await self.upsert_member_state(phone, {"state": "idle"})
            return (
                f"Order received! ID: {order_id}. We'll confirm totals and payment shortly.",
                "idle",
                state_before,
                "order_capture",
                ai_used,
            )

        if state == "awaiting_address":
            address_text = body_clean
            await self.upsert_member_state(phone, {"address": address_text, "state": "idle"})
            # Override to proceed with checkout immediately without recursion
            body_clean = "CHECKOUT"
            lower = "checkout"
            state = "idle"
            member["address"] = address_text
            member["state"] = "idle"
            # Fall through to AI-based intent classification below

        # Cart action shortcut if waiting for confirmation - AI-only with context
        if member.get("state") == "awaiting_cart_action":
            product = member.get("last_product")
            recent_products = member.get("recent_products", [])
            
            if not product:
                await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                return ("Let's start over. Tell me what you want and I'll add it to your cart.", "idle", state_before, intent, ai_used, button_actions)
            
            # Use AI for intent classification - no keyword matching
            if self.ai_service:
                # Use AI with context that we're in cart action state
                try:
                    # Build context with product info
                    cart_context = {
                        "in_cart_action_state": True,
                        "has_product_selected": True,
                        "product_name": product.get("name", "") if isinstance(product, dict) else "",
                        "has_multiple_products": len(recent_products) > 1
                    }
                    if recent_products:
                        cart_context["available_products"] = [p.get("name", "") for p in recent_products[:5]]
                    
                    intent_check = await self.ai_service.classify_intent(body_clean, context=cart_context)
                    
                    if intent_check == "cart_checkout":
                        # Fall through to checkout logic below
                        await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                        # Override to proceed with checkout
                        body_clean = "CHECKOUT"
                        lower = "checkout"
                        state = "idle"
                        member["state"] = "idle"
                    elif intent_check == "cart_add":
                        # If multiple products, use AI to identify which one they want
                        selected_product = product
                        user_lower = body_clean.lower().strip()
                        
                        # If multiple products, always try to identify which one using AI
                        if len(recent_products) > 1:
                            # Check if user just said "add" without any product identifier
                            simple_add_commands = ["add", "yes", "y", "ok", "okay", "sure", "proceed", "add to cart"]
                            if user_lower in simple_add_commands:
                                # User didn't specify - ask them to clarify
                                product_names = [p.get("name", "") for p in recent_products[:5]]
                                product_list = "\n".join([f"• {name}" for name in product_names])
                                button_actions = [
                                    {"action": "quick_reply", "content": "Add First"},
                                    {"action": "quick_reply", "content": "View Cart"},
                                    {"action": "quick_reply", "content": "Search More"}
                                ]
                                return (
                                    f"I see multiple products. Which one would you like to add?\n\n{product_list}\n\nPlease specify the product name (e.g., 'Mango Rice' or 'Big Bull').",
                                    "awaiting_cart_action",
                                    state_before,
                                    "cart_add_clarify",
                                    True,
                                    button_actions
                                )
                            
                            # User specified something - use AI to identify the product
                            if self.ai_service:
                                try:
                                    # Use AI to extract product name from user message
                                    # Build context with available products
                                    products_list = "\n".join([f"- {p.get('name', '')} (SKU: {p.get('sku', '')})" for p in recent_products])
                                    ai_prompt = f"""The user wants to add a product to their cart. They said: "{body_clean}"

Available products:
{products_list}

Extract which product they want. Look for:
- Product names mentioned (e.g., "mango", "big bull", "rice", "oil")
- SKU codes mentioned
- Any keywords that match product names

Return ONLY the product name or SKU from the list above, nothing else. If you cannot determine, return "UNKNOWN"."""
                                    
                                    completion = await self.ai_service.client.chat.completions.create(
                                        model="gpt-4o-mini",
                                        messages=[
                                            {"role": "system", "content": "Extract the exact product name or SKU the user wants to add from the provided list. Return only the product name/SKU or 'UNKNOWN' if unclear."},
                                            {"role": "user", "content": ai_prompt}
                                        ],
                                        max_tokens=50,
                                        temperature=0.1,
                                    )
                                    extracted = completion.choices[0].message.content.strip()
                                    
                                    # Match extracted product to recent products
                                    if extracted.upper() != "UNKNOWN":
                                        extracted_lower = extracted.lower()
                                        for p in recent_products:
                                            p_name = p.get("name", "").lower()
                                            p_sku = p.get("sku", "").lower()
                                            # Check if extracted text matches this product
                                            if (extracted_lower in p_name or p_name in extracted_lower or 
                                                (p_sku and (extracted_lower in p_sku or p_sku in extracted_lower)) or
                                                any(word in p_name for word in extracted_lower.split() if len(word) > 2)):
                                                # Found a match - get full product object
                                                search_results = await self.search_products(p.get("name", ""), member.get("city"))
                                                if search_results:
                                                    selected_product = search_results[0]
                                                    break
                                    
                                    # If no match found, try direct text matching as fallback
                                    if selected_product == product:  # Still using default
                                        for p in recent_products:
                                            p_name = p.get("name", "").lower()
                                            p_sku = p.get("sku", "").lower()
                                            # Check for keyword matches (e.g., "mango" matches "MANGO RICE")
                                            user_words = set(user_lower.split())
                                            product_words = set(p_name.split())
                                            if (user_lower in p_name or p_name in user_lower or 
                                                (p_sku and p_sku in user_lower) or
                                                len(user_words & product_words) > 0):
                                                search_results = await self.search_products(p.get("name", ""), member.get("city"))
                                                if search_results:
                                                    selected_product = search_results[0]
                                                    break
                                except Exception as e:
                                    print(f"Error using AI to identify product: {e}")
                                    # Fall back to simple text matching
                                    for p in recent_products:
                                        p_name = p.get("name", "").lower()
                                        p_sku = p.get("sku", "").lower()
                                        if (user_lower in p_name or p_name in user_lower or 
                                            (p_sku and p_sku in user_lower)):
                                            search_results = await self.search_products(p.get("name", ""), member.get("city"))
                                            if search_results:
                                                selected_product = search_results[0]
                                                break
                        
                        # Ensure product is a dict with all required fields (sku, name, price)
                        if not isinstance(selected_product, dict) or not selected_product.get("sku"):
                            # If product is incomplete, search for it using name
                            product_name = selected_product.get("name", "") if isinstance(selected_product, dict) else str(selected_product)
                            if product_name:
                                search_results = await self.search_products(product_name, member.get("city"))
                                if search_results:
                                    selected_product = search_results[0]
                                else:
                                    # Last resort: try to get from recent_products
                                    if recent_products and len(recent_products) > 0:
                                        first_product_info = recent_products[0]
                                        search_results = await self.search_products(first_product_info.get("name", ""), member.get("city"))
                                        if search_results:
                                            selected_product = search_results[0]
                        
                        # Final check - if still no valid product, return error
                        if not isinstance(selected_product, dict) or not selected_product.get("sku"):
                            await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                            return ("Sorry, I couldn't find that product. Please search again or specify which product you want.", "idle", state_before, "cart_add_fail", True, button_actions)
                        
                        await self.add_item_to_cart(phone, selected_product, qty=1)
                        cart = await self.get_cart(phone)
                        summary = self.render_cart_summary(cart)
                        await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                        product_name = selected_product.get("name", "item")
                        # Add cart action buttons
                        button_actions = [
                            {"action": "quick_reply", "content": "View Cart"},
                            {"action": "quick_reply", "content": "Checkout"},
                            {"action": "quick_reply", "content": "Continue Shopping"}
                        ]
                        return (f"✅ Added {product_name} to your cart.\n{summary}", "idle", state_before, "cart_add", True, button_actions)
                    elif intent_check in {"cart_view", "order_help", "other"}:
                        # Revert state to idle so main logic picks it up below
                        await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                        # Fall through to main logic
                        pass
                    elif intent_check == "catalog_search":
                        # User wants to browse/search instead - revert state
                        await self.upsert_member_state(phone, {"state": "idle", "last_product": None, "recent_products": []})
                        # Fall through to main logic
                        pass
                    else:
                        # Add buttons for cart action
                        button_actions = [
                            {"action": "quick_reply", "content": "Add to Cart"},
                            {"action": "quick_reply", "content": "Checkout"},
                            {"action": "quick_reply", "content": "View Details"}
                        ]
                        return ("Would you like to add this to your cart, checkout, or continue browsing? Please let me know what you'd like to do.", "awaiting_cart_action", state_before, "cart_prompt", True, button_actions)
                except Exception as e:
                    print(f"Error in cart action AI classification: {e}")
                    # On error, default to asking for clarification
                    # Add buttons for cart action
                    button_actions = [
                        {"action": "quick_reply", "content": "Add to Cart"},
                        {"action": "quick_reply", "content": "Checkout"},
                        {"action": "quick_reply", "content": "View Details"}
                    ]
                    return ("Would you like to add this to your cart, checkout, or continue browsing? Please let me know what you'd like to do.", "awaiting_cart_action", state_before, "cart_prompt", False, button_actions)
            else:
                return ("I need AI assistance to understand your response. Please try again in a moment.", "idle", state_before, "ai_unavailable", False, button_actions)

        # ============================================
        # AI-FIRST INTENT CLASSIFICATION
        # ============================================

        product_query = None
        intent_guess = None

        # CRITICAL KEYWORD OVERRIDES (system-level commands only)
        if "JOIN_CLUSTER_" in body_clean:
            cluster_id = body_clean.split("JOIN_CLUSTER_")[1].strip()
            cluster = await self.get_custom_cluster(cluster_id)
            if not cluster:
                return ("Sorry, I couldn't find that cluster.", "idle", state_before, "cluster_join_fail", False, button_actions)

            if member.get("payment_status") != "paid":
                return (
                    "You need an active subscription before joining a shared cluster. Reply UPGRADE to see plans.",
                    "idle",
                    state_before,
                    "cluster_join_blocked_unpaid",
                    False,
                )

            if len(cluster.get("members", [])) >= cluster.get("max_people", 5):
                 return (f"Sorry, the cluster '{cluster['name']}' is already full.", "idle", state_before, "cluster_full", False, button_actions)

            if phone not in cluster.get("members", []):
                cluster["members"].append(phone)
                await self.save_custom_cluster(cluster)

            await self.upsert_member_state(phone, {"current_cluster_id": cluster_id, "state": "idle"})
            return (
                f"✅ You've joined the cluster '{cluster['name']}'!\n\n"
                "You now share a cart with other members. Anyone can add items, but only the creator can checkout.",
                "idle",
                state_before,
                "cluster_join_success",
                False
            )

        # Cluster leave is handled by AI intent classification - no keyword matching needed

        # Use AI for intent classification - AI-only, no keyword fallbacks
        if not self.ai_service:
            # AI service is required - return error message if unavailable
            return (
                "I'm having trouble understanding messages right now. Please try again in a moment.",
                "idle",
                state_before,
                "ai_unavailable",
                False
            )
        
        # Build rich context for AI
        intent_context = {
            "in_cluster": member.get("current_cluster_id") is not None,
            "payment_status": member.get("payment_status"),
        }
        
        # Use AI for intent classification - no fallbacks
        try:
            import asyncio
            ai_intent = await asyncio.wait_for(
                self.ai_service.classify_intent(body_clean, context=intent_context),
                timeout=5.0  # Increased timeout for reliability
            )
            if ai_intent:
                intent_guess = ai_intent
                ai_used = True
            else:
                # If AI returns None, default to catalog_search
                intent_guess = "catalog_search"
                ai_used = True
        except asyncio.TimeoutError:
            # On timeout, default to catalog_search to avoid blocking user
            print(f"AI intent classification timeout for message: {body_clean[:50]}")
            intent_guess = "catalog_search"
            ai_used = False
        except Exception as e:
            print(f"AI intent error: {e}")
            # On error, return error message - no keyword fallback
            return (
                "I'm having trouble understanding your message right now. Please try rephrasing or try again in a moment.",
                "idle",
                state_before,
                "ai_error",
                False
            )

        # Set product query for catalog searches
        if intent_guess == "catalog_search":
            product_query = body_clean

        # MENU/HELP Intent
        if intent_guess == "menu_help":
            name = member.get("name", "")
            greeting = f"Hey {name}! " if name else "Hi! "
            help_text = (
                f"{greeting}Here's what I can help you with:\n\n"
                "🛒 *Shopping*\n"
                "Just type what you're looking for (rice, oil, indomie, etc.) and I'll show you what's available\n\n"
                "🛍️ *Your Cart*\n"
                "Say 'cart' to see your items or 'checkout' when ready to order\n\n"
                "👥 *Shopping Clusters*\n"
                "Create or join groups to shop together and save\n\n"
                "🔗 *Share & Earn*\n"
                "Say 'referral' to get your invite link\n\n"
                "Type what you need and let's get started!"
            )
            return (help_text, "idle", state_before, "menu_help", ai_used, button_actions)

        # PAYMENT CONFIRMATION Intent
        if intent_guess == "payment_confirmation":
            if media_url:
                # They sent payment proof
                ref = await self.apply_payment_proof(phone, media_url)
                return (
                    f"✅ Payment proof received! Reference: {ref}\n\n"
                    "Our team will verify and activate your account within 24 hours. Thanks for your patience!",
                    "idle",
                    state_before,
                    "payment_proof_received",
                    ai_used,
                )
            else:
                # They're asking about payment status - CHECK ACTUAL STATUS FIRST
                current_member = await self.get_member(phone)
                actual_status = current_member.get("payment_status")
                
                if actual_status == "paid":
                    return (
                        "Your payment is confirmed! ✅ You can start shopping now. Type 'products' to see what's available.",
                        "idle",
                        state_before,
                        "payment_already_confirmed",
                        ai_used,
                    )
                else:
                    return (
                        "Your payment status is currently *not confirmed*. If you've already paid via Paystack, it should reflect automatically within a few minutes. "
                        "If you paid via bank transfer, please send a screenshot of your payment receipt, and we'll verify it manually.",
                        "idle",
                        state_before,
                        "payment_status_inquiry",
                        ai_used,
                    )

        if intent_guess == "cluster_join":
            # Enforce join via invite link only
            bot_num = self.settings.twilio_from_number.replace("whatsapp:", "").replace("+", "")
            return (
                f"To join a cluster, please use the invite link shared by the owner. It looks like https://wa.me/{bot_num}?text=JOIN_CLUSTER_<id>.",
                "idle",
                state_before,
                "cluster_join_link_required",
                ai_used,
            )

        if intent_guess == "cluster_create":
            # Check if they already provided name/limit
            details = await self.ai_service.extract_cluster_details(body_clean) if self.ai_service else None
            if details and details.get("name"):
                await self.upsert_member_state(phone, {"state": "awaiting_cluster_limit", "temp_cluster_name": details["name"]})
                return (
                    f"I'll set up the cluster '{details['name']}'. What's the maximum number of people allowed? (default is 5)",
                    "awaiting_cluster_limit",
                    state_before,
                    "cluster_create_start",
                    True
                )
            else:
                await self.upsert_member_state(phone, {"state": "awaiting_cluster_name"})
                return (
                    "Sure! Let's create a custom cluster. What should we name it?",
                    "awaiting_cluster_name",
                    state_before,
                    "cluster_create_name_prompt",
                    True
                )

        if intent_guess == "cluster_view":
            clusters = await self.get_user_clusters(phone)
            if not clusters:
                return (
                    "You aren't in any clusters yet. Would you like to create one or join a friend's?",
                    "idle",
                    state_before,
                    "cluster_view_empty",
                    True
                )

            current_cluster_id = member.get("current_cluster_id")
            active_summary = ""
            if current_cluster_id:
                cluster = await self.get_custom_cluster(current_cluster_id)
                if cluster:
                    active_summary = f"\n\n*Current Active Cluster: {cluster['name']}*\n"
                    active_summary += self.render_cart_summary({
                        "cluster_name": cluster['name'],
                        "items": cluster.get("items") or []
                    }, with_instructions=False)

            lines = ["*Your Clusters:*"]
            for c in clusters:
                role = "Owner" if c.get("owner_phone") == phone else "Member"
                member_count = len(c.get("members", []))
                limit = c.get("max_people", 5)
                indicator = "🟢 " if str(c.get("_id")) == current_cluster_id else "• "
                lines.append(f"{indicator}*{c['name']}* ({role}) - {member_count}/{limit} members")

            lines.append("\nTo use a cluster's shared cart, just join it using the link provided when it was created.")
            return ("\n".join(lines) + active_summary, "idle", state_before, "cluster_view", True, button_actions)

        if intent_guess == "cluster_rename":
            details = await self.ai_service.extract_cluster_details(body_clean) if self.ai_service else None
            new_name = details.get("new_name") if details else None

            if not new_name:
                return ("What would you like to rename the cluster to?", "idle", state_before, "cluster_rename_prompt", True, button_actions)

            # Check for clusters owned by this user
            clusters = await self.get_user_clusters(phone)
            owned = [c for c in clusters if c.get("owner_phone") == phone]

            if not owned:
                return ("You don't own any clusters that can be renamed.", "idle", state_before, "cluster_rename_no_owned", True, button_actions)

            # If they own multiple, we might need to ask which one, but for now let's assume the active one or the most recent one
            target_cluster = None
            current_cluster_id = member.get("current_cluster_id")
            if current_cluster_id:
                target_cluster = await self.get_custom_cluster(current_cluster_id)
                if target_cluster and target_cluster.get("owner_phone") != phone:
                    target_cluster = None

            if not target_cluster and owned:
                target_cluster = owned[0] # Pick the first/most recent

            if target_cluster:
                old_name = target_cluster.get("name")
                target_cluster["name"] = new_name
                await self.save_custom_cluster(target_cluster)
                return (f"✅ Cluster '{old_name}' has been renamed to '{new_name}'!", "idle", state_before, "cluster_renamed", True, button_actions)

            return ("I couldn't find a cluster you own to rename.", "idle", state_before, "cluster_rename_fail", True, button_actions)

        if intent_guess == "referral_link":
            me = member.get("name", "Friend")
            bot_num = self.settings.twilio_from_number.replace("whatsapp:", "").replace("+", "")
            link = f"https://wa.me/{bot_num}?text=I%20was%20referred%20by%20{me}"
            return (f"Share PNP Lite with your friends! Give them this link: {link}", "idle", state_before, "referral", True, button_actions)
        
        # HANDLE INTENTS
        
        # 1. Cart View
        if intent_guess == "cart_view":
             target = "cluster"
             spec_cluster_name = None
             forced_choice_prompt = False
             if self.ai_service:
                 actions = await self.ai_service.extract_cart_action(body_clean)
                 if actions:
                     target = actions[0].get("target", "cluster")
                     spec_cluster_name = actions[0].get("cluster_name")
             
             # If a specific cluster name is mentioned, try to find it and switch to it
             if spec_cluster_name:
                 user_clusters = await self.get_user_clusters(phone)
                 found_c = None
                 for uc in user_clusters:
                    if uc["name"].lower() == spec_cluster_name.lower():
                        found_c = uc
                        break
                 
                 if found_c:
                    # Switch active cluster to this one
                    await self.upsert_member_state(phone, {"current_cluster_id": str(found_c["_id"])})
                    target = "cluster"
                 else:
                    return (f"❓ I couldn't find a cluster named '{spec_cluster_name}' among your groups.", "idle", state_before, "cart_view_fail", True, button_actions)

             force_p = (target == "personal")
             cart = await self.get_cart(phone, force_personal=force_p)
             summary = self.render_cart_summary(cart)
             
             # Add cart action buttons
             cart_button_actions = [
                 {"action": "quick_reply", "content": "Checkout"},
                 {"action": "quick_reply", "content": "Add More"},
                 {"action": "quick_reply", "content": "Remove Item"}
             ]
             
             # If showing one, and they have items in the other, mention it
             other_target = "personal" if target == "cluster" else "cluster"
             other_cart = await self.get_cart(phone, force_personal=(other_target == "personal"))
             if other_cart.get("items"):
                 summary_other = self.render_cart_summary(other_cart, with_instructions=False)
                 combined = [
                     f"Here are both carts so you can choose:",
                     f"*{cart.get('cluster_name') or 'Cluster Cart' if target == 'cluster' else 'Personal Cart'}*",
                     summary,
                     "",
                     f"*{other_cart.get('cluster_name') or 'Cluster Cart' if other_target == 'cluster' else 'Personal Cart'}*",
                     summary_other,
                     "",
                     "Reply 'cluster cart' or 'personal cart' to focus on one."
                 ]
                 return ("\n".join(combined), "idle", state_before, "cart_view_dual", True, cart_button_actions)
             else:
                 if target == "cluster" and not cart.get("items"):
                     # If cluster cart empty but personal has items, prompt
                     if other_cart.get("items"):
                         return (
                             "Your cluster cart is empty. I found items in your personal cart. Reply 'personal cart' to see it.",
                             "idle",
                             state_before,
                             "cart_view_switch",
                             True,
                             cart_button_actions,
                         )
                 return (summary, "idle", state_before, "cart_view", True, cart_button_actions)

        # 2. Checkout
        if intent_guess == "cart_checkout":
             cart = await self.get_cart(phone)
             items = cart.get("items")
             cluster_id = cart.get("cluster_id")
             cluster = None
             if cluster_id:
                 cluster = await self.get_custom_cluster(cluster_id)
                 if not cluster:
                     return ("I couldn't find this cluster anymore. Try switching to your personal cart or create a new cluster.", "idle", state_before, "checkout_cluster_missing", True, button_actions)
                 if cluster.get("owner_phone") != phone:
                     owner = await self.get_member(cluster.get("owner_phone"))
                     owner_name = (owner or {}).get("name") or "the cluster owner"
                     return (f"Only {owner_name} can check out this shared cluster cart.", "idle", state_before, "checkout_restricted", True, button_actions)
             if not items:
                 return ("Your cart is empty.", "idle", state_before, "cart_checkout_empty", True, button_actions)
             
             # Check for address
             if not member.get("address"):
                  await self.upsert_member_state(phone, {"state": "awaiting_address"})
                  return ("Wait! We don't have your delivery address yet. Please reply with your full delivery address.", "awaiting_address", state_before, "checkout_need_address", True, button_actions)
             
             # Block if not paid
             if member.get("payment_status") != "paid":
                  return ("Oops! To place an order, you need to have an active subscription. Please complete your registration/payment first or reply UPGRADE to see plans.", "idle", state_before, "checkout_blocked", True, button_actions)

             # Create Order
             order_slug, total_val = await self.create_order_from_cart(phone)
             
             if order_slug == "RESTRICTED":
                  # ... restricted logic remains same ...
                  cluster_id = member.get("current_cluster_id")
                  cluster = await self.get_custom_cluster(cluster_id)
                  owner_name = "the cluster owner"
                  if cluster:
                      owner = await self.get_member(cluster.get("owner_phone"))
                      owner_name = owner.get("name") or "the cluster owner"
                  return (f"Only {owner_name} can check out this shared cluster cart.", "idle", state_before, "checkout_restricted", True, button_actions)
             
             if not order_slug:
                  return ("I couldn't create an order from your cart. Please try again.", "idle", state_before, "cart_checkout_fail", True, button_actions)

             # Cluster checkout: send payment links to all members
             if cluster:
                 summary = await self.initiate_cluster_payment_links(order_slug, total_val, cluster, member)
                 await self.upsert_member_state(phone, {"state": "idle", "last_order_slug": order_slug})
                 return (summary, "idle", state_before, "cart_checkout_cluster", True, button_actions)
             
             # Initialize Paystack for Order
             metadata = {
                 "type": "order",
                 "phone": phone,
                 "order_slug": order_slug
             }
             
             amount_kobo = int(total_val * 100)
             pay_link = await self.paystack.initialize_transaction(
                 email=f"{phone}@pnplite.ng",
                 amount_kobo=amount_kobo,
                 metadata=metadata
             )

             if pay_link and pay_link.get("authorization_url"):
                 url = pay_link["authorization_url"]
                 msg = (
                     f"Order *{order_slug}* created! \n"
                     f"Total: *₦{total_val:,.0f}* (includes delivery).\n\n"
                     f"Click here to pay: {url}\n\n"
                     "Your order will be processed automatically after payment."
                 )
                 await self.upsert_member_state(phone, {"state": "idle", "last_order_slug": order_slug})
                 return (msg, "idle", state_before, "cart_checkout_paystack", True, button_actions)

             return (
                 "Sorry, I couldn't generate a payment link for your order. Please try again in a moment.",
                 "idle",
                 state_before,
                 "cart_checkout_fail",
                 True
             )

        # 3. Cart Modification (nl add/remove)
        if intent_guess in {"cart_add", "cart_remove"} and self.ai_service:
            # Use AI to extract all actions (can be multiple)
            actions = await self.ai_service.extract_cart_action(body_clean)
            if actions:
                feedback = []
                # Get owned/joined cluster names to avoid confusion
                user_clusters = await self.get_user_clusters(phone)
                cluster_names = {c["name"].lower() for c in user_clusters}

                for act in actions:
                    a_type = act.get("action", "add")
                    item_q = act.get("item")
                    qty = int(act.get("qty", 1))
                    target = act.get("target", "cluster")
                    spec_cluster_name = act.get("cluster_name")

                    # If specific cluster name provided, switch/target it
                    if spec_cluster_name:
                        user_clusters = await self.get_user_clusters(phone)
                        found_c = None
                        for uc in user_clusters:
                            if uc["name"].lower() == spec_cluster_name.lower():
                                found_c = uc
                                break
                        if found_c:
                            await self.upsert_member_state(phone, {"current_cluster_id": str(found_c["_id"])})
                            target = "cluster"
                        # if not found, we just use current/default logic

                    force_p = (target == "personal")
                    
                    if not item_q: continue

                    # Safety check: if the item_q matches a known cluster name, skip it
                    if item_q.lower() in cluster_names:
                        continue

                    if a_type == "remove":
                        removed = await self.remove_item_from_cart(phone, item_q, force_personal=force_p)
                        if removed:
                            target_str = "personal cart" if force_p else "shared cart"
                            feedback.append(f"✅ Removed {item_q} from {target_str}.")
                        else:
                            feedback.append(f"❓ Could not find {item_q} in your cart.")
                    else:
                        # Search for the product
                        results = await self.search_products(item_q, member.get("city"))
                        if len(results) == 1:
                            p = results[0]
                            await self.add_item_to_cart(phone, p, qty=qty, force_personal=force_p)
                            target_str = "personal cart" if force_p else "shared cart"
                            feedback.append(f"✅ Added {p['name']} (x{qty}) to {target_str}.")
                        elif len(results) > 1:
                            feedback.append(f"🔍 Multiple matches for '{item_q}'. Please be specific.")
                        else:
                            feedback.append(f"❌ Product '{item_q}' not found.")
                
                # Show updated cart summary
                # If mixed, we might show both or just the last used one. 
                # For simplicity, if they added to personal, show personal.
                is_any_personal = any(act.get("target") == "personal" for act in actions)
                cart = await self.get_cart(phone, force_personal=is_any_personal)
                summary = self.render_cart_summary(cart)
                reply = "\n".join(feedback) + f"\n\n{summary}"
                return (reply, "idle", state_before, f"cart_mod", True, button_actions)

        # 4. Product Search
        if intent_guess == "catalog_search" or product_query is not None:
            # Extract product query using AI first (handles questions like "Do you have Indomie?")
            original_query = body_clean
            if product_query is None:
                if self.ai_service:
                    try:
                        extracted_q = await self.ai_service.extract_product_query(body_clean)
                        if extracted_q and extracted_q.strip():
                            product_query = extracted_q.strip()
                        else:
                            # Empty string means general query - show all products
                            product_query = ""
                    except Exception as e:
                        print(f"Error extracting product query: {e}")
                        # On error, try the original message as fallback
                        product_query = body_clean
                else:
                    product_query = body_clean

            # If product_query is still None or empty after extraction, use original message
            # AI extraction handles determining if it's a general vs specific query
            if not product_query or not product_query.strip():
                product_query = body_clean.strip()

            # Perform search
            # Use unified search_products (even for empty query to get featured list matched to city)
            results = await self.search_products(product_query, member.get("city"))

            # FINAL FALLBACK: If no results and we have a specific query, try multiple fallback strategies
            if not results and product_query and product_query.strip():
                # Try 1: Search with empty query to show all products
                results = await self.search_products("", member.get("city"))
                
                # Try 2: If still no results, don't bypass city filter
                # City filtering is important - if no products match the city, return empty
                # This ensures products are only shown to users in the correct cities

            if results:
                # Manual Cart Add from catalog search (single match auto-add)
                if intent_guess == "cart_add" and len(results) == 1:
                    # Try to extract quantity
                    qty = 1
                    if self.ai_service:
                        actions = await self.ai_service.extract_cart_action(body_clean)
                        if actions and len(actions) > 0:
                            qty = int(actions[0].get("qty", 1))
                    
                    product = results[0]
                    await self.add_item_to_cart(phone, product, qty=qty)
                    cart = await self.get_cart(phone)
                    summary = self.render_cart_summary(cart)
                    return (f"✅ Added {product['name']} (x{qty}) to cart.\n{summary}", "idle", state_before, "cart_add_auto", True, button_actions)
                
                lines = [f"*Available products:*"]
                for p in results:
                    base_price = p.get("price", 0)
                    try:
                        base_price_val = float(str(base_price).replace(",", "").replace("₦", "").strip())
                    except:
                        base_price_val = 0
                    
                    name = p.get("name", "Unknown")
                    clusters = p.get("clusters") or []
                    
                    # Calculate slot cost and total cost if cluster info available
                    price_display = f"₦{base_price_val:,.0f}"
                    
                    if clusters:
                        for c in clusters:
                            people_per_cluster = c.get("people_per_cluster") or 1
                            if people_per_cluster > 0:
                                slot_cost = base_price_val / people_per_cluster
                                price_display = f"₦{base_price_val:,.0f} (Slot: ₦{slot_cost:,.0f}, Total: ₦{base_price_val:,.0f})"
                                break
                    
                    cluster_note = ""
                    if clusters:
                        snippets = []
                        for c in clusters:
                            seg = c.get("city") or ""
                            if c.get("area"):
                                seg += f" / {c['area']}"
                            if c.get("people_per_cluster"):
                                seg += f" • {c['people_per_cluster']} ppl/cluster"
                            snippets.append(seg)
                        if snippets:
                            cluster_note = " [" + "; ".join(snippets) + "]"
                    lines.append(f"• {name}: {price_display}{cluster_note}")
                
                reply = "\n".join(lines)
                await self.send_catalog_cards(phone, results, limit=5)
                
                if len(results) == 1:
                    product = results[0]
                    await self.upsert_member_state(phone, {"state": "awaiting_cart_action", "last_product": product})
                    reply += "\nAdd this to your cart?"
                    # Add buttons for single product
                    button_actions = [
                        {"action": "quick_reply", "content": "Add to Cart"},
                        {"action": "quick_reply", "content": "Checkout"},
                        {"action": "quick_reply", "content": "View Details"}
                    ]
                    return (reply, "awaiting_cart_action", state_before, "catalogue_search", True, button_actions)
                else:
                    # Multiple products: store them and set state to await cart action
                    # Store first product as default, and list of all products for AI to choose from
                    await self.upsert_member_state(phone, {
                        "state": "awaiting_cart_action", 
                        "last_product": results[0] if results else None,
                        "recent_products": [{"name": p.get("name"), "sku": p.get("sku"), "price": p.get("price")} for p in results[:10]]  # Store up to 10
                    })
                    reply += "\nWhich one would you like to add?"
                    # Add buttons for multiple products
                    button_actions = [
                        {"action": "quick_reply", "content": "Add First"},
                        {"action": "quick_reply", "content": "View Cart"},
                        {"action": "quick_reply", "content": "Search More"}
                    ]
                    return (reply, "awaiting_cart_action", state_before, "catalogue_search", True, button_actions)
            else:
                # No products found - suggest categories (filtered by city)
                categories = await self.get_product_categories()
                # Filter categories by city to match what search_products would return
                member_city = member.get("city")
                filtered_categories = {}
                for cat, prods in categories.items():
                    filtered_prods = [p for p in prods if self._product_visible_for_city(p, member_city)]
                    if filtered_prods:
                        filtered_categories[cat] = filtered_prods
                
                available_categories = [cat for cat, prods in filtered_categories.items() if prods and cat != "other"]
                
                if original_query and product_query:
                    # Use original query in error message for better user experience
                    display_query = original_query if len(original_query) < 50 else product_query
                    suggestion_lines = [
                        f"Sorry, I couldn't find '{display_query}' in our current catalog."
                    ]
                    
                    if available_categories:
                        suggestion_lines.append("\n*Available product categories:*")
                        for cat in available_categories[:6]:  # Show top 6
                            cat_name = cat.capitalize()
                            count = len(filtered_categories[cat])
                            suggestion_lines.append(f"• {cat_name} ({count} items)")
                    
                    suggestion_lines.append("\nTry searching for a category like: rice, oil, fish, chicken, etc.")
                    
                    return (
                        "\n".join(suggestion_lines),
                        "idle",
                        state_before,
                        "catalog_no_results",
                        ai_used
                    )
                else:
                    return (
                        f"We're still building our catalog for {member.get('city', 'your area')}. "
                        "Check back soon or contact support for specific products you need!",
                        "idle",
                        state_before,
                        "catalog_empty",
                        ai_used
                    )

        # 5. General AI Chat / FAQ
        owned_clusters = [c["name"] for c in await self.get_user_clusters(phone) if c["owner_phone"] == phone]
        joined_clusters = [c["name"] for c in await self.get_user_clusters(phone) if phone in c.get("members", []) and c["owner_phone"] != phone]
        
        context = {
            "member_name": member.get("name", "Friend"),
            "member_city": member.get("city", "Unknown"),
            "membership": member.get("membership_type"),
            "paid": member.get("payment_status") == "paid",
            "cart_items": (await self.get_cart(phone)).get("items", []),
            "owned_clusters": owned_clusters,
            "joined_clusters": joined_clusters,
            "current_cluster": (await self.get_custom_cluster(member.get("current_cluster_id")) or {}).get("name") if member.get("current_cluster_id") else None
        }

        if self.ai_service:
            # Fallback for general conversation
            ai_reply = await self.ai_service.generate_response(body_clean, context)
            if ai_reply:
                return (ai_reply, "idle", state_before, "ai_chat", True, button_actions)

        # Final fallback with helpful suggestions
        name = context.get("member_name", "")
        greeting = f"Hey {name}! " if name else "Hi there! "
        return (
            f"{greeting}I can help you with:\n"
            "• Browse products - just type what you're looking for (rice, oil, etc.)\n"
            "• View your cart - say 'cart'\n"
            "• Checkout - say 'checkout'\n"
            "• Get help - say 'menu'\n\n"
            "What would you like to do?",
            "idle",
            state_before,
            "fallback",
            ai_used,
        )
