from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Query, Header, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import io
import time
import logging
import asyncio
import random
import uuid
from collections import defaultdict
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env', override=True)

# Configure Playwright browsers path BEFORE importing playwright
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/pw-browsers"))

from security import encrypt_secret, decrypt_secret
from auth import hash_password, verify_password, create_token, decode_token, bearer_from_header
from adapters import get_adapter

# Playwright is imported lazily inside extraction to keep app startup snappy
_playwright = None
_browser_install_lock = asyncio.Lock()


# --- Screenshot directory ---
SCREENSHOTS_DIR = Path(os.environ.get("SCREENSHOTS_DIR", str(ROOT_DIR / "data/screenshots")))
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
RETENTION_DAYS = int(os.environ.get("SCREENSHOT_RETENTION_DAYS", "7"))

# --- DB setup ---
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# --- App ---
app = FastAPI(title="PharmaScrape API")
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("server")


# ============================================================
# MODELS
# ============================================================
class Portal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    baseUrl: str
    status: str = "ACTIVE"
    description: Optional[str] = ""


class PortalCreate(BaseModel):
    name: str
    baseUrl: str
    status: str = "ACTIVE"
    description: Optional[str] = ""


class Distributor(BaseModel):
    """Distributor with credentials. Password never returned in responses."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    url: str
    portal: str                 # e.g. "SUNSHOP" | "CHETHANA" | "VARDHAMAN"
    portalType: str = "GENERIC" # adapter to use — SUNSHOP | GENERIC etc.
    location: Optional[str] = None
    username: Optional[str] = None
    hasCredentials: bool = False
    selected: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DistributorCreate(BaseModel):
    name: str
    url: str
    portal: str
    portalType: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    selected: bool = True


class DistributorUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    portal: Optional[str] = None
    portalType: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    selected: Optional[bool] = None


class BulkSelect(BaseModel):
    selected: bool


class ExtractRequest(BaseModel):
    product: str
    quantity: Optional[int] = None
    target_ids: List[str]


class TestLoginResponse(BaseModel):
    ok: bool
    detail: str
    screenshot: Optional[str] = None


class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    name: Optional[str] = None
    isAdmin: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class LoginResponse(BaseModel):
    token: str
    user: User


# ============================================================
# HELPERS
# ============================================================
def strip_mongo(doc: dict) -> dict:
    if not doc:
        return doc
    doc.pop("_id", None)
    doc.pop("encryptedPassword", None)  # never expose
    return doc


def infer_portal_type(portal: str) -> str:
    p = (portal or "").upper()
    if "SUNSHOP" in p:
        return "SUNSHOP"
    if "CHETHANA" in p or "CHIRAG" in p:
        return "CHETHANA"
    if "LIVECONNECT" in p:
        return "LIVECONNECT"
    if "VARDHAMAN" in p or "EASYSOL" in p:
        return "VARDHAMAN"
    if "RETAILIO" in p:
        return "RETAILIO"
    if "YASHIKA" in p:
        return "YASHIKA"
    if "MARG" in p:
        return "MARG"
    return "GENERIC"


async def _get_browser():
    """Launch a shared Playwright browser instance. Auto-recovers if the
    Chromium executable was wiped between sessions. Uses a persistent
    PLAYWRIGHT_BROWSERS_PATH (see .env) so browsers survive across
    ephemeral container resets."""
    global _playwright
    import sys
    from playwright.async_api import async_playwright
    if _playwright is None:
        _playwright = await async_playwright().start()

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]

    async with _browser_install_lock:
        # 1) Try Playwright's bundled chromium first
        try:
            return await _playwright.chromium.launch(headless=True, args=launch_args)
        except Exception as e:
            err = str(e)
            logger.warning(f"Bundled chromium launch failed: {err[:200]}")

        # 2) Try system chromium (persists across environment resets)
        for candidate in ("/root/chromium-persistent/chromium", "/usr/bin/chromium", "/root/bin/chromium"):
            if os.path.exists(candidate):
                try:
                    logger.info(f"Falling back to system chromium at {candidate}")
                    return await _playwright.chromium.launch(
                        headless=True,
                        executable_path=candidate,
                        args=launch_args,
                    )
                except Exception as e2:
                    logger.warning(f"System chromium at {candidate} failed: {str(e2)[:200]}")

        # 3) Last resort — install playwright chromium on the fly using
        # the current python interpreter (so PATH issues don't matter).
        logger.warning("All chromium candidates failed — running `python -m playwright install chromium`...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        logger.info(f"playwright install exit={proc.returncode} stdout={(stdout or b'')[:200]!r} stderr={(stderr or b'')[:200]!r}")
        return await _playwright.chromium.launch(headless=True, args=launch_args)


async def _cleanup_old_screenshots():
    """Delete screenshots older than RETENTION_DAYS."""
    try:
        cutoff = datetime.now().timestamp() - RETENTION_DAYS * 86400
        removed = 0
        for f in SCREENSHOTS_DIR.glob("*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info(f"Cleaned up {removed} old screenshot(s)")
    except Exception as e:
        logger.warning(f"Screenshot cleanup failed: {e}")


async def seed_if_empty():
    """Seed portals + distributors + users if collections are empty."""
    if await db.users.count_documents({}) == 0:
        seed_users = [
            ("shubhada", "2612", "Shubhada"),
            ("manju", "6387", "Manju"),
            ("abhishek", "5555", "Abhishek"),
            ("narendra", "6666", "Narendra"),
        ]
        docs = []
        for username, password, name in seed_users:
            u = User(username=username, name=name, isAdmin=True)
            d = u.dict()
            d["hashedPassword"] = hash_password(password)
            docs.append(d)
        await db.users.insert_many(docs)
        try:
            await db.users.create_index("username", unique=True)
        except Exception:
            pass
        logger.info(f"Seeded {len(seed_users)} users")

    if await db.portals.count_documents({}) == 0:
        portals = [
            Portal(name="SUNSHOP", baseUrl="https://www.sunshop.co.in", status="ACTIVE", description="Sunshop portal — supports real login + scrape"),
            Portal(name="CHETHANA", baseUrl="http://www.chethanapharma.in", status="ACTIVE", description="Chethana Pharma portal (adapter pending)"),
            Portal(name="VARDHAMAN", baseUrl="http://easysol.co.in", status="ACTIVE", description="Vardhaman medisales portal (adapter pending)"),
            Portal(name="MEDPLUS", baseUrl="https://medplus.in", status="INACTIVE", description="MedPlus wholesale portal"),
            Portal(name="APOLLO", baseUrl="https://apollo.co.in", status="ACTIVE", description="Apollo pharmacy portal"),
            Portal(name="MARG", baseUrl="https://margcompusoft.com/eRetail", status="ACTIVE", description="Marg eRetail aggregator — OTP session"),
        ]
        await db.portals.insert_many([p.dict() for p in portals])
        logger.info("Seeded portals")

    if await db.targets.count_documents({}) == 0:
        seeds = [
            {"name": "SAROJ PHARMA", "url": "https://www.sunshop.co.in", "portal": "SUNSHOP", "portalType": "SUNSHOP"},
            {"name": "HEGDE BROTHER", "url": "https://www.sunshop.co.in", "portal": "SUNSHOP", "portalType": "SUNSHOP"},
            {"name": "KAPILA PHARMA", "url": "https://www.sunshop.co.in", "portal": "SUNSHOP", "portalType": "SUNSHOP"},
            {"name": "KAPILA MEDICAL AGENCIES", "url": "https://www.sunshop.co.in", "portal": "SUNSHOP", "portalType": "SUNSHOP"},
            {"name": "CHIRAG PHARMA", "url": "http://www.chethanapharma.in", "portal": "CHETHANA", "portalType": "CHETHANA"},
            {"name": "VARDHAMAN MEDISALES PVT LTD", "url": "http://easysol.co.in", "portal": "VARDHAMAN", "portalType": "VARDHAMAN"},
            {"name": "RETAILIO", "url": "https://order.retailio.in/rio/secure-login", "portal": "RETAILIO", "portalType": "RETAILIO"},
        ]
        docs = []
        for s in seeds:
            d = Distributor(**s, selected=True, hasCredentials=False)
            docs.append(d.dict())
        await db.targets.insert_many(docs)
        logger.info("Seeded distributors")


# ============================================================
# AUTHENTICATION
# ============================================================
# In-memory login rate limiter (per-process; resets on restart). Keyed by both
# client IP and username so a single account can't be brute-forced from many
# IPs, and a single IP can't spray many usernames.
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5
_failed_logins: Dict[str, List[float]] = defaultdict(list)


def _login_rate_limit_keys(request: Request, username: str) -> List[str]:
    # Behind Cloudflare Tunnel, request.client.host is always 127.0.0.1 (the
    # tunnel connects locally) — use the real visitor IP Cloudflare forwards
    # instead, falling back through the usual proxy headers, then the raw
    # socket. Trusting these headers is safe here because the only thing that
    # can reach this port at all is cloudflared / a local caller.
    ip = (
        request.headers.get("cf-connecting-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return [f"ip:{ip}", f"user:{username}"]


def _check_login_rate_limit(request: Request, username: str) -> None:
    now = time.time()
    for key in _login_rate_limit_keys(request, username):
        attempts = _failed_logins[key]
        attempts[:] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            raise HTTPException(429, "Too many failed login attempts. Try again in a few minutes.")


def _record_failed_login(request: Request, username: str) -> None:
    now = time.time()
    for key in _login_rate_limit_keys(request, username):
        _failed_logins[key].append(now)


def _clear_failed_logins(request: Request, username: str) -> None:
    for key in _login_rate_limit_keys(request, username):
        _failed_logins.pop(key, None)


async def _get_user_by_username(username: str) -> Optional[dict]:
    return await db.users.find_one({"username": username.lower()})


async def _current_user_from_request(request) -> Optional[dict]:
    """Extract & validate the JWT from the Authorization header. Return user dict or None."""
    token = bearer_from_header(request.headers.get("authorization"))
    if not token:
        return None
    try:
        payload = decode_token(token)
    except HTTPException:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = await db.users.find_one({"id": user_id})
    return user


@api_router.post("/auth/login", response_model=LoginResponse)
async def auth_login(payload: LoginRequest, request: Request):
    username = payload.username.strip().lower()
    _check_login_rate_limit(request, username)
    user = await _get_user_by_username(username)
    if not user or not verify_password(payload.password, user.get("hashedPassword", "")):
        _record_failed_login(request, username)
        raise HTTPException(401, "Invalid username or password")
    _clear_failed_logins(request, username)
    token = create_token(user["id"], user["username"])
    return LoginResponse(token=token, user=User(**{k: v for k, v in user.items() if k not in ("_id", "hashedPassword")}))


@api_router.get("/auth/me", response_model=User)
async def auth_me(authorization: Optional[str] = Header(None)):
    token = bearer_from_header(authorization)
    if not token:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(token)
    user = await db.users.find_one({"id": payload.get("sub")})
    if not user:
        raise HTTPException(401, "User no longer exists")
    return User(**{k: v for k, v in user.items() if k not in ("_id", "hashedPassword")})


@api_router.post("/auth/change-password")
async def auth_change_password(payload: ChangePasswordRequest, authorization: Optional[str] = Header(None)):
    token = bearer_from_header(authorization)
    if not token:
        raise HTTPException(401, "Not authenticated")
    p = decode_token(token)
    user = await db.users.find_one({"id": p.get("sub")})
    if not user:
        raise HTTPException(401, "User no longer exists")
    if not verify_password(payload.current_password, user.get("hashedPassword", "")):
        raise HTTPException(400, "Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(400, "New password too short (min 8 chars)")
    await db.users.update_one({"id": user["id"]}, {"$set": {"hashedPassword": hash_password(payload.new_password)}})
    return {"ok": True}


# ============================================================
# PORTALS
# ============================================================
@api_router.get("/portals", response_model=List[Portal])
async def list_portals():
    docs = await db.portals.find().to_list(1000)
    return [Portal(**strip_mongo(d)) for d in docs]


@api_router.post("/portals", response_model=Portal)
async def create_portal(payload: PortalCreate):
    p = Portal(**payload.dict())
    await db.portals.insert_one(p.dict())
    return p


# ============================================================
# DISTRIBUTORS (stored in `targets` collection for backwards compat)
# ============================================================
@api_router.get("/targets", response_model=List[Distributor])
async def list_distributors():
    docs = await db.targets.find().sort("created_at", 1).to_list(1000)
    out = []
    for d in docs:
        d = strip_mongo(d)
        d.setdefault("portalType", infer_portal_type(d.get("portal", "")))
        d.setdefault("hasCredentials", False)
        out.append(Distributor(**d))
    return out


@api_router.post("/targets", response_model=Distributor)
async def create_distributor(payload: DistributorCreate):
    data = payload.dict()
    pwd = data.pop("password", None)
    portal_type = data.get("portalType") or infer_portal_type(data.get("portal", ""))
    dist = Distributor(**{
        "name": data["name"],
        "url": data["url"],
        "portal": data["portal"],
        "portalType": portal_type,
        "username": data.get("username"),
        "selected": data.get("selected", True),
        "hasCredentials": bool(pwd),
    })
    to_store = dist.dict()
    if pwd:
        to_store["encryptedPassword"] = encrypt_secret(pwd)
    await db.targets.insert_one(to_store)
    return dist


@api_router.patch("/targets/{tid}", response_model=Distributor)
async def update_distributor(tid: str, payload: DistributorUpdate):
    raw = {k: v for k, v in payload.dict().items() if v is not None}
    if not raw:
        raise HTTPException(400, "No fields to update")
    updates: Dict[str, Any] = {}
    for k, v in raw.items():
        if k == "password":
            updates["encryptedPassword"] = encrypt_secret(v)
            updates["hasCredentials"] = True
        else:
            updates[k] = v
    # Re-derive portalType if portal changed but portalType not provided
    if "portal" in updates and "portalType" not in updates:
        updates["portalType"] = infer_portal_type(updates["portal"])
    doc = await db.targets.find_one_and_update({"id": tid}, {"$set": updates}, return_document=True)
    if not doc:
        raise HTTPException(404, "Distributor not found")
    doc = strip_mongo(doc)
    doc.setdefault("portalType", infer_portal_type(doc.get("portal", "")))
    doc.setdefault("hasCredentials", False)
    return Distributor(**doc)


@api_router.delete("/targets/{tid}")
async def delete_distributor(tid: str):
    res = await db.targets.delete_one({"id": tid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Distributor not found")
    return {"ok": True, "id": tid}


@api_router.post("/targets/bulk-select")
async def bulk_select(payload: BulkSelect):
    res = await db.targets.update_many({}, {"$set": {"selected": payload.selected}})
    return {"ok": True, "matched": res.matched_count, "modified": res.modified_count}


# ============================================================
# TEST LOGIN
# ============================================================
@api_router.post("/targets/{tid}/test-login", response_model=TestLoginResponse)
async def test_login(tid: str):
    doc = await db.targets.find_one({"id": tid})
    if not doc:
        raise HTTPException(404, "Distributor not found")
    if not doc.get("encryptedPassword") or not doc.get("username"):
        return TestLoginResponse(ok=False, detail="Credentials not set for this distributor")

    username = doc["username"]
    try:
        password = decrypt_secret(doc["encryptedPassword"])
    except Exception as e:
        return TestLoginResponse(ok=False, detail=f"Password decrypt failed: {e}")

    url = doc["url"]
    portal_type = doc.get("portalType") or infer_portal_type(doc.get("portal", ""))

    filename = f"testlogin_{tid}_{uuid.uuid4().hex[:8]}.png"
    async def _shot(page, tag):
        p = SCREENSHOTS_DIR / f"testlogin_{tid}_{tag}_{uuid.uuid4().hex[:6]}.png"
        try:
            await page.screenshot(path=str(p), full_page=False)
            return p.name
        except Exception:
            return None

    browser = None
    try:
        browser = await _get_browser()
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
            extra_http_headers={
                "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = await ctx.new_page()
        adapter = get_adapter(portal_type)
        adapter.screenshotter = _shot
        ok, detail = await adapter.test_login(page, url, username, password)
        shot = await _shot(page, "final")
        await ctx.close()
        return TestLoginResponse(ok=ok, detail=detail, screenshot=shot)
    except Exception as e:
        return TestLoginResponse(ok=False, detail=f"{e.__class__.__name__}: {e}")
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass


async def _run_one_distributor(browser, doc, product_upper: str, qty: Optional[int], entry_id: str, liveconnect_cookies=None, retailio_cookies=None, retailio_local_storage=None, marg_cookies=None, force_candidate_name: Optional[str] = None) -> Dict[str, Any]:
    """Run a single distributor extraction and return a result dict (used by
    both /extract and /extract/manual-pick)."""
    tid = doc["id"]
    name = doc["name"]
    portal = doc.get("portal", "")
    portal_type = doc.get("portalType") or infer_portal_type(portal)
    url = doc["url"]

    base = {
        "targetId": tid,
        "targetName": name,
        "portal": portal,
        "portalType": portal_type,
        "url": url,
        "product": product_upper,
    }
    empty_result = {"status": "ERROR", "detail": "", "items": [], "requestedQty": qty, "canFulfill": None, "loginScreenshot": None, "searchScreenshot": None, "resultsScreenshot": None, "debug": {}}

    if portal_type == "LIVECONNECT":
        if not liveconnect_cookies:
            return {**base, **{**empty_result, "status": "LOGIN_FAILED", "detail": "SESSION_EXPIRED — please authenticate via LIVECONNECT SESSION menu"}}
        password = None
    elif portal_type == "RETAILIO":
        if not retailio_cookies:
            return {**base, **{**empty_result, "status": "LOGIN_FAILED", "detail": "SESSION_EXPIRED — please authenticate via RETAILIO SESSION menu"}}
        password = None
    elif portal_type == "MARG":
        if not marg_cookies:
            return {**base, **{**empty_result, "status": "LOGIN_FAILED", "detail": "SESSION_EXPIRED — please authenticate via MARG SESSION menu"}}
        password = None
    elif not doc.get("username") or not doc.get("encryptedPassword"):
        return {**base, **{**empty_result, "status": "LOGIN_FAILED", "detail": "Credentials not set. Edit distributor to add username/password."}}
    else:
        try:
            password = decrypt_secret(doc["encryptedPassword"])
        except Exception as e:
            return {**base, **{**empty_result, "status": "ERROR", "detail": f"Password decrypt failed: {e}"}}

    async def _shot(page, tag):
        p = SCREENSHOTS_DIR / f"{entry_id}_{tid}_{tag}_{uuid.uuid4().hex[:6]}.png"
        try:
            await page.screenshot(path=str(p), full_page=False)
            return p.name
        except Exception:
            return None

    ctx = None
    try:
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
            extra_http_headers={
                "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = await ctx.new_page()
        adapter = get_adapter(
            portal_type,
            liveconnect_cookies=liveconnect_cookies,
            retailio_cookies=retailio_cookies,
            retailio_local_storage=retailio_local_storage,
            marg_cookies=marg_cookies,
        )
        adapter.screenshotter = _shot
        outcome = await adapter.extract(page, url, doc.get("username") or "", password or "", product_upper, qty or 0, distributor_name=name, force_candidate_name=force_candidate_name)
        return {**base, **outcome.to_dict()}
    except Exception as e:
        return {**base, **{**empty_result, "status": "ERROR", "detail": f"{e.__class__.__name__}: {e}"}}
    finally:
        if ctx:
            try: await ctx.close()
            except Exception: pass


# ============================================================
# EXTRACTION (REAL — Playwright + adapters)
# ============================================================
# In-memory task store (survives process lifetime). Each task holds:
#   { status: 'running' | 'done', progress: int, result: entry-dict|None }
_extract_tasks: dict = {}


async def _run_extraction(task_id: str, payload: "ExtractRequest") -> None:
    """Background worker: runs the Playwright extraction and stores the final
    history-entry dict under _extract_tasks[task_id]['result']. All errors
    are trapped so the poll endpoint can always return a clean payload."""
    try:
        docs = await db.targets.find({"id": {"$in": payload.target_ids}}).to_list(1000)
        if not docs:
            _extract_tasks[task_id] = {"status": "done", "result": None, "error": "No matching distributors"}
            return

        entry_id = str(uuid.uuid4())
        product_upper = payload.product.upper().strip()
        qty = payload.quantity
        start_ts = datetime.utcnow()

        # Preload session cookies (LIVECONNECT / RETAILIO / MARG)
        liveconnect_cookies = None
        if any(d.get("portalType") == "LIVECONNECT" or infer_portal_type(d.get("portal", "")) == "LIVECONNECT" for d in docs):
            try:
                lc_doc = await db.liveconnect_session.find_one({"_id": "default"})
                liveconnect_cookies = (lc_doc or {}).get("cookies")
            except Exception:
                liveconnect_cookies = None
        retailio_cookies = None
        retailio_local_storage = None
        if any(d.get("portalType") == "RETAILIO" or infer_portal_type(d.get("portal", "")) == "RETAILIO" for d in docs):
            try:
                r_doc = await db.retailio_session.find_one({"_id": "default"})
                retailio_cookies = (r_doc or {}).get("cookies")
                retailio_local_storage = (r_doc or {}).get("localStorage")
            except Exception:
                retailio_cookies = None
        marg_cookies = None
        if any(d.get("portalType") == "MARG" or infer_portal_type(d.get("portal", "")) == "MARG" for d in docs):
            try:
                m_doc = await db.marg_session.find_one({"_id": "default"})
                marg_cookies = (m_doc or {}).get("cookies")
            except Exception:
                marg_cookies = None

        browser = None
        results: List[Dict[str, Any]] = []
        try:
            browser = await _get_browser()
            sem = asyncio.Semaphore(10)

            async def _guarded(d):
                async with sem:
                    return await _run_one_distributor(
                        browser, d, product_upper, qty, entry_id,
                        liveconnect_cookies=liveconnect_cookies,
                        retailio_cookies=retailio_cookies,
                        retailio_local_storage=retailio_local_storage,
                        marg_cookies=marg_cookies,
                    )

            results = await asyncio.gather(*[_guarded(d) for d in docs])
        finally:
            if browser:
                try: await browser.close()
                except Exception: pass

        elapsed = (datetime.utcnow() - start_ts).total_seconds()
        success = sum(1 for r in results if r["status"] == "SUCCESS")
        not_found = sum(1 for r in results if r["status"] == "NOT_FOUND")
        login_failed = sum(1 for r in results if r["status"] == "LOGIN_FAILED")
        errors = sum(1 for r in results if r["status"] == "ERROR")

        entry = {
            "id": entry_id,
            "product": product_upper,
            "quantity": qty,
            "timestamp": start_ts,
            "duration": f"{elapsed:.1f}s",
            "targetsRun": len(results),
            "found": success,
            "notFound": not_found,
            "loginFailed": login_failed,
            "errors": errors,
            "outOfStock": not_found,
            "status": "COMPLETED" if errors == 0 and login_failed == 0 else "PARTIAL",
            "results": results,
        }
        await db.history.insert_one(entry)
        entry.pop("_id", None)
        _extract_tasks[task_id] = {"status": "done", "result": entry}
    except Exception as e:
        _extract_tasks[task_id] = {"status": "done", "result": None, "error": f"{e.__class__.__name__}: {e}"}


@api_router.post("/extract")
async def run_extraction(payload: ExtractRequest):
    """Fire-and-poll extraction. Returns { task_id } immediately; the frontend
    polls /api/extract/status/{task_id} until status == 'done'. This bypasses
    Cloudflare's ~100s edge timeout on synchronous requests."""
    if not payload.product.strip():
        raise HTTPException(400, "Product name is required")
    if not payload.target_ids:
        raise HTTPException(400, "At least one distributor is required")
    task_id = uuid.uuid4().hex
    _extract_tasks[task_id] = {"status": "running", "result": None}
    asyncio.create_task(_run_extraction(task_id, payload))
    return {"task_id": task_id, "status": "running"}


