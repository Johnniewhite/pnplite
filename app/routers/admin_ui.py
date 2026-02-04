from datetime import datetime
from typing import Optional
import secrets
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form, UploadFile, File, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from pathlib import Path
import uuid
from urllib.parse import urlparse, urlunparse, quote

from app.config.db import mongo
from app.config.settings import Settings, get_settings
from app.services.whatsapp_service import WhatsAppService

router = APIRouter(prefix="/ui/admin", tags=["admin-ui"])
templates = Jinja2Templates(directory="templates")

# Simple in-memory session store (in production, use Redis or database)
_sessions: dict[str, dict] = {}


class AuthRedirectException(Exception):
    """Exception to signal authentication redirect needed."""
    def __init__(self, next_url: str = "/ui/admin/dashboard"):
        self.next_url = next_url


def datetimeformat(value):
    if not value:
        return ""
    if isinstance(value, float) or isinstance(value, int):
        # Assume timestamp
        dt = datetime.fromtimestamp(value)
        return dt.strftime("%b %d, %H:%M")
    if isinstance(value, str):
        # Try parse or return
        try:
            val = datetime.fromisoformat(value)
            return val.strftime("%b %d, %H:%M")
        except:
            pass
    if isinstance(value, datetime):
        return value.strftime("%b %d, %H:%M")
    return value


def comma(value):
    """Format number with comma separators"""
    try:
        return "{:,}".format(int(value))
    except (ValueError, TypeError):
        return value


templates.env.filters["datetimeformat"] = datetimeformat
templates.env.filters["comma"] = comma


def require_db():
    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")
    return mongo.db


def get_service(settings: Settings = Depends(get_settings)) -> WhatsAppService:
    if mongo.db is None:
        raise RuntimeError("Mongo client not initialized")
    return WhatsAppService(mongo.db, settings, ai_service=None)


