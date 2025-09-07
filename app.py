import os, json, uuid, base64
from aiohttp import web, ClientSession
from dotenv import load_dotenv
from auth import validate_init_data

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")
OWNER_ID    = int(os.getenv("OWNER_ID", "0"))

YK_SHOP_ID     = os.getenv("YK_SHOP_ID")
YK_SECRET_KEY  = os.getenv("YK_SECRET_KEY")

# ===== In-memory (демо). В проде замени на БД.
USERS: dict[int, dict] = {}        # uid -> {"gems": int, "premium_items": set()}
PAYMENTS: dict[str, dict] = {}     # payment_id -> {"status": "pending|succeeded", "uid": int, "gems": int, "pack": str}

def get_user(uid: int):
    u = USERS.get(uid)
    if not u:
        u = {"gems": 0, "premium_items": set()}
        USERS[uid] = u
    return u

# Пакеты (рубли -> gems)
PACKS = {
    "gems_100": {"rub": 99,  "gems": 100, "title":"100 Gems"},
    "gems_300": {"rub": 249, "gems": 320, "title":"300 Gems (+20)"},
    "gems_600": {"rub": 449, "gems": 660, "title":"600 Gems (+60)"},
}

# Премиум-товары (цены в Gems)
PREMIUM_ITEMS = {
    "aura_neon":   80,
    "skin_dragon": 120,
}

def parse_uid(pairs) -> int | None:
    try:
        user_json = pairs.get("user")
        if not user_json: return None
        return json.loads(user_json).get("id")
    except Exception:
        return None

# ===== CORS
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp

def yk_auth_header():
    token = f"{YK_SHOP_ID}:{YK_SECRET_KEY}".encode()
    b64 = base64.b64encode(token).decode()
    return {"Authorization": f"Basic {b64}"}

async def yk_create_payment(amount_rub: int, description: str, metadata: dict, return_url: str):
    url = "https://api.yookassa.ru/v3/payments"
    idempotence_key = str(uuid.uuid4())
    headers = {"Idempotence-Key": idempotence_key, "Content-Type": "application/json", **yk_auth_header()}
    payload = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "capture": True,
        "description": description,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "metadata": metadata
    }
    async with ClientSession() as s:
        async with s.post(url, headers=headers, json=payload) as r:
            data = await r.json()
            if r.status >= 400:
                raise web.HTTPBadRequest(text=json.dumps(data))
            return data

# ===== API
async def api_profile(request: web.Request):
    init = request.headers.get("X-Telegram-Init-Data","")
    pairs = validate_init_data(init, BOT_TOKEN)
    if not pairs: return web.json_response({"ok":False,"error":"invalid init_data"}, status=401)
    uid = parse_uid(pairs)
    if not uid: return web.json_response({"ok":False,"error":"no user"}, status=400)
    u = get_user(uid)
    return web.json_response({"ok":True,"gems":u["gems"],"premium_items": list(u["premium_items"]), "is_owner": (uid==OWNER_ID)})

async def api_buy_premium(request: web.Request):
    init = request.headers.get("X-Telegram-Init-Data","")
    pairs = validate_init_data(init, BOT_TOKEN)
    if not pairs: return web.json_response({"ok":False,"error":"invalid init_data"}, status=401)
    uid = parse_uid(pairs)
    if not uid: return web.json_response({"ok":False,"error":"no user"}, status=400)
    body = await request.json()
    item_id = body.get("itemId")
    cost = PREMIUM_ITEMS.get(item_id)
    if not cost: return web.json_response({"ok":False,"error":"unknown item"}, status=400)
    u = get_user(uid)
    if u["gems"] < cost:
        return web.json_response({"ok":False,"error":"not_enough_gems"}, status=200)
    u["gems"] -= cost
    u["premium_items"].add(item_id)
    return web.json_response({"ok":True,"gems":u["gems"],"premium_items": list(u["premium_items"])})