@api_router.get("/extract/status/{task_id}")
async def extract_status(task_id: str):
    t = _extract_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Unknown task_id (server may have restarted)")
    if t["status"] == "running":
        return {"status": "running"}
    # Done — return the result (either entry-dict or an error string)
    if t.get("error"):
        return {"status": "done", "error": t["error"]}
    return {"status": "done", "result": t.get("result")}


# ---------- Manual pick: rerun one distributor with a forced candidate ----------
class ManualPickRequest(BaseModel):
    history_id: str
    target_id: str
    candidate_name: str


@api_router.post("/extract/manual-pick")
async def extract_manual_pick(payload: ManualPickRequest):
    hist = await db.history.find_one({"id": payload.history_id})
    if not hist:
        raise HTTPException(404, "History entry not found")
    doc = await db.targets.find_one({"id": payload.target_id})
    if not doc:
        raise HTTPException(404, "Distributor not found")
    product_upper = hist["product"]
    qty = hist.get("quantity")

    liveconnect_cookies = None
    retailio_cookies = None
    retailio_local_storage = None
    marg_cookies = None
    ptype = doc.get("portalType") or infer_portal_type(doc.get("portal", ""))
    if ptype == "LIVECONNECT":
        try:
            lc_doc = await db.liveconnect_session.find_one({"_id": "default"})
            liveconnect_cookies = (lc_doc or {}).get("cookies")
        except Exception:
            pass
    if ptype == "RETAILIO":
        try:
            r_doc = await db.retailio_session.find_one({"_id": "default"})
            retailio_cookies = (r_doc or {}).get("cookies")
            retailio_local_storage = (r_doc or {}).get("localStorage")
        except Exception:
            pass
    if ptype == "MARG":
        try:
            m_doc = await db.marg_session.find_one({"_id": "default"})
            marg_cookies = (m_doc or {}).get("cookies")
        except Exception:
            pass

    browser = None
    try:
        browser = await _get_browser()
        new_result = await _run_one_distributor(
            browser, doc, product_upper, qty, payload.history_id,
            liveconnect_cookies=liveconnect_cookies,
            retailio_cookies=retailio_cookies,
            retailio_local_storage=retailio_local_storage,
            marg_cookies=marg_cookies,
            force_candidate_name=payload.candidate_name,
        )
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass

    # Merge the new result back into the history entry (replace the old row)
    updated_results = []
    replaced = False
    for r in hist.get("results", []):
        if r.get("targetId") == payload.target_id:
            updated_results.append(new_result)
            replaced = True
        else:
            updated_results.append(r)
    if not replaced:
        updated_results.append(new_result)

    # Recount tallies
    success = sum(1 for r in updated_results if r["status"] == "SUCCESS")
    not_found = sum(1 for r in updated_results if r["status"] == "NOT_FOUND")
    login_failed = sum(1 for r in updated_results if r["status"] == "LOGIN_FAILED")
    errors = sum(1 for r in updated_results if r["status"] == "ERROR")

    await db.history.update_one(
        {"id": payload.history_id},
        {"$set": {
            "results": updated_results,
            "found": success,
            "notFound": not_found,
            "loginFailed": login_failed,
            "errors": errors,
            "outOfStock": not_found,
            "status": "COMPLETED" if errors == 0 and login_failed == 0 else "PARTIAL",
        }},
    )
    return {"result": new_result}