def build_public_base(request: Request, settings: Settings) -> str:
    if settings.ngrok_url:
        return settings.ngrok_url.rstrip("/")
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def get_current_admin(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """Get the current admin from session cookie. Raises AuthRedirectException if not authenticated."""
    session_id = request.cookies.get("admin_session")

    if not session_id or session_id not in _sessions:
        # Redirect to login page with next URL
        next_url = str(request.url.path)
        if request.url.query:
            next_url += f"?{request.url.query}"
        raise AuthRedirectException(next_url=next_url)

    session = _sessions[session_id]
    username = session.get("username")

    # Verify the user is still an admin
    allowed = set(settings.admin_numbers if isinstance(settings.admin_numbers, list) else [])
    if username not in allowed:
        # Session exists but user is no longer an admin, clear session
        del _sessions[session_id]
        raise AuthRedirectException(next_url=str(request.url.path))

    return username


def get_optional_admin(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Optional[str]:
    """Get the current admin if authenticated, otherwise return None."""
    try:
        return get_current_admin(request, settings)
    except AuthRedirectException:
        return None


@router.get("/login")
async def login_page(
    request: Request,
    error: Optional[str] = None,
    next: Optional[str] = None,
):
    """Display the login page."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "next_url": next}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(None),
    settings: Settings = Depends(get_settings),
):
    """Handle login form submission."""
    # Normalize phone number
    username = username.strip()
    if not username.startswith("+"):
        username = "+" + username

    # Check if user is an admin
    allowed = set(settings.admin_numbers if isinstance(settings.admin_numbers, list) else [])

    if username not in allowed:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "This phone number is not authorized as an admin.", "next_url": next},
            status_code=401
        )

    # Check password
    if settings.admin_dash_password and password != settings.admin_dash_password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password. Please try again.", "next_url": next},
            status_code=401
        )

    # Create session
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "username": username,
        "created_at": datetime.utcnow()
    }

    # Redirect to the next URL or dashboard
    redirect_url = next if next and next.startswith("/ui/admin") else "/ui/admin/dashboard"
    redirect_response = RedirectResponse(url=redirect_url, status_code=303)
    redirect_response.set_cookie(
        key="admin_session",
        value=session_id,
        httponly=True,
        max_age=86400 * 7,  # 7 days
        samesite="lax"
    )
    return redirect_response


@router.get("/logout")
async def logout(request: Request):
    """Log out the current admin."""
    session_id = request.cookies.get("admin_session")
    if session_id and session_id in _sessions:
        del _sessions[session_id]

    response = RedirectResponse(url="/ui/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


@router.get("/")
async def admin_home(request: Request):
    # Check if authenticated, redirect to login if not
    try:
        get_current_admin(request, get_settings())
        return RedirectResponse(url="/ui/admin/dashboard")
    except AuthRedirectException:
        return RedirectResponse(url="/ui/admin/login")


@router.get("/dashboard")
async def admin_dashboard(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    # Aggregated stats
    total_members = await db.members.count_documents({})
    start_of_day = datetime.utcnow().timestamp() - 86400
    msgs_24h = await db.messages.count_documents({"ts": {"$gte": start_of_day}})
    total_orders = await db.orders.count_documents({})
    status_health = "Online"
    recent_products = await db.products.find().sort("_id", -1).limit(5).to_list(length=5)
    
    # Custom Clusters (Groups)
    custom_clusters = await db.custom_clusters.find({"is_active": True}).sort("created_at", -1).limit(10).to_list(length=10)
    total_clusters = await db.custom_clusters.count_documents({"is_active": True})
    for cluster in custom_clusters:
        cluster["_id"] = str(cluster["_id"])
        owner = await db.members.find_one({"phone": cluster.get("owner_phone")})
        cluster["owner_name"] = owner.get("name") if owner else cluster.get("owner_phone", "Unknown")
        cluster["member_count"] = len(cluster.get("members", []))
        cluster["item_count"] = len(cluster.get("items", []))

    # Product Clusters (Fulfillment Tracking)
    # We find products that HAVE cluster definitions
    product_clusters = []
    managed_products = await db.products.find({"clusters": {"$exists": True, "$not": {"$size": 0}}}).to_list(length=100)
    
    for p in managed_products:
        p["_id"] = str(p["_id"])
        # For each cluster rule in the product
        for rule in p.get("clusters", []):
            # Find active orders (PAID or CONFIRMED) containing this SKU in this city/area
            query = {
                "status": {"$in": ["PAID", "CONFIRMED", "paid"]},
                "items.sku": p["sku"]
            }
            if rule.get("city"):
                query["city"] = rule["city"]
            
            orders = await db.orders.find(query).to_list(length=1000)
            current_units = 0
            for o in orders:
                for item in o.get("items", []):
                    if item["sku"] == p["sku"]:
                        current_units += item.get("qty", 0)
            
            target = rule.get("units_per_cluster") or 10 # Default
            progress = min(100, int((current_units / target) * 100)) if target else 0
            
            product_clusters.append({
                "sku": p["sku"],
                "name": p["name"],
                "image": p.get("image_url"),
                "city": rule.get("city"),
                "area": rule.get("area"),
                "current": current_units,
                "target": target,
                "progress": progress
            })

    # Notifications
    notifications = await db.notifications.find().sort("ts", -1).limit(15).to_list(length=15)
    for n in notifications:
        n["_id"] = str(n["_id"])

    pending_orders = await db.orders.count_documents({"status": {"$in": ["WAITING_PAYMENT", "PAID", "paid"]}})
    paid_members = await db.members.count_documents({"payment_status": "paid"})
    
    stats = {
        "members": total_members,
        "paid_members": paid_members,
        "msgs_24h": msgs_24h,
        "orders": total_orders,
        "pending_orders": pending_orders,
        "health": status_health,
        "total_clusters": total_clusters,
    }
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, 
            "admin": admin, 
            "stats": stats, 
            "recent_products": recent_products,
            "custom_clusters": custom_clusters,
            "product_clusters": product_clusters,
            "notifications": notifications,
            "active": "dashboard"
        },
    )


@router.get("/chats")
async def ui_chats(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
    msg: Optional[str] = None,
):
    pipeline = [
        {"$sort": {"ts": -1}},
        {
            "$group": {
                "_id": "$phone",
                "last": {"$first": "$body"},
                "media_url": {"$first": "$media_url"},
                "ts": {"$first": "$ts"},
                "dir": {"$first": "$direction"},
            }
        },
        {"$sort": {"ts": -1}},
        {"$limit": 100},
    ]
    chats = await db.messages.aggregate(pipeline).to_list(length=100)
    # hydrate names from members if available
    for c in chats:
        member = await db.members.find_one({"phone": c["_id"]})
        c["name"] = member.get("name") if member else None
    return templates.TemplateResponse(
        "chats.html",
        {"request": request, "admin": admin, "chats": chats, "msg": msg},
    )


@router.get("/chat")
async def ui_chat_detail(
    request: Request,
    phone: str,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    normalized = phone.strip()
    if not normalized.startswith("+"):
        normalized_alt = "+" + normalized
    else:
        normalized_alt = normalized.lstrip("+")
    phone_variants = list({normalized, normalized_alt})
    messages = await db.messages.find({"phone": {"$in": phone_variants}}).sort("ts", 1).to_list(length=500)
    member = await db.members.find_one({"phone": {"$in": phone_variants}}) or {}
    return templates.TemplateResponse(
        "chat_detail.html",
        {
            "request": request,
            "admin": admin,
            "phone": normalized,
            "member": member,
            "messages": messages,
        },
    )


@router.post("/chat")
async def ui_chat_send(
    request: Request,
    phone: str = Form(...),
    body: str = Form(...),
    media_url: Optional[str] = Form(None),
    media_file: Optional[UploadFile] = File(None),
    admin: str = Depends(get_current_admin),
    service: WhatsAppService = Depends(get_service),
    settings: Settings = Depends(get_settings),
):
    target_phone = phone if phone.startswith("+") else f"+{phone}"

    file_url = media_url
    if media_file and media_file.filename:
        upload_dir = Path("uploads")
        upload_dir.mkdir(exist_ok=True)
        fname = f"{uuid.uuid4().hex}_{media_file.filename}"
        dest = upload_dir / fname
        content = await media_file.read()
        dest.write_bytes(content)
        base = build_public_base(request, settings)
        file_url = f"{base}/uploads/{fname}"

    await service.send_outbound(target_phone, body, media_url=file_url)
    return RedirectResponse(url=f"/ui/admin/chat?phone={target_phone}", status_code=303)


@router.post("/broadcast-all")
async def ui_broadcast_all(
    request: Request,
    body: str = Form(...),
    media_url: Optional[str] = Form(None),
    media_file: Optional[UploadFile] = File(None),
    admin: str = Depends(get_current_admin),
    service: WhatsAppService = Depends(get_service),
    settings: Settings = Depends(get_settings),
):
    file_url = media_url
    if media_file and media_file.filename:
        upload_dir = Path("uploads")
        upload_dir.mkdir(exist_ok=True)
        fname = f"{uuid.uuid4().hex}_{media_file.filename}"
        dest = upload_dir / fname
        content = await media_file.read()
        dest.write_bytes(content)
        base = build_public_base(request, settings)
        file_url = f"{base}/uploads/{fname}"

    result = await service.broadcast_all_conversed(body, media_url=file_url)
    msg = f"Broadcast sent to {result['sent']} numbers. Errors: {result['errors']}."
    return RedirectResponse(url=f"/ui/admin/chats?msg={msg}", status_code=303)


@router.get("/messages")
async def ui_messages(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(require_db),
):
    cursor = db.messages.find().sort("ts", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "messages.html",
        {"request": request, "admin": admin, "messages": docs},
    )


@router.get("/members")
async def ui_members(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(require_db),
):
    cursor = db.members.find().sort("join_date", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "members.html",
        {"request": request, "admin": admin, "members": docs},
    )


@router.get("/orders")
async def ui_orders(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(require_db),
):
    cursor = db.orders.find().sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "orders.html",
        {"request": request, "admin": admin, "orders": docs, "active": "orders"},
    )


@router.get("/carts")
async def ui_carts(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(100, ge=1, le=500),
    db=Depends(require_db),
):
    cursor = db.carts.find().sort("updated_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "carts.html",
        {"request": request, "admin": admin, "carts": docs, "active": "carts"},
    )


@router.get("/broadcasts")
async def ui_broadcasts(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(require_db),
):
    cursor = db.broadcasts.find().sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "broadcasts.html",
        {"request": request, "admin": admin, "broadcasts": docs},
    )


@router.get("/status")
async def ui_status(
    request: Request,
    admin: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(require_db),
):
    cursor = db.message_status.find().sort("ts", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["_id"] = str(d["_id"])
    return templates.TemplateResponse(
        "status.html",
        {"request": request, "admin": admin, "statuses": docs},
    )


@router.get("/catalogue")
async def ui_catalogue(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
    msg: Optional[str] = None,
    edit: Optional[str] = None,
):
    products = await db.products.find().sort("name", 1).to_list(length=1000)
    for p in products:
        p["_id"] = str(p["_id"])
    edit_product = None
    if edit:
        edit_product = await db.products.find_one({"sku": edit})
        if edit_product:
            edit_product["_id"] = str(edit_product["_id"])
    return templates.TemplateResponse(
        "catalogue.html",
        {
            "request": request,
            "admin": admin,
            "products": products,
            "msg": msg,
            "active": "catalogue",
            "edit_product": edit_product,
        },
    )


@router.get("/clusters")
async def ui_clusters(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    clusters = await db.custom_clusters.find().sort("created_at", -1).to_list(length=200)
    for c in clusters:
        c["_id"] = str(c["_id"])
        owner = await db.members.find_one({"phone": c.get("owner_phone")})
        c["owner_name"] = owner.get("name") if owner else c.get("owner_phone", "Unknown")
        c["member_count"] = len(c.get("members", []))
        c["item_count"] = len(c.get("items", []))
    
    return templates.TemplateResponse(
        "clusters.html",
        {"request": request, "admin": admin, "clusters": clusters, "active": "clusters"},
    )


@router.get("/clusters/{cluster_id}")
async def ui_cluster_detail(
    request: Request,
    cluster_id: str,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    try:
        from bson import ObjectId
        oid = ObjectId(cluster_id)
        cluster = await db.custom_clusters.find_one({"_id": oid})
    except:
        cluster = None
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
        
    cluster["_id"] = str(cluster["_id"])
    owner = await db.members.find_one({"phone": cluster.get("owner_phone")})
    cluster["owner_name"] = owner.get("name") if owner else cluster.get("owner_phone", "Unknown")
    
    # Hydrate members
    members = []
    for phone in cluster.get("members", []):
        m = await db.members.find_one({"phone": phone})
        if m:
            members.append({
                "phone": phone,
                "name": m.get("name", "Unknown"),
                "status": m.get("payment_status", "unpaid")
            })
        else:
            members.append({"phone": phone, "name": "Non-Member", "status": "N/A"})

    # Fetch order history for this cluster
    orders = await db.orders.find({"cluster_id": cluster_id}).sort("created_at", -1).limit(50).to_list(length=50)
    for o in orders:
        o["_id"] = str(o["_id"])
        # Normalize totals and paid tracking
        o["total"] = o.get("total") or 0
        o["paid_kobo"] = o.get("cluster_paid_amount_kobo") or 0
        o["paid_amount"] = o["paid_kobo"] / 100
        o["created_at_fmt"] = o.get("created_at")
            
    return templates.TemplateResponse(
        "cluster_detail.html",
        {"request": request, "admin": admin, "cluster": cluster, "members": members, "orders": orders, "active": "clusters"},
    )


@router.get("/notifications")
async def ui_notifications(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    notifications = await db.notifications.find().sort("ts", -1).limit(100).to_list(length=100)
    for n in notifications:
        n["_id"] = str(n["_id"])
        
    return templates.TemplateResponse(
        "notifications.html",
        {"request": request, "admin": admin, "notifications": notifications, "active": "notifications"},
    )


@router.post("/catalogue")
async def ui_catalogue_upsert(
    request: Request,
    image_file: Optional[UploadFile] = File(None),
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
    settings: Settings = Depends(get_settings),
):
    try:
        form = await request.form()
        sku = (form.get("sku") or "").strip()
        original_sku = (form.get("original_sku") or "").strip()
        name = (form.get("name") or "").strip()
        price = (form.get("price") or "").strip()
        in_stock = form.get("in_stock")

        if not sku or not name or not price:
            raise ValueError("SKU, name, and price are required")

        in_stock_value = bool(in_stock)

        # Handle Image Upload
        image_url = None
        if image_file and image_file.filename:
            upload_dir = Path("uploads")
            upload_dir.mkdir(exist_ok=True)
            fname = f"prod_{uuid.uuid4().hex}_{image_file.filename}"
            dest = upload_dir / fname
            content = await image_file.read()
            dest.write_bytes(content)
            base = build_public_base(request, settings)
            image_url = f"{base}/uploads/{fname}"

        # Parse clusters
        clusters = []
        cities = form.getlist("cluster_city")
        areas = form.getlist("cluster_area")
        people = form.getlist("cluster_people")
        units = form.getlist("cluster_units")
        for idx, city in enumerate(cities):
            city_clean = (city or "").strip()
            area_clean = (areas[idx] if idx < len(areas) else "").strip()
            if not city_clean:
                continue
            try:
                ppl = int(people[idx]) if idx < len(people) and people[idx] else None
            except ValueError:
                ppl = None
            try:
                unit_val = int(units[idx]) if idx < len(units) and units[idx] else None
            except ValueError:
                unit_val = None
            clusters.append(
                {
                    "city": city_clean,
                    "area": area_clean or None,
                    "people_per_cluster": ppl,
                    "units_per_cluster": unit_val,
                }
            )

        product_data = {
            "sku": sku,
            "name": name,
            "price": price,
            "in_stock": in_stock_value,
            "clusters": clusters,
        }
        if image_url:
            product_data["image_url"] = image_url

        # Handle SKU rename
        if original_sku and original_sku != sku:
            # Check if new SKU exists? For now, we assume we can overwrite or it's a new one.
            # Delete old SKU entry
            await db.products.delete_one({"sku": original_sku})

        # Upsert by SKU
        await db.products.update_one(
            {"sku": sku},
            {"$set": product_data},
            upsert=True
        )

        return RedirectResponse(url="/ui/admin/catalogue?msg=Product saved", status_code=303)
    except Exception as exc:
        return RedirectResponse(
            url=f"/ui/admin/catalogue?msg=Upload failed: {exc}",
            status_code=303,
        )


@router.post("/catalogue/delete")
async def ui_catalogue_delete(
    request: Request,
    sku: str = Form(...),
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    await db.products.delete_one({"sku": sku})
    return RedirectResponse(url="/ui/admin/catalogue?msg=Product deleted", status_code=303)


@router.get("/subscriptions")
async def ui_subscriptions(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    members = await db.members.find({"membership_type": {"$exists": True}}).sort("join_date", -1).limit(300).to_list(length=300)
    for m in members:
        m["_id"] = str(m["_id"])
    return templates.TemplateResponse(
        "subscriptions.html",
        {"request": request, "admin": admin, "members": members, "active": "subscriptions"},
    )


@router.post("/subscriptions/approve")
async def ui_subscriptions_approve(
    request: Request,
    phone: str = Form(...),
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
    service: WhatsAppService = Depends(get_service),
):
    normalized = phone if phone.startswith("+") else f"+{phone}"
    await db.members.update_one(
        {"phone": {"$in": [normalized, normalized.lstrip('+')]}},
        {"$set": {"payment_status": "paid", "state": "idle"}},
    )
    try:
        await service.send_outbound(normalized, "Your PNP Lite subscription has been approved. Welcome aboard! Reply PRICE to see this week's deals.")
    except Exception:
        pass
    return RedirectResponse(url="/ui/admin/subscriptions", status_code=303)


@router.post("/subscriptions/update")
async def ui_subscriptions_update(
    request: Request,
    phone: str = Form(...),
    plan: str = Form(...),
    status: str = Form(...),
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
    service: WhatsAppService = Depends(get_service),
):
    normalized = phone if phone.startswith("+") else f"+{phone}"
    
    updates = {
        "membership_type": plan,
        "payment_status": status,
        "updated_at": datetime.utcnow()
    }
    
    if status == "paid":
        updates["state"] = "idle"

    await db.members.update_one(
        {"phone": {"$in": [normalized, normalized.lstrip('+')]}},
        {"$set": updates},
    )
    
    # Notify user of membership update
    msg = f"Your PNP Lite membership has been updated! Plan: *{plan}*, Status: *{status}*."
    if status == "paid":
        msg += "\nYou can now start placing orders."
    
    try:
        await service.send_outbound(normalized, msg)
    except Exception:
        pass

    return RedirectResponse(url="/ui/admin/subscriptions", status_code=303)


@router.get("/orders/{order_id}")
async def ui_order_detail(
    request: Request,
    order_id: str,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    try:
        from bson import ObjectId
        oid = ObjectId(order_id)
        order = await db.orders.find_one({"_id": oid})
    except:
        order = None
    
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    order["_id"] = str(order["_id"])
    member = await db.members.find_one({"phone": order.get("member_phone")})
    
    return templates.TemplateResponse(
        "order_detail.html",
        {"request": request, "admin": admin, "order": order, "member": member, "active": "orders"},
    )


@router.post("/orders/{order_id}/status")
async def ui_order_status(
    request: Request,
    order_id: str,
    status: str = Form(...),
    admin: str = Depends(get_current_admin),
    service: WhatsAppService = Depends(get_service),
    db=Depends(require_db),
):
    try:
        from bson import ObjectId
        oid = ObjectId(order_id)
    except:
        pass # Let standard find fail if invalid
    
    # Update Status
    result = await db.orders.find_one_and_update(
        {"_id": oid},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        return_document=True
    )
    
    if result:
        # NOTIFICATION: Status Update
        from app.services.whatsapp_service import WhatsAppService
        settings = get_settings()
        service_temp = WhatsAppService(db, settings, ai_service=None)
        await service_temp.add_notification(
            type="status",
            message=f"Order #{result.get('slug')} changed to *{status}*",
            metadata={"order_id": order_id, "status": status, "slug": result.get('slug')}
        )
        # Notify User
        phone = result.get("member_phone")
        msg = f"Update for Order #{result.get('slug')}: Status is now *{status}*."
        if status == "CONFIRMED":
             msg += " We are processing your items."
        elif status == "DISPATCHED":
             msg += " Your order is on the way!"
        elif status == "DELIVERED":
             msg += " Enjoy your order!"
             
        try:
             await service.send_outbound(phone, msg)
        except:
             pass

    return RedirectResponse(url=f"/ui/admin/orders/{order_id}", status_code=303)


@router.get("/bot-responses")
async def ui_bot_responses(
    request: Request,
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    """Admin page to edit bot responses and system prompts."""
    system_prompt = await db.config.find_one({"_id": "bot_system_prompt"})
    prompt_value = system_prompt.get("value") if system_prompt else None
    
    return templates.TemplateResponse(
        "bot_responses.html",
        {
            "request": request,
            "admin": admin,
            "system_prompt": prompt_value,
            "active": "bot_responses"
        }
    )


@router.post("/bot-responses")
async def ui_bot_responses_save(
    request: Request,
    system_prompt: str = Form(...),
    admin: str = Depends(get_current_admin),
    db=Depends(require_db),
):
    """Save bot system prompt configuration."""
    await db.config.update_one(
        {"_id": "bot_system_prompt"},
        {"$set": {"value": system_prompt, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return RedirectResponse(url="/ui/admin/bot-responses?msg=Bot responses updated", status_code=303)