async def api_admin_grant_gems(request: web.Request):
    init = request.headers.get("X-Telegram-Init-Data","")
    pairs = validate_init_data(init, BOT_TOKEN)
    if not pairs: return web.json_response({"ok":False,"error":"invalid init_data"}, status=401)
    uid = parse_uid(pairs)
    if not uid: return web.json_response({"ok":False,"error":"no user"}, status=400)
    if uid != OWNER_ID:
        return web.json_response({"ok":False,"error":"forbidden"}, status=403)
    body = await request.json()
    amount = int(body.get("amount", 0))
    if amount <= 0 or amount > 10000:
        return web.json_response({"ok":False,"error":"bad_amount"}, status=400)
    u = get_user(uid)
    u["gems"] += amount
    return web.json_response({"ok":True, "gems": u["gems"]})

async def api_create_yk_payment(request: web.Request):
    init = request.headers.get("X-Telegram-Init-Data","")
    pairs = validate_init_data(init, BOT_TOKEN)
    if not pairs: return web.json_response({"ok":False,"error":"invalid init_data"}, status=401)
    uid = parse_uid(pairs)
    if not uid: return web.json_response({"ok":False,"error":"no user"}, status=400)
    body = await request.json()
    pack_id = body.get("pack","gems_100")
    pack = PACKS.get(pack_id)
    if not pack: return web.json_response({"ok":False,"error":"unknown pack"}, status=400)
    if not (YK_SHOP_ID and YK_SECRET_KEY):
        return web.json_response({"ok":False,"error":"yookassa_not_configured"}, status=500)
    metadata = {"uid": uid, "pack_id": pack_id}
    return_url = f"{PUBLIC_BASE}/thankyou" if PUBLIC_BASE else "https://t.me"
    data = await yk_create_payment(pack["rub"], f"Tamagocho: {pack['title']}", metadata, return_url)
    payment_id = data["id"]
    confirmation_url = data["confirmation"]["confirmation_url"]
    PAYMENTS[payment_id] = {"status":"pending", "uid":uid, "gems":pack["gems"], "pack":pack_id}
    return web.json_response({"ok":True, "payment_id": payment_id, "confirmation_url": confirmation_url})

async def api_check_payment(request: web.Request):
    body = await request.json()
    pid = body.get("payment_id")
    if not pid: return web.json_response({"ok":False,"error":"no_payment_id"}, status=400)
    info = PAYMENTS.get(pid)
    if not info: return web.json_response({"ok":False,"error":"not_found"}, status=404)
    u = get_user(info["uid"])
    return web.json_response({"ok":True,"status":info["status"],"gems":u["gems"]})

async def yk_webhook(request: web.Request):
    data = await request.json()
    if data.get("event") == "payment.succeeded":
        obj = data.get("object", {})
        pid = obj.get("id")
        meta = obj.get("metadata", {}) or {}
        uid = int(meta.get("uid", 0)) if meta.get("uid") else 0
        pack_id = meta.get("pack_id")
        pay = PAYMENTS.get(pid)
        if pay:
            pay["status"] = "succeeded"
            u = get_user(pay["uid"])
            u["gems"] += pay["gems"]
        else:
            if uid and pack_id in PACKS:
                u = get_user(uid)
                u["gems"] += PACKS[pack_id]["gems"]
                PAYMENTS[pid] = {"status":"succeeded","uid":uid,"gems":PACKS[pack_id]["gems"],"pack":pack_id}
    return web.json_response({"ok":True})

def build_app():
    app = web.Application(middlewares=[cors_mw])
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/buy-premium", api_buy_premium)
    app.router.add_post("/api/admin/grant-gems", api_admin_grant_gems)
    app.router.add_post("/api/create-yookassa-payment", api_create_yk_payment)
    app.router.add_post("/api/check-payment", api_check_payment)
    app.router.add_post("/yookassa/webhook", yk_webhook)
    async def thanks(req): return web.Response(text="Спасибо! Вернитесь в приложение Telegram.")
    app.router.add_get("/thankyou", thanks)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