# ============================================================
# HISTORY
# ============================================================
@api_router.get("/history")
async def list_history():
    docs = await db.history.find().sort("timestamp", -1).to_list(1000)
    out = []
    for d in docs:
        d = strip_mongo(d)
        d.setdefault("results", [])
        d.setdefault("errors", 0)
        d.setdefault("quantity", None)
        out.append(d)
    return out


@api_router.get("/history/{entry_id}")
async def get_history(entry_id: str):
    doc = await db.history.find_one({"id": entry_id})
    if not doc:
        raise HTTPException(404, "Not found")
    doc = strip_mongo(doc)
    doc.setdefault("results", [])
    doc.setdefault("errors", 0)
    doc.setdefault("quantity", None)
    return doc


@api_router.delete("/history/{entry_id}")
async def delete_history(entry_id: str):
    res = await db.history.delete_one({"id": entry_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True, "id": entry_id}


# ============================================================
# SCREENSHOTS
# ============================================================
@api_router.get("/screenshots/{filename}")
async def get_screenshot(filename: str):
    # Prevent path traversal
    fn = os.path.basename(filename)
    p = SCREENSHOTS_DIR / fn
    if not p.exists():
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(str(p), media_type="image/png")


# ============================================================
# PRODUCT MASTER
# ============================================================
def _normalize_product(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _pick_col(headers, aliases):
    """Return the header value that matches any alias (case-insensitive contains)."""
    lower_map = {h.lower(): h for h in headers if isinstance(h, str)}
    for a in aliases:
        a_low = a.lower()
        for k, orig in lower_map.items():
            if a_low == k or a_low in k:
                return orig
    return None


class Product(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    pack: Optional[str] = None
    strength: Optional[str] = None
    mrp: Optional[str] = None
    manufacturer: Optional[str] = None
    code: Optional[str] = None
    norm: str = ""


@api_router.get("/products/count")
async def products_count():
    return {"count": await db.products.count_documents({})}


@api_router.get("/products/search")
async def products_search(q: str = Query("", min_length=0, max_length=100), limit: int = Query(20, ge=1, le=50)):
    q_norm = _normalize_product(q)
    if not q_norm:
        # Return top N by name
        docs = await db.products.find().limit(limit).to_list(limit)
        return [strip_mongo(d) for d in docs]

    # Split query into tokens; every token must appear in `norm` (prefix or substring)
    tokens = [t for t in q_norm.split() if t]

    # Build regex for prefix on first token; fall back to substring for others
    # Fast prefix on `norm` (indexed) then further filter with $all-style regex
    query = {"norm": {"$regex": f".*{re.escape(tokens[0])}", "$options": "i"}}
    if len(tokens) > 1:
        # Ensure all remaining tokens are present in norm too
        query = {"$and": [query] + [
            {"norm": {"$regex": re.escape(t), "$options": "i"}} for t in tokens[1:]
        ]}

    cursor = db.products.find(query).limit(limit)
    docs = await cursor.to_list(limit)
    return [strip_mongo(d) for d in docs]


@api_router.delete("/products/clear")
async def products_clear():
    r = await db.products.delete_many({})
    return {"deleted": r.deleted_count}


# ============================================================
# LIVECONNECT — OTP session (used by liveconnect.in and its sellers)
# ============================================================
from liveconnect_session import LiveconnectSessionManager  # noqa: E402
_lc_manager = LiveconnectSessionManager(db, _get_browser)


class LcBeginRequest(BaseModel):
    mobile: str


class LcVerifyRequest(BaseModel):
    pendingId: str
    otp: str


@api_router.get("/liveconnect/session")
async def liveconnect_session_status():
    return await _lc_manager.get_status()


@api_router.post("/liveconnect/session/begin")
async def liveconnect_session_begin(payload: LcBeginRequest):
    mob = (payload.mobile or "").strip()
    if not mob or not mob.isdigit() or len(mob) < 10:
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    res = await _lc_manager.begin(mob)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Could not send OTP")
    return res


@api_router.post("/liveconnect/session/verify")
async def liveconnect_session_verify(payload: LcVerifyRequest):
    if not payload.pendingId or not payload.otp:
        raise HTTPException(400, "pendingId and otp are required")
    res = await _lc_manager.verify(payload.pendingId, payload.otp)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "OTP verification failed")
    return res


@api_router.delete("/liveconnect/session")
async def liveconnect_session_clear():
    await _lc_manager.clear_session()
    return {"ok": True}


# ============================================================
# RETAILIO — OTP session (order.retailio.in)
# ============================================================
from retailio_session import RetailioSessionManager  # noqa: E402
_rio_manager = RetailioSessionManager(db, _get_browser)


class RioBeginRequest(BaseModel):
    mobile: str


class RioVerifyRequest(BaseModel):
    pendingId: str
    otp: str


@api_router.get("/retailio/session")
async def retailio_session_status():
    return await _rio_manager.get_status()


@api_router.post("/retailio/session/begin")
async def retailio_session_begin(payload: RioBeginRequest):
    mob = (payload.mobile or "").strip()
    if not mob or not mob.isdigit() or len(mob) < 10:
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    res = await _rio_manager.begin(mob)
    if not res.get("ok"):
        # Preserve diagnostic screenshot names on failure
        raise HTTPException(400, {"error": res.get("error") or "Could not send OTP", "diag": res.get("diag", [])})
    return res


@api_router.post("/retailio/session/verify")
async def retailio_session_verify(payload: RioVerifyRequest):
    if not payload.pendingId or not payload.otp:
        raise HTTPException(400, "pendingId and otp are required")
    res = await _rio_manager.verify(payload.pendingId, payload.otp)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "OTP verification failed")
    return res


@api_router.delete("/retailio/session")
async def retailio_session_clear():
    await _rio_manager.clear_session()
    return {"ok": True}


# ============================================================
# MARG — OTP session (margcompusoft.com/eRetail)
# ============================================================
from marg_session import MargSessionManager  # noqa: E402
_marg_manager = MargSessionManager(db, _get_browser)


class MargBeginRequest(BaseModel):
    mobile: str


class MargVerifyRequest(BaseModel):
    pendingId: str
    otp: str


@api_router.get("/marg/session")
async def marg_session_status():
    return await _marg_manager.get_status()


@api_router.post("/marg/session/begin")
async def marg_session_begin(payload: MargBeginRequest):
    mob = (payload.mobile or "").strip()
    if not mob or not mob.isdigit() or len(mob) < 10:
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    res = await _marg_manager.begin(mob)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Could not send OTP")
    return res


@api_router.post("/marg/session/verify")
async def marg_session_verify(payload: MargVerifyRequest):
    if not payload.pendingId or not payload.otp:
        raise HTTPException(400, "pendingId and otp are required")
    res = await _marg_manager.verify(payload.pendingId, payload.otp)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "OTP verification failed")
    return res


@api_router.delete("/marg/session")
async def marg_session_clear():
    await _marg_manager.clear_session()
    return {"ok": True}


# ============================================================
# ============================================================
# Shubhada Pharma — PO placement automation
# ============================================================
from shubhada_po import place_order as _sh_place_order  # noqa: E402
import uuid as _uuid  # noqa: E402


class OrderPlaceRequest(BaseModel):
    product: str
    supplier: Optional[str] = ""
    qty: int
    mobile: Optional[str] = ""
    patient: str
    advance: Optional[float] = 0


# In-memory task store (survives for lifetime of process). Each task holds:
# { status: 'running' | 'done', result: { ok, error, screenshots, steps } }
_order_tasks: dict = {}


async def _run_order_task(task_id: str, payload: OrderPlaceRequest):
    try:
        res = await _sh_place_order(
            _get_browser,
            product=payload.product,
            supplier=(payload.supplier or "").strip(),
            qty=int(payload.qty),
            mobile=(payload.mobile or "").strip(),
            patient=payload.patient.strip(),
            advance=float(payload.advance or 0),
        )
    except Exception as e:
        res = {"ok": False, "error": f"{e.__class__.__name__}: {e}", "screenshots": [], "steps": []}
    _order_tasks[task_id] = {"status": "done", "result": res}


@api_router.post("/order/place")
async def order_place(payload: OrderPlaceRequest):
    """Start the Shubhada PO automation in the background and immediately
    return a task_id. The frontend polls /api/order/status/{task_id} for
    the final result. This avoids Cloudflare's ~100s edge timeout."""
    if not payload.product or not payload.patient or not payload.qty:
        raise HTTPException(400, "product, patient and qty are required")
    task_id = _uuid.uuid4().hex
    _order_tasks[task_id] = {"status": "running", "result": None}
    asyncio.create_task(_run_order_task(task_id, payload))
    return {"task_id": task_id, "status": "running"}


@api_router.get("/order/status/{task_id}")
async def order_status(task_id: str):
    t = _order_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "unknown task_id")
    if t["status"] == "running":
        return {"task_id": task_id, "status": "running"}
    return {"task_id": task_id, "status": "done", **(t.get("result") or {})}


# ============================================================
# Price-list vault (bulk-upload distributor pricelists → searchable)
# ============================================================
from pricelist import register_routes as _register_pricelist_routes  # noqa: E402
_register_pricelist_routes(api_router, db)


@api_router.post("/products/upload")
async def products_upload(file: UploadFile = File(...)):
    """Accepts .xlsx or .csv, extracts columns matching Product Name / Pack / Strength / MRP / Manufacturer / Code."""
    try:
        import pandas as pd
    except Exception:
        raise HTTPException(500, "pandas not installed on server")

    filename = (file.filename or "").lower()
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str)
            df = df.fillna("")
        else:
            raise HTTPException(400, "Unsupported file type. Please upload .xlsx or .csv")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")

    if df.empty:
        raise HTTPException(400, "File contains no rows")

    headers = list(df.columns)
    name_col = _pick_col(headers, ["product name", "product", "name", "item", "item name", "description"])
    pack_col = _pick_col(headers, ["pack", "packing", "size"])
    strength_col = _pick_col(headers, ["strength", "mg", "dosage"])
    mrp_col = _pick_col(headers, ["mrp"])
    mfr_col = _pick_col(headers, ["manufacturer", "mfr", "company", "brand"])
    code_col = _pick_col(headers, ["code", "product code", "sku", "barcode", "id"])

    if not name_col:
        raise HTTPException(400, f"Could not find a Product Name column in: {headers}. Please rename one to 'Product Name'.")

    # Wipe existing products before importing new master (upload replaces)
    await db.products.delete_many({})

    now = datetime.utcnow()
    docs: List[Dict[str, Any]] = []
    inserted = 0
    for _, row in df.iterrows():
        name = str(row.get(name_col, "") or "").strip()
        if not name:
            continue
        p = {
            "id": str(uuid.uuid4()),
            "name": name.upper(),
            "pack": (str(row.get(pack_col, "") or "").strip() or None) if pack_col else None,
            "strength": (str(row.get(strength_col, "") or "").strip() or None) if strength_col else None,
            "mrp": (str(row.get(mrp_col, "") or "").strip() or None) if mrp_col else None,
            "manufacturer": (str(row.get(mfr_col, "") or "").strip() or None) if mfr_col else None,
            "code": (str(row.get(code_col, "") or "").strip() or None) if code_col else None,
            "norm": _normalize_product(name),
            "created_at": now,
        }
        docs.append(p)
        # Chunked insert to avoid huge single-shot
        if len(docs) >= 2000:
            try:
                await db.products.insert_many(docs, ordered=False)
                inserted += len(docs)
            except Exception as e:
                logger.warning(f"insert_many chunk error: {e}")
            docs = []
    if docs:
        try:
            await db.products.insert_many(docs, ordered=False)
            inserted += len(docs)
        except Exception as e:
            logger.warning(f"insert_many tail error: {e}")

    # Ensure index for fast search on `norm`
    try:
        await db.products.create_index("norm")
    except Exception:
        pass

    return {
        "inserted": inserted,
        "detectedColumns": {
            "name": name_col, "pack": pack_col, "strength": strength_col,
            "mrp": mrp_col, "manufacturer": mfr_col, "code": code_col,
        },
    }




# ============================================================
# ROOT
# ============================================================
@api_router.get("/")
async def root():
    return {"service": "pharmascrape", "status": "ok", "version": "2.0"}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Auth middleware: protect all /api/* except /api/auth/* and /api/ root ----
PUBLIC_API_PATHS = {"/api/", "/api"}


@app.middleware("http")
async def auth_middleware(request, call_next):
    from starlette.responses import JSONResponse
    path = request.url.path
    # Only guard /api/* endpoints; skip auth endpoints, root, and screenshots
    if (
        path.startswith("/api/")
        and not path.startswith("/api/auth/")
        and not path.startswith("/api/screenshots/")
        and path not in PUBLIC_API_PATHS
    ):
        # Allow OPTIONS preflight through
        if request.method != "OPTIONS":
            token = bearer_from_header(request.headers.get("authorization"))
            if not token:
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            try:
                decode_token(token)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
            except Exception:
                return JSONResponse({"detail": "Invalid token"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
async def on_startup():
    try:
        # Migrate: reset seed if legacy targets have no portalType and no credentials
        legacy = await db.targets.count_documents({"portalType": {"$exists": False}})
        if legacy > 0:
            await db.targets.update_many(
                {"portalType": {"$exists": False}},
                {"$set": {"portalType": "GENERIC", "hasCredentials": False}}
            )
            logger.info(f"Backfilled portalType on {legacy} legacy distributor(s)")
        await seed_if_empty()
        # Ensure RETAILIO distributor exists (post-seed migration)
        rio_existing = await db.targets.count_documents({"portalType": "RETAILIO"})
        if rio_existing == 0:
            d = Distributor(
                name="RETAILIO",
                url="https://order.retailio.in/rio/secure-login",
                portal="RETAILIO",
                portalType="RETAILIO",
                selected=True,
                hasCredentials=False,
            )
            await db.targets.insert_one(d.dict())
            logger.info("Added RETAILIO distributor")
        # Ensure YASHIKA AGENCIES distributor exists with default customer creds
        yashika_existing = await db.targets.count_documents({"portalType": "YASHIKA"})
        if yashika_existing == 0:
            d = Distributor(
                name="YASHIKA AGENCIES HUBLI",
                url="https://www.yashikaagencies.in",
                portal="YASHIKA",
                portalType="YASHIKA",
                location="Hubballi",
                username="1005173682",
                selected=True,
                hasCredentials=True,
            )
            to_store = d.dict()
            to_store["encryptedPassword"] = encrypt_secret("1005173682")
            await db.targets.insert_one(to_store)
            logger.info("Added YASHIKA AGENCIES HUBLI distributor")
        # Fix CHETHANA PHARMA — it lives on chethanapharma.in but was
        # registered with SUNSHOP portalType. Use the CHETHANA adapter
        # (same as Chirag Pharma). Idempotent.
        try:
            fixed = await db.targets.update_many(
                {"name": {"$regex": "^chethana pharma$", "$options": "i"}, "portalType": {"$ne": "CHETHANA"}},
                {"$set": {"portalType": "CHETHANA", "portal": "CHETHANA", "url": "http://www.chethanapharma.in"}},
            )
            if fixed.modified_count:
                logger.info(f"Repointed CHETHANA PHARMA to CHETHANA adapter ({fixed.modified_count} row)")
        except Exception as e:
            logger.warning(f"CHETHANA PHARMA migration skipped: {e}")
        # Fix any distributor stuck on portalType=GENERIC when its `portal`
        # field actually maps to a dedicated adapter (e.g. CHIRAG PHARMA /
        # VARDHAMAN MEDISALES were seeded with portalType hardcoded to
        # GENERIC instead of being inferred). Idempotent, keyed off `portal`
        # rather than a specific name so it self-heals future cases too.
        try:
            generic_docs = await db.targets.find({"portalType": "GENERIC"}).to_list(1000)
            for doc in generic_docs:
                inferred = infer_portal_type(doc.get("portal", ""))
                if inferred != "GENERIC":
                    await db.targets.update_one({"id": doc["id"]}, {"$set": {"portalType": inferred}})
                    logger.info(f"Repointed {doc.get('name')} from GENERIC to {inferred} adapter")
        except Exception as e:
            logger.warning(f"GENERIC portalType migration skipped: {e}")
        # Ensure MARG (ALL SUPPLIERS) distributor exists (aggregator entry)
        try:
            marg_existing = await db.targets.count_documents({"portalType": "MARG"})
            if marg_existing == 0:
                d = Distributor(
                    name="MARG (ALL SUPPLIERS)",
                    url="https://margcompusoft.com/eRetail/User/Login",
                    portal="MARG",
                    portalType="MARG",
                    selected=True,
                    hasCredentials=False,
                )
                await db.targets.insert_one(d.dict())
                logger.info("Added MARG (ALL SUPPLIERS) distributor")
        except Exception as e:
            logger.warning(f"MARG seed skipped: {e}")
        # Ensure MARG portal exists in the PORTALS list (idempotent)
        try:
            if await db.portals.count_documents({"name": "MARG"}) == 0:
                p = Portal(
                    name="MARG",
                    baseUrl="https://margcompusoft.com/eRetail",
                    status="ACTIVE",
                    description="Marg eRetail aggregator — OTP session",
                )
                await db.portals.insert_one(p.dict())
                logger.info("Added MARG portal to PORTALS list")
        except Exception as e:
            logger.warning(f"MARG portal seed skipped: {e}")
        # Enforce role model: only `shubhada` is admin; other seat users
        # (manju / abhishek / narendra) are regular members. Idempotent.
        try:
            await db.users.update_one({"username": "shubhada"}, {"$set": {"isAdmin": True}})
            r = await db.users.update_many(
                {"username": {"$in": ["manju", "abhishek", "narendra"]}, "isAdmin": True},
                {"$set": {"isAdmin": False}},
            )
            if r.modified_count:
                logger.info(f"Demoted {r.modified_count} seat users to non-admin")
        except Exception as e:
            logger.warning(f"Role migration skipped: {e}")
        asyncio.create_task(_cleanup_old_screenshots())
    except Exception as e:
        logger.error(f"Startup error: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    global _playwright
    try:
        client.close()
    except Exception:
        pass
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
