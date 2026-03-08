from fastapi import FastAPI, Request, Form, Depends, Response, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from passlib.hash import pbkdf2_sha256
from datetime import datetime, timedelta
from sqlalchemy import func
import secrets
import smtplib
from email.message import EmailMessage
from models import Event
import base64
from PIL import Image
import io
import os
import requests
import subprocess
import sys
import secrets
import hashlib
from urllib.parse import quote_plus
import re
import json

# ===== Anti-spam / Rate limit (mínimo viável, sem Redis) =====
from collections import deque
import time

DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "tempmail.com", "temp-mail.org", "10minutemail.com",
    "10minutemail.net", "guerrillamail.com", "guerrillamail.net",
    "yopmail.com", "yopmail.net", "yopmail.fr",
    "getnada.com", "trashmail.com", "trashmail.net",
    "throwawaymail.com", "dispostable.com", "fakeinbox.com",
    "maildrop.cc", "mailnesia.com", "inboxbear.com",
    "minuteinbox.com", "mohmal.com", "emailondeck.com",
    "tempinbox.com", "tempmailo.com", "sharklasers.com"
}

def normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def get_client_ip(request) -> str:
    # Render geralmente manda X-Forwarded-For; o ProxyHeadersMiddleware já ajuda,
    # mas isso aqui garante.
    xf = request.headers.get("x-forwarded-for")
    if xf:
        return xf.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

def is_disposable_email(email: str) -> bool:
    email = normalize_email(email)
    if "@" not in email:
        return True
    domain = email.split("@", 1)[1]
    # bloqueia domínios exatos e subdomínios desses provedores
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        return True
    for d in DISPOSABLE_EMAIL_DOMAINS:
        if domain.endswith("." + d):
            return True
    # heurística leve (evita bloquear e-mails reais)
    bad_words = ("tempmail", "temp-mail", "throwaway", "disposable", "mailinator", "yopmail", "guerrilla")
    if any(w in domain for w in bad_words):
        return True
    return False

class MemoryRateLimiter:
    """
    Rate-limit em memória (bom o suficiente pra começar).
    Limitação: se você tiver múltiplas instâncias, cada uma tem seu contador.
    """
    def __init__(self):
        self._buckets = {}

    def _prune(self, key: str, window_sec: int):
        now = time.time()
        dq = self._buckets.get(key)
        if not dq:
            dq = deque()
        while dq and dq[0] < now - window_sec:
            dq.popleft()
        self._buckets[key] = dq
        return dq

    def allow_and_hit(self, key: str, limit: int, window_sec: int) -> bool:
        dq = self._prune(key, window_sec)
        if len(dq) >= limit:
            return False
        dq.append(time.time())
        return True

    def is_limited(self, key: str, limit: int, window_sec: int) -> bool:
        dq = self._prune(key, window_sec)
        return len(dq) >= limit

rate_limiter = MemoryRateLimiter()

def rl_key(request, action: str, extra: str = "") -> str:
    ip = get_client_ip(request)
    if extra:
        return f"{action}:ip={ip}:x={extra}"
    return f"{action}:ip={ip}"


from db import SessionLocal, engine, Base
from models import (
    User, UserSession,
    Service, Client,
    Proposal, ProposalItem, ProposalVersion,
    PaymentStage
)
from pdf_gen import generate_proposal_pdf
import traceback

# ====== migração leve ======
try:
    subprocess.run([sys.executable, "migrate.py"], check=False)
except Exception:
    pass

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


import uuid
from starlette.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = str(uuid.uuid4())[:8]
    # loga no Render (pra você ver)
    print(f"❌ ERROR {request_id}: {repr(exc)}")
    return templates.TemplateResponse(
        "500.html",
        {"request": request, "request_id": request_id},
        status_code=500
    )

@app.middleware("http")
async def require_verified_email(request: Request, call_next):
    path = request.url.path

    # rotas liberadas
    allow = (
        path.startswith("/static")
        or path.startswith("/p/")          # público
        or path in ("/login", "/register", "/logout", "/verify")
        or path.startswith("/verify/")
        or path.startswith("/webhooks")
        or path in ("/favicon.ico", "/")
    )
    if allow:
        return await call_next(request)

    # se está logado e não verificado => manda pro verify
    try:
        db = SessionLocal()
        try:
            user = get_current_user(request, db)  # usa sua sessão (SESSION_COOKIE)
            if user and not getattr(user, "email_verified", False):
                return RedirectResponse("/verify", status_code=302)
        finally:
            db.close()
    except Exception:
        pass

    return await call_next(request)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================
# CONFIG
# ==========================
SESSION_COOKIE = "session_token"

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
COOKIE_SECURE = True if (APP_BASE_URL.startswith("https://")) else False

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()


def asaas_api_base() -> str:
    return "https://api.asaas.com/v3" if ASAAS_ENV == "prod" else "https://api-sandbox.asaas.com/v3"


def asaas_headers():
    return {
        "access_token": ASAAS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def brl(v: float) -> str:
    try:
        return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def asaas_get_subscription_payments(sub_id: str):
    r = requests.get(
        f"{asaas_api_base()}/subscriptions/{sub_id}/payments",
        headers=asaas_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Erro ao listar cobranças da assinatura: {r.status_code} - {r.text}")
    data = r.json()
    return data.get("data", []) if isinstance(data, dict) else []


def asaas_get_pix_qr(payment_id: str):
    r = requests.get(
        f"{asaas_api_base()}/payments/{payment_id}/pixQrCode",
        headers=asaas_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Erro ao obter QR Pix: {r.status_code} - {r.text}")
    return r.json()  # deve conter encodedImage, payload, expirationDate

# ==========================
# AUTH / SESSIONS
# ==========================
def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = _sha256_hex(token)
    sess = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
    if not sess:
        return None
    if sess.expires_at < _now():
        db.delete(sess)
        db.commit()
        return None
    return db.query(User).filter(User.id == sess.user_id).first()


def create_session(user: User) -> tuple[str, UserSession]:
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    expires = _now() + timedelta(days=30)
    sess = UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires)
    return token, sess

import urllib.parse
from urllib.parse import urlencode

def upgrade_redirect(reason: str, used: int | None = None, limit: int | None = None, next_url: str | None = None):
    params = {"reason": reason}
    if used is not None: params["used"] = str(used)
    if limit is not None: params["limit"] = str(limit)
    if next_url: params["next"] = next_url
    return RedirectResponse(url="/pricing?" + urlencode(params), status_code=302)

def is_pro_active(user: User) -> bool:
    if user.plan == "pro":
        if user.paid_until and user.paid_until >= _now():
            return True
        if not user.paid_until:
            return True
        return False
    if user.paid_until and user.paid_until >= _now():
        return True
    return False


def set_user_pro_month(
    db: Session,
    user: User,
    paid_until: datetime,
    subscription_id: str | None = None,
    customer_id: str | None = None
):
    user.plan = "pro"
    user.proposal_limit = 999999
    user.delete_credits = 999999
    user.plan_updated_at = _now()
    user.paid_until = paid_until
    if subscription_id:
        user.asaas_subscription_id = subscription_id
    if customer_id:
        user.asaas_customer_id = customer_id
    db.add(user)
    db.commit()


def render_message_template(tpl: str, cliente: str, servico: str, link: str) -> str:
    tpl = (tpl or "").strip()
    if not tpl:
        tpl = "Oi {cliente}! Segue seu orçamento do *{servico}*.\n👉 Link: {link}\n\nSe quiser ajustar algo, me avise 😊"
    return (tpl
            .replace("{cliente}", cliente or "")
            .replace("{servico}", servico or "")
            .replace("{link}", link or ""))

def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    domain = domain.lower()

    # evita "contas infinitas" no Gmail: remove +tag e pontos
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0]
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def gen_6digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def send_email(to_email: str, subject: str, body: str):
    brevo_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.getenv("BREVO_SENDER_NAME", "PropoFlow").strip()

    if not (brevo_key and sender_email):
        raise RuntimeError("Brevo API não configurada (BREVO_API_KEY e BREVO_SENDER_EMAIL).")

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }

    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": brevo_key,
            "Content-Type": "application/json",
            "accept": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo API erro {r.status_code}: {r.text}")

def issue_verification_code(db: Session, user: User, force: bool = False):
    now = datetime.utcnow()

    from datetime import datetime, timezone

    last = getattr(user, "email_verify_last_sent_at", None)
    if last:
        # garante timezone
        try:
            now = datetime.now(timezone.utc)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < 60:
                return templates.TemplateResponse("verify.html", {
                    "request": request,
                    "email": user.email,
                    "error": "Aguarde 1 minuto para reenviar o código."
                })
        except Exception:
            pass

    code = gen_6digit_code()
    user.email_verify_code_hash = pbkdf2_sha256.hash(code)
    user.email_verify_expires_at = now + timedelta(minutes=15)
    user.email_verify_last_sent_at = now
    db.add(user)
    db.commit()

    body = (
        "Seu código PropoFlow:\n\n"
        f"{code}\n\n"
        "Ele expira em 15 minutos.\n"
        "Se você não solicitou, ignore este e-mail."
    )
    send_email(user.email, "Seu código PropoFlow", body)
# ==========================
# HELPERS
# ==========================

def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None

def _attrib_from_request(request: Request) -> dict:
    # 1) pega do cookie (se existir)
    cookie = request.cookies.get("pf_attrib")
    if cookie:
        try:
            return json.loads(cookie)
        except:
            return {}

    # 2) pega do querystring (primeiro acesso vindo do anúncio)
    qp = dict(request.query_params)
    attrib = {}
    for k in ["utm_source","utm_medium","utm_campaign","utm_content","utm_term","ttclid"]:
        if qp.get(k):
            attrib[k] = qp.get(k)
    # referrer
    ref = request.headers.get("referer")
    if ref:
        attrib["ref"] = ref
    return attrib

def track_event(request: Request, name: str, user_id: int | None = None, proposal_id: int | None = None, extra: dict | None = None):
    attrib = _attrib_from_request(request)
    meta = {**attrib}
    if extra:
        meta.update(extra)

    db2 = SessionLocal()
    try:
        ev = Event(
            name=name,
            path=request.url.path,
            user_id=user_id,
            proposal_id=proposal_id,
            ip=_client_ip(request),
            ua=(request.headers.get("user-agent") or "")[:255],
            ref=(request.headers.get("referer") or "")[:512],
            meta=json.dumps(meta, ensure_ascii=False),
        )
        db2.add(ev)
        db2.commit()
    except:
        db2.rollback()
    finally:
        db2.close()


def parse_money_to_cents(v: str | None) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # remove tudo que não for dígito, vírgula, ponto, sinal
    s = re.sub(r"[^\d,.-]", "", s)
    # se veio no formato BR: 1.234,56 -> remove milhares e troca vírgula por ponto
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except:
        return None

def parse_money(v: str, field: str):
    s = (v or "").strip().replace(",", ".")
    if s == "":
        return None
    try:
        x = float(s)
        if x < 0:
            raise ValueError
        return x
    except Exception:
        raise ValueError(f"{field}: use apenas números (ex: 19.90)")

def parse_qty(v: str | None) -> float:
    s = (v or "").strip()
    if not s:
        return 1.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except:
        return 1.0


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    domain = domain.lower()

    # Gmail: remove +tag e pontos (evita infinitas contas free no mesmo inbox)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0]
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"

def gen_6digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def issue_verification_code(db: Session, user: User):
    code = gen_6digit_code()
    user.email_verify_code_hash = pbkdf2_sha256.hash(code)
    user.email_verify_expires_at = datetime.utcnow() + timedelta(minutes=15)
    db.add(user)
    db.commit()

    body = (
        "Seu código PropoFlow:\n\n"
        f"{code}\n\n"
        "Ele expira em 15 minutos.\n"
        "Se você não solicitou, ignore este e-mail."
    )
    send_email(user.email, "Seu código PropoFlow", body)

def base_url_from_request(request: Request) -> str:
    if APP_BASE_URL:
        return APP_BASE_URL
    return str(request.base_url).rstrip("/")


def normalize_phone_br(phone: str) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone).lstrip("0")
    if not digits:
        return None
    if digits.startswith("55"):
        core = digits[2:]
    else:
        core = digits
    if len(core) not in (10, 11):
        return None
    return "55" + core


def whatsapp_url(phone_digits_55: str, text: str) -> str:
    return f"https://wa.me/{phone_digits_55}?text={quote_plus(text)}"


def proposal_public_link(request: Request, p: Proposal) -> str:
    return f"{base_url_from_request(request)}/p/{p.public_id}"


def status_label(s: str) -> str:
    return {
        "created": "Criado",
        "sent": "Enviado",
        "viewed": "Visualizado",
        "accepted": "Aceito",
    }.get(s or "", s or "Criado")


def terms_to_list(text: str) -> list[str]:
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        ln = re.sub(r"^(\-|\•|\*|\d+\)|\d+\.)\s+", "", ln).strip()
        if ln:
            lines.append(ln)
    return lines

def process_logo_upload(file_bytes: bytes) -> tuple[str, str]:
    """
    Retorna (mime, b64). Converte para PNG e reduz para um tamanho seguro.
    """
    img = Image.open(io.BytesIO(file_bytes))
    img = img.convert("RGBA")

    # Reduz para ficar leve (logo não precisa gigante)
    img.thumbnail((320, 320))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    png_bytes = out.getvalue()

    # limita tamanho (evita estourar banco)
    if len(png_bytes) > 250_000:
        raise ValueError("Logo muito grande. Use uma imagem menor (até ~250KB).")

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    return ("image/png", b64)

def brl_to_cents(v: str) -> int:
    """
    Aceita:
      "250" -> 25000
      "250,50" -> 25050
      "1.500,00" -> 150000
      "5.0" -> 500
      "5.00" -> 500
    """
    if not v:
        return 0
    s = str(v).strip()
    s = re.sub(r"[^\d,\.]", "", s)
    if not s:
        return 0

    if "," in s and "." in s:
        tmp = s.replace(".", "").replace(",", ".")
    elif "," in s:
        tmp = s.replace(",", ".")
    else:
        tmp = s

    try:
        n = float(tmp)
        return int(round(n * 100))
    except Exception:
        return 0


def cents_to_brl(cents: int) -> str:
    n = max(0, int(cents)) / 100.0
    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def normalize_deadline(deadline: str) -> str:
    s = (deadline or "").strip()
    if not s:
        return s
    if re.fullmatch(r"\d+", s):
        n = int(s)
        return f"{n} dia" if n == 1 else f"{n} dias"
    return s


def compute_total(items: list[ProposalItem], overhead_percent: int = 0, margin_percent: int = 0) -> int:
    base = sum(int(it.line_total_cents or 0) for it in items)
    overhead_percent = max(0, int(overhead_percent or 0))
    margin_percent = max(0, int(margin_percent or 0))
    total = base
    total += int(round(base * (overhead_percent / 100.0)))
    total += int(round(base * (margin_percent / 100.0)))
    return max(0, total)


def rebuild_items_from_form(descs: list[str], qtys: list[str], units: list[str], unit_prices: list[str]) -> list[ProposalItem]:
    items: list[ProposalItem] = []
    n = min(len(descs), len(qtys), len(units), len(unit_prices))
    for i in range(n):
        d = (descs[i] or "").strip()
        if not d:
            continue
        try:
            q = float(str(qtys[i] or "1").replace(",", "."))
            if q <= 0:
                q = 1.0
        except Exception:
            q = 1.0

        u = (units[i] or "").strip() or "un"
        up_cents = brl_to_cents(unit_prices[i] or "0")
        line = int(round(q * up_cents))
        items.append(
            ProposalItem(
                sort=i,
                description=d,
                qty=q,
                unit=u,
                unit_price_cents=up_cents,
                line_total_cents=line,
            )
        )
    return items


def plan_to_percents(payment_plan: str) -> list[tuple[str, int]]:
    payment_plan = (payment_plan or "").strip()
    if payment_plan == "entrada_final_50":
        return [("Entrada", 50), ("Na entrega", 50)]
    if payment_plan == "entrada_final_30":
        return [("Entrada", 30), ("Na entrega", 70)]
    if payment_plan == "3x_30_40_30":
        return [("Entrada", 30), ("Durante o serviço", 40), ("Na entrega", 30)]
    return [("À vista", 100)]


def upsert_payment_stages(db: Session, p: Proposal, plan: list[tuple[str, int]]):
    cleaned: list[tuple[str, int]] = []
    for title, percent in plan:
        percent = max(0, min(100, int(percent or 0)))
        if percent > 0:
            cleaned.append((title, percent))

    if not cleaned:
        cleaned = [("À vista", 100)]

    total_percent = sum(x[1] for x in cleaned)
    if total_percent != 100:
        delta = 100 - total_percent
        t, pcent = cleaned[-1]
        cleaned[-1] = (t, max(0, min(100, pcent + delta)))

    total = int(p.total_cents or 0)

    def amt(percent: int) -> int:
        return int(round(total * (percent / 100.0)))

    existing = db.query(PaymentStage).filter(PaymentStage.proposal_id == p.id).all()
    for e in existing:
        db.delete(e)
    db.commit()

    for title, percent in cleaned:
        db.add(PaymentStage(proposal_id=p.id, title=title, percent=percent, amount_cents=amt(percent), status="pending"))
    db.commit()


def build_send_message(owner: User, p: Proposal, link: str) -> str:
    return render_message_template(
        owner.default_message_template if hasattr(owner, "default_message_template") else "",
        p.client_name,
        p.project_name,
        link
    )

def service_prefill(s: Service) -> dict:
    return {
        "project_name": s.title,
        "deadline": s.default_deadline or "",
        "description": s.default_description or "",
        "price": cents_to_brl(s.default_price_cents) if (s.default_price_cents or 0) > 0 else "",
        "payment_plan": (s.default_payment_plan or "avista").strip() or "avista",
    }


def normalize_whatsapp_key(phone: str) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits or None


def upsert_client_for_user(db: Session, owner_id: int, name: str, whatsapp: str | None) -> Client:
    n = (name or "").strip()
    w = (whatsapp or "").strip() or None
    wkey = normalize_whatsapp_key(w or "")

    q = db.query(Client).filter(Client.owner_id == owner_id, Client.archived.is_(False))

    found: Client | None = None
    if wkey:
        # tenta bater por whatsapp
        candidates = q.filter(Client.whatsapp.isnot(None)).all()
        for c in candidates:
            if normalize_whatsapp_key(c.whatsapp or "") == wkey:
                found = c
                break
    if not found:
        # tenta bater por nome
        found = q.filter(Client.name.ilike(n)).first()

    if found:
        # atualiza whatsapp se vier e se não tinha
        if w and (not found.whatsapp):
            found.whatsapp = w
            found.updated_at = _now()
            db.add(found)
            db.commit()
        # atualiza nome se estava diferente (mantém simples)
        if n and found.name != n:
            found.name = n
            found.updated_at = _now()
            db.add(found)
            db.commit()
        return found

    c = Client(owner_id=owner_id, name=n, whatsapp=w, archived=False, created_at=_now(), updated_at=_now())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def payment_plan_from_stages(stages: list[PaymentStage]) -> str:
    if not stages:
        return "avista"
    if len(stages) == 1 and int(stages[0].percent or 0) == 100:
        return "avista"
    if len(stages) == 2:
        p1, p2 = int(stages[0].percent or 0), int(stages[1].percent or 0)
        if p1 == 30 and p2 == 70:
            return "entrada_final_30"
        if p1 == 50 and p2 == 50:
            return "entrada_final_50"
    if len(stages) == 3:
        p = [int(s.percent or 0) for s in stages[:3]]
        if p == [30, 40, 30]:
            return "3x_30_40_30"
    return "avista"


# ==========================
# ROUTES
# ==========================
@app.get("/")
def home(request: Request):
    # registra evento
    track_event(request, "landing_view")

    # render normal
    resp = templates.TemplateResponse("home.html", {"request": request})

    # salva cookie com utm/ttclid (30 dias)
    attrib = _attrib_from_request(request)
    if attrib:
        resp.set_cookie(
            "pf_attrib",
            json.dumps(attrib, ensure_ascii=False),
            max_age=60*60*24*30,
            samesite="Lax"
        )
    return resp

# ===== AUTH =====
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})



@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email_norm = normalize_email(email)
    user = db.query(User).filter(User.email == email_norm).first()

    if (not user) or (not pbkdf2_sha256.verify(password, user.password_hash)):
        # Rate limit por IP+email: 8 falhas em 10 min
        key = rl_key(request, "login_fail", extra=email)
        if not rate_limiter.allow_and_hit(key, limit=8, window_sec=600):
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Muitas tentativas com esse e-mail. Aguarde 10 minutos."
            })
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Email ou senha inválidos."
        })

    token, sess = create_session(user)
    db.add(sess)
    db.commit()

    email = normalize_email(email)

    # Rate limit de login por IP (ex.: 30 tentativas / 10 min)
    if rate_limiter.is_limited(rl_key(request, "login"), limit=30, window_sec=600):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Muitas tentativas de login. Aguarde 10 minutos e tente novamente."
        })

    # Se não verificou, manda pro verify (e reenvia se expirou/não existe)
    if not getattr(user, "email_verified", False):
        expired = (not getattr(user, "email_verify_expires_at", None)) or (user.email_verify_expires_at < datetime.utcnow())
        if expired or not getattr(user, "email_verify_code_hash", None):
            try:
                issue_verification_code(db, user)
            except Exception as e:
                return templates.TemplateResponse("login.html", {"request": request, "error": f"Confirme seu e-mail. Erro ao enviar código: {str(e)}"})

        resp = RedirectResponse("/verify", status_code=302)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/register")
def register(request: Request,
             email: str = Form(...),
             password: str = Form(...),
             db: Session = Depends(get_db)):

    email_norm = (email or "").strip().lower()

    # validações básicas
    if not email_norm or "@" not in email_norm:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Digite um e-mail válido."
        })

    if not password or len(password) < 6:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Sua senha precisa ter pelo menos 6 caracteres."
        })

    # já existe?
    if db.query(User).filter(User.email == email_norm).first():
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "Esse email já existe. Faça login."
        })

    # cria usuário (FREE)
    user = User(
        email=email_norm,
        password_hash=pbkdf2_sha256.hash(password),
        proposal_limit=5,
        plan="free",
        delete_credits=1,
        email_verified=False,  # garante que nasce como não verificado
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    track_event(request, "register_success", user_id=new_user.id)

    # cria sessão já logando (pra ele conseguir entrar na /verify)
    token, sess = create_session(user)
    db.add(sess)
    db.commit()

    # manda código e redireciona pra verify
    try:
        send_email_verification_code(user, db)
    except Exception:
        # se der problema de e-mail, ainda assim leva pra verify (lá ele tenta reenviar)
        pass

    resp = RedirectResponse("/verify", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 24 * 30
    )
    return resp

@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if getattr(user, "email_verified", False):
        return RedirectResponse("/dashboard", status_code=302)

    return templates.TemplateResponse("verify.html", {
        "request": request,
        "email": user.email,
        "error": None,
        "info": None,
    })


@app.post("/verify")
def verify_submit(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if getattr(user, "email_verified", False):
        return RedirectResponse("/dashboard", status_code=302)

    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": "Código inválido. Digite 6 números.",
            "info": None,
        })

    if not user.email_verify_code_hash or not user.email_verify_expires_at:
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": "Seu código expirou. Clique em reenviar.",
            "info": None,
        })

    if user.email_verify_expires_at < datetime.utcnow():
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": "Seu código expirou. Clique em reenviar.",
            "info": None,
        })

    if not pbkdf2_sha256.verify(code, user.email_verify_code_hash):
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": "Código incorreto. Tente novamente.",
            "info": None,
        })

    user.email_verified = True
    user.email_verify_code_hash = None
    user.email_verify_expires_at = None
    db.add(user)
    db.commit()

    count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    if count == 0:
        return RedirectResponse("/welcome", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/verify/resend")
def verify_resend(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if getattr(user, "email_verified", False):
        return RedirectResponse("/dashboard", status_code=302)

    track_event(request, "verify_success", user_id=user.id)

    try:
        issue_verification_code(db, user)
    except Exception as e:
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": f"Erro ao reenviar código: {str(e)}",
            "info": None,
        })

    # Rate limit por IP: 10 reenvios / hora
    if not rate_limiter.allow_and_hit(rl_key(request, "verify_resend"), limit=10, window_sec=3600):
        return templates.TemplateResponse("verify.html", {
            "request": request,
            "email": user.email,
            "error": "Muitas tentativas de reenvio. Tente novamente em 1 hora."
        })

    return templates.TemplateResponse("verify.html", {
        "request": request,
        "email": user.email,
        "error": None,
        "info": "Código reenviado. Verifique seu e-mail.",
    })



@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        th = _sha256_hex(token)
        sess = db.query(UserSession).filter(UserSession.token_hash == th).first()
        if sess:
            db.delete(sess)
            db.commit()

    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/welcome")
def welcome(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # se ainda não verificou email, força verificação
    if not getattr(user, "email_verified", False):
        return RedirectResponse("/verify", status_code=302)

    # se já tem orçamentos, não precisa welcome
    count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    if count > 0:
        return RedirectResponse("/dashboard", status_code=302)

    missing = []
    if not (user.display_name or "").strip():
        missing.append("Nome")
    if not (user.phone or "").strip():
        missing.append("WhatsApp")
    if not (user.pix_key or "").strip():
        missing.append("Pix")

    return templates.TemplateResponse("welcome.html", {
        "request": request,
        "user": user,
        "missing_profile": missing
    })

def is_profile_ok(user) -> bool:
    # bem “tolerante” pra não quebrar se mudar nome de campo
    display_name = (getattr(user, "display_name", "") or "").strip()
    phone = (getattr(user, "phone", "") or "").strip()
    pix_key = (getattr(user, "pix_key", "") or "").strip()
    return bool(display_name and phone and pix_key)

def has_any_proposal(db: Session, user_id: int) -> bool:
    return db.query(Proposal.id).filter(Proposal.owner_id == user_id).first() is not None

@app.get("/start")
def start_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # se já está tudo ok, manda pro dashboard
    profile_ok = is_profile_ok(user)
    has_proposal = has_any_proposal(db, user.id)
    if profile_ok and has_proposal:
        return RedirectResponse("/dashboard", status_code=302)

    return templates.TemplateResponse("start.html", {
        "request": request,
        "user": user,
        "profile_ok": profile_ok,
        "has_proposal": has_proposal,
    })

# ===== DASHBOARD =====
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    status: str = "all",
    q: str = "",
    days: int = 30,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    skip_start = request.query_params.get("skip_start") == "1"
    if not skip_start:
        profile_ok = is_profile_ok(user)
        has_proposal = has_any_proposal(db, user.id)
        if (not profile_ok) or (not has_proposal):
            return RedirectResponse("/start", status_code=302)

    days = int(days or 30)
    days = 7 if days <= 7 else (30 if days <= 30 else 90)
    since = _now() - timedelta(days=days)

    query = db.query(Proposal).filter(
        Proposal.owner_id == user.id,
        Proposal.created_at >= since
    )

    if status == "accepted":
        query = query.filter(Proposal.accepted_at.isnot(None))
    elif status == "pending":
        query = query.filter(Proposal.accepted_at.is_(None))

    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            (Proposal.client_name.ilike(like)) |
            (Proposal.project_name.ilike(like)) |
            (Proposal.public_id.ilike(like))
        )

    proposals = query.order_by(Proposal.created_at.desc()).all()

    total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    accepted = db.query(Proposal).filter(Proposal.owner_id == user.id, Proposal.accepted_at.isnot(None)).count()

    viewed = db.query(Proposal).filter(
        Proposal.owner_id == user.id,
        Proposal.first_viewed_at.isnot(None)
    ).count()

    rate = round((accepted / total) * 100) if total else 0

    pro_active = is_pro_active(user)
    free_limit = int(getattr(user, "proposal_limit", 5) or 5)
    free_used = int(total or 0)  # usa o total geral (mesmo do KPI)
    free_pct = 0
    if (not pro_active) and free_limit > 0:
        free_pct = int((free_used * 100) / free_limit)
        if free_pct > 100:
            free_pct = 100

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "owner": user,
        "proposals": proposals,
        "total": total,
        "accepted": accepted,
        "rate": rate,
        "status": status,
        "q": term,
        "days": days,
        "status_label": status_label,
        "error": None,
        "show_upgrade": False,
        "viewed": viewed,
        "pro_active": pro_active,
        "free_used": free_used,
        "free_limit": free_limit,
        "free_pct": free_pct,
    })

@app.get("/proposals/{proposal_id}/again")
def proposal_again(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    original = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()
    if not original:
        return RedirectResponse("/dashboard", status_code=302)

    # limite free
    if not is_pro_active(user):
        count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
        if count >= (user.proposal_limit or 5):
            return RedirectResponse("/limit", status_code=302)

    new_p = Proposal(
        client_id=original.client_id,
        client_name=original.client_name,
        client_whatsapp=original.client_whatsapp,
        project_name=original.project_name,
        description=original.description,
        deadline=original.deadline,
        owner_id=user.id,
        status="created",
        valid_until=_now() + timedelta(days=int(getattr(user, "default_validity_days", 7) or 7)),
        last_activity_at=_now(),
        revision=1,
        updated_at=_now(),
        total_cents=int(original.total_cents or 0),
        price=original.price,
    )
    db.add(new_p)
    db.commit()
    db.refresh(new_p)

    # dup itens
    for it in original.items:
        db.add(ProposalItem(
            proposal_id=new_p.id,
            sort=it.sort,
            description=it.description,
            unit=it.unit,
            qty=it.qty,
            unit_price_cents=it.unit_price_cents,
            line_total_cents=it.line_total_cents,
        ))
    db.commit()

    # dup payment plan
    stages = db.query(PaymentStage).filter(PaymentStage.proposal_id == original.id).order_by(PaymentStage.id.asc()).all()
    plan = [(s.title, int(s.percent or 0)) for s in stages] if stages else [("À vista", 100)]
    upsert_payment_stages(db, new_p, plan)

    return RedirectResponse(f"/proposals/{new_p.id}/created", status_code=302)

def terms_to_list(text: str) -> list[str]:
    # quebra por linha e remove vazios
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # remove bullets comuns
        ln = re.sub(r"^(\-|\•|\*|\d+\)|\d+\.)\s+", "", ln).strip()
        if ln:
            lines.append(ln)
    return lines


@app.get("/proposals/{proposal_id}/created", response_class=HTMLResponse)
def proposal_created(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    public_link = proposal_public_link(request, p)
    msg = build_send_message(user, p, public_link)

    return templates.TemplateResponse("created.html", {
        "request": request,
        "user": user,
        "p": p,
        "public_link": public_link,
        "message": msg
    })

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "saved": bool(saved)})


@app.post("/settings")
def settings_save(
    request: Request,
    default_message_template: str = Form(""),
    default_terms: str = Form(""),
    default_validity_days: int = Form(7),
    default_payment_plan: str = Form("avista"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.default_message_template = (default_message_template or "").strip() or None
    user.default_terms = (default_terms or "").strip() or ""
    user.default_validity_days = int(default_validity_days or 7)
    user.default_payment_plan = (default_payment_plan or "avista").strip()

    db.add(user)
    db.commit()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "saved": True,
        "error": None
    })
# ===== SERVICES =====
@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request, saved: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    services = db.query(Service).filter(Service.owner_id == user.id, Service.archived.is_(False)).order_by(Service.favorite.desc(), Service.title.asc()).all()
    return templates.TemplateResponse("services.html", {
        "request": request,
        "services": services,
        "saved": bool(saved),
        "error": None
    })


@app.post("/services/new")
def services_new(
    request: Request,
    title: str = Form(...),
    default_price: str = Form(""),
    default_deadline: str = Form(""),
    default_description: str = Form(""),
    default_payment_plan: str = Form("avista"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    s = Service(
        owner_id=user.id,
        title=title.strip(),
        default_description=(default_description or "").strip() or None,
        default_price_cents=brl_to_cents(default_price),
        default_deadline=normalize_deadline(default_deadline) or None,
        default_payment_plan=(default_payment_plan or "avista").strip() or "avista",
        updated_at=_now()
    )
    db.add(s)
    db.commit()
    return RedirectResponse("/services", status_code=302)


@app.post("/services/{service_id}/update")
def services_update(
    service_id: int,
    request: Request,
    title: str = Form(...),
    default_price: str = Form(""),
    default_deadline: str = Form(""),
    default_description: str = Form(""),
    default_payment_plan: str = Form("avista"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id).first()
    if not s:
        return RedirectResponse("/services", status_code=302)

    s.title = title.strip()
    s.default_description = (default_description or "").strip() or None
    s.default_price_cents = brl_to_cents(default_price)
    s.default_deadline = normalize_deadline(default_deadline) or None
    s.default_payment_plan = (default_payment_plan or "avista").strip() or "avista"
    s.updated_at = _now()
    db.add(s)
    db.commit()
    return RedirectResponse("/services", status_code=302)


@app.post("/services/{service_id}/delete")
def services_delete(service_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id).first()
    if s:
        s.archived = True
        s.updated_at = _now()
        db.add(s)
        db.commit()
    return RedirectResponse("/services", status_code=302)


# ===== CLIENTS =====
@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    clients = db.query(Client).filter(Client.owner_id == user.id, Client.archived.is_(False)).order_by(Client.favorite.desc(), Client.name.asc()).all()
    return templates.TemplateResponse("clients.html", {
        "request": request,
        "clients": clients,
        "error": None
    })


@app.post("/clients/new")
def clients_new(
    request: Request,
    name: str = Form(...),
    whatsapp: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    upsert_client_for_user(db, user.id, name, whatsapp or None)
    return RedirectResponse("/clients", status_code=302)


@app.post("/clients/{client_id}/update")
def clients_update(
    client_id: int,
    request: Request,
    name: str = Form(...),
    whatsapp: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    c = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
    if not c:
        return RedirectResponse("/clients", status_code=302)

    c.name = (name or "").strip()
    c.whatsapp = (whatsapp or "").strip() or None
    c.updated_at = _now()
    db.add(c)
    db.commit()
    return RedirectResponse("/clients", status_code=302)


@app.post("/clients/{client_id}/delete")
def clients_delete(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    c = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id).first()
    if c:
        c.archived = True
        c.updated_at = _now()
        db.add(c)
        db.commit()
    return RedirectResponse("/clients", status_code=302)

@app.post("/services/{service_id}/favorite")
def toggle_service_favorite(service_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
    if s:
        s.favorite = not bool(s.favorite)
        s.updated_at = _now()
        db.add(s)
        db.commit()

    back = request.headers.get("referer") or "/services"
    return RedirectResponse(back, status_code=302)


@app.post("/clients/{client_id}/favorite")
def toggle_client_favorite(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    c = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
    if c:
        c.favorite = not bool(c.favorite)
        c.updated_at = _now()
        db.add(c)
        db.commit()

    back = request.headers.get("referer") or "/clients"
    return RedirectResponse(back, status_code=302)

# ===== NEW PROPOSAL =====
@app.get("/proposals/new", response_class=HTMLResponse)
def new_proposal_page(request: Request, service_id: int = 0, client_id: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    services = db.query(Service).filter(Service.owner_id == user.id, Service.archived.is_(False)).order_by(Service.title.asc()).all()
    clients = db.query(Client).filter(Client.owner_id == user.id, Client.archived.is_(False)).order_by(Client.name.asc()).all()

    prefill = {"project_name": "", "deadline": "", "description": "", "price": "", "payment_plan": "avista"}
    prefill_client = {"name": "", "whatsapp": ""}

    selected_service = None
    if service_id:
        selected_service = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
        if selected_service:
            prefill = service_prefill(selected_service)

    selected_client = None
    if client_id:
        selected_client = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
        if selected_client:
            prefill_client = {"name": selected_client.name, "whatsapp": selected_client.whatsapp or ""}

    return templates.TemplateResponse("new_proposal.html", {
        "request": request,
        "error": None,
        "services": services,
        "clients": clients,
        "selected_service_id": selected_service.id if selected_service else 0,
        "selected_client_id": selected_client.id if selected_client else 0,
        "prefill": prefill,
        "prefill_client": prefill_client,
    })


@app.post("/proposals/new")
def create_proposal(
    request: Request,
    service_id: int = Form(0),
    client_id: int = Form(0),
    client_name: str = Form(...),
    client_whatsapp: str = Form(""),
    project_name: str = Form(...),
    description: str = Form(...),
    price: str = Form(""),
    deadline: str = Form(...),
    validity_days: int = Form(7),
    payment_plan: str = Form("avista"),
    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # BLOQUEIO FREE (6º orçamento)
    if not is_pro_active(user):
        used = count_user_proposals(db, user.id)
        limit = int(getattr(user, "proposal_limit", 5) or 5)
        if used >= limit:
            return upgrade_redirect("limit", used=used, limit=limit, next_url="/wizard")

    # Prefill por serviço (se escolhido)
    if service_id:
        s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
        if s:
            if not project_name.strip():
                project_name = s.title
            if not description.strip() and s.default_description:
                description = s.default_description
            if not deadline.strip() and s.default_deadline:
                deadline = s.default_deadline
            if (not price.strip()) and (s.default_price_cents or 0) > 0:
                price = cents_to_brl(s.default_price_cents)
            if (payment_plan in ("", "avista")) and s.default_payment_plan:
                payment_plan = s.default_payment_plan

    # Prefill por cliente (se escolhido)
    selected_client: Client | None = None
    if client_id:
        selected_client = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
        if selected_client:
            if not client_name.strip():
                client_name = selected_client.name
            if not client_whatsapp.strip() and selected_client.whatsapp:
                client_whatsapp = selected_client.whatsapp

    # Auto-salvar/atualizar cliente (se não veio client_id)
    final_client_id: int | None = None
    if selected_client:
        final_client_id = selected_client.id
    else:
        if (client_name or "").strip():
            c = upsert_client_for_user(db, user.id, client_name, client_whatsapp or None)
            final_client_id = c.id

    valid_until = _now() + timedelta(days=max(1, min(int(validity_days or 7), 30)))

    p = Proposal(
        client_id=final_client_id,
        client_name=(client_name or "").strip(),
        client_whatsapp=(client_whatsapp or "").strip() or None,
        project_name=(project_name or "").strip(),
        description=(description or "").strip(),
        deadline=normalize_deadline(deadline),
        owner_id=user.id,
        status="created",
        valid_until=valid_until,
        last_activity_at=_now(),
        revision=1,
        updated_at=_now(),
        terms_text=(getattr(user, "default_terms", "") or "").strip() or None,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    track_event(request, "proposal_created", user_id=user.id, proposal_id=p.id)

    items = rebuild_items_from_form(item_desc, item_qty, item_unit, item_unit_price)
    for it in items:
        it.proposal_id = p.id
        db.add(it)
    db.commit()

    saved_items = db.query(ProposalItem).filter(ProposalItem.proposal_id == p.id).order_by(ProposalItem.sort.asc()).all()
    total_cents = compute_total(saved_items, 0, 0)

    override = brl_to_cents((price or "").strip())
    if override > 0:
        total_cents = override

    p.total_cents = total_cents
    p.price = cents_to_brl(total_cents)
    db.add(p)
    db.commit()

    upsert_payment_stages(db, p, plan_to_percents(payment_plan))

    return RedirectResponse(f"/proposals/{p.id}/send", status_code=302)


@app.get("/proposals/{proposal_id}/send_whatsapp")
def send_whatsapp(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    phone = normalize_phone_br(p.client_whatsapp or "")
    if not phone:
        return RedirectResponse(f"/proposals/{p.id}/edit", status_code=302)

    link = proposal_public_link(request, p)
    text = build_send_message(user, p, link)

    if p.status != "accepted":
        p.status = "sent"
    p.last_activity_at = _now()
    db.add(p)
    db.commit()

    return RedirectResponse(whatsapp_url(phone, text), status_code=302)


# ===== EDIT / VERSION =====
@app.get("/proposals/{proposal_id}/edit", response_class=HTMLResponse)
def edit_proposal_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    stages = db.query(PaymentStage).filter(PaymentStage.proposal_id == p.id).order_by(PaymentStage.id.asc()).all()
    current_plan = payment_plan_from_stages(stages)

    clients = db.query(Client).filter(Client.owner_id == user.id, Client.archived.is_(False)).order_by(Client.name.asc()).all()
    services = db.query(Service).filter(Service.owner_id == user.id, Service.archived.is_(False)).order_by(Service.title.asc()).all()

    return templates.TemplateResponse("edit_proposal.html", {
        "request": request,
        "p": p,
        "clients": clients,
        "services": services,
        "payment_plan": current_plan,
    })


@app.post("/proposals/{proposal_id}/edit")
def edit_proposal_save(
    proposal_id: int,
    request: Request,
    client_id: int = Form(0),
    service_id: int = Form(0),
    client_name: str = Form(...),
    client_whatsapp: str = Form(""),
    project_name: str = Form(...),
    description: str = Form(...),
    deadline: str = Form(...),
    price: str = Form(""),
    payment_plan: str = Form("avista"),
    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        try:
            # ... seu código atual de salvar (atualiza proposal, itens, etapas etc)
            db.commit()
            return RedirectResponse("/dashboard", status_code=302)

        except Exception as e:
            db.rollback()
            print("❌ ERRO ao salvar /proposals/{id}/edit:", str(e))
            traceback.print_exc()
            return HTMLResponse("Internal Server Error", status_code=500)

    # Se o orçamento é antigo e ainda não tem terms_text, congela agora (uma vez só)
    if not getattr(p, "terms_text", None):
        p.terms_text = (getattr(user, "default_terms", "") or "").strip() or None

    # snapshot antes
    snapshot = {
        "revision": p.revision,
        "client_id": p.client_id,
        "client_name": p.client_name,
        "client_whatsapp": p.client_whatsapp,
        "project_name": p.project_name,
        "description": p.description,
        "deadline": p.deadline,
        "price": p.price,
        "total_cents": p.total_cents,
        "items": [
            {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
            for it in p.items
        ],
        "payment_stages": [
            {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents, "status": st.status}
            for st in p.payment_stages
        ]
    }
    db.add(ProposalVersion(proposal_id=p.id, revision=p.revision, snapshot_json=json.dumps(snapshot, ensure_ascii=False)))
    db.commit()

    # aplicar defaults de serviço no edit (se escolheu e deixou campos vazios)
    if service_id:
        s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
        if s:
            if not project_name.strip():
                project_name = s.title
            if not description.strip() and s.default_description:
                description = s.default_description
            if not deadline.strip() and s.default_deadline:
                deadline = s.default_deadline
            if (not price.strip()) and (s.default_price_cents or 0) > 0:
                price = cents_to_brl(s.default_price_cents)
            if (payment_plan in ("", "avista")) and s.default_payment_plan:
                payment_plan = s.default_payment_plan

    # cliente: se veio client_id válido, usa. Senão, upsert
    selected_client = None
    if client_id:
        selected_client = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()

    if selected_client:
        p.client_id = selected_client.id
        if not client_name.strip():
            client_name = selected_client.name
        if not client_whatsapp.strip() and selected_client.whatsapp:
            client_whatsapp = selected_client.whatsapp
    else:
        if (client_name or "").strip():
            c = upsert_client_for_user(db, user.id, client_name, client_whatsapp or None)
            p.client_id = c.id

    p.revision = int(p.revision or 1) + 1
    p.updated_at = _now()
    p.last_activity_at = _now()

    p.client_name = (client_name or "").strip()
    p.client_whatsapp = (client_whatsapp or "").strip() or None
    p.project_name = (project_name or "").strip()
    p.description = (description or "").strip()
    p.deadline = normalize_deadline(deadline)

    # recria itens
    for it in list(p.items):
        db.delete(it)
    db.commit()

    items = rebuild_items_from_form(item_desc, item_qty, item_unit, item_unit_price)
    for it in items:
        it.proposal_id = p.id
        db.add(it)
    db.commit()

    saved_items = db.query(ProposalItem).filter(ProposalItem.proposal_id == p.id).order_by(ProposalItem.sort.asc()).all()
    total_cents = compute_total(saved_items, 0, 0)

    override = brl_to_cents((price or "").strip())
    if override > 0:
        total_cents = override

    p.total_cents = total_cents
    p.price = cents_to_brl(total_cents)
    db.add(p)
    db.commit()

    upsert_payment_stages(db, p, plan_to_percents(payment_plan))

    return RedirectResponse("/dashboard", status_code=302)


@app.get("/proposals/{proposal_id}/duplicate")
def duplicate_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    original = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not original:
        return RedirectResponse("/dashboard", status_code=302)

    # limite free
    if not is_pro_active(user):
        count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
        if count >= (user.proposal_limit or 5):
            return RedirectResponse("/pricing", status_code=302)

    new_p = Proposal(
        client_id=original.client_id,
        client_name=original.client_name,
        client_whatsapp=original.client_whatsapp,
        project_name=original.project_name,
        description=original.description,
        price=original.price,
        deadline=original.deadline,
        owner_id=user.id,
        status="created",
        valid_until=_now() + timedelta(days=7),
        last_activity_at=_now(),
        revision=1,
        updated_at=_now(),
        total_cents=int(original.total_cents or 0),
    )
    db.add(new_p)
    db.commit()
    db.refresh(new_p)

    # dup itens
    for it in original.items:
        db.add(ProposalItem(
            proposal_id=new_p.id,
            sort=it.sort,
            description=it.description,
            unit=it.unit,
            qty=it.qty,
            unit_price_cents=it.unit_price_cents,
            line_total_cents=it.line_total_cents,
        ))
    db.commit()

    # dup payment plan (aprox)
    stages = db.query(PaymentStage).filter(PaymentStage.proposal_id == original.id).order_by(PaymentStage.id.asc()).all()
    plan = [(s.title, int(s.percent or 0)) for s in stages] if stages else [("À vista", 100)]
    upsert_payment_stages(db, new_p, plan)

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/proposals/{proposal_id}/delete")
def delete_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    if not is_pro_active(user):
        credits = int(getattr(user, "delete_credits", 0) or 0)
        if credits <= 0:
            return upgrade_redirect("delete_limit", used=None, limit=None, next_url="/dashboard")

    # free: só 1 exclusão
    if not is_pro_active(user) and user.plan == "free":
        credits = user.delete_credits or 0
        if credits <= 0:
            # tenta renderizar dashboard com erro (se template suportar)
            proposals = db.query(Proposal).filter(Proposal.owner_id == user.id).order_by(Proposal.created_at.desc()).all()
            total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
            accepted = db.query(Proposal).filter(Proposal.owner_id == user.id, Proposal.accepted_at.isnot(None)).count()
            rate = round((accepted / total) * 100) if total else 0
            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "owner": user,
                "proposals": proposals,
                "total": total,
                "accepted": accepted,
                "rate": rate,
                "status": "all",
                "status_label": status_label,
                "error": "No plano gratuito você só pode excluir 1 orçamento. Faça upgrade para excluir ilimitado.",
                "show_upgrade": True,
            })
        user.delete_credits = credits - 1
        db.add(user)

    if not is_pro_active(user):
        user.delete_credits = max(0, (user.delete_credits or 0) - 1)
        db.add(user)

    # apaga tudo
    db.delete(p)
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)

def count_user_proposals(db: Session, user_id: int) -> int:
    q = db.query(Proposal).filter(Proposal.owner_id == user_id)

    # Compatível com versões diferentes do model
    if hasattr(Proposal, "archived"):
        q = q.filter(Proposal.archived.is_(False))
    elif hasattr(Proposal, "deleted_at"):
        q = q.filter(Proposal.deleted_at.is_(None))
    elif hasattr(Proposal, "is_deleted"):
        q = q.filter(Proposal.is_deleted.is_(False))

    return q.count()


@app.post("/proposals/{proposal_id}/save_service")
def save_proposal_as_service(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    title = (p.project_name or "").strip() or "Meu serviço"

    existing = db.query(Service).filter(Service.owner_id == user.id, Service.title == title, Service.archived.is_(False)).first()
    if existing:
        existing.default_description = p.description
        existing.default_deadline = p.deadline
        existing.default_price_cents = int(p.total_cents or 0)
        existing.updated_at = _now()
        db.add(existing)
    else:
        s = Service(
            owner_id=user.id,
            title=title,
            default_description=p.description,
            default_price_cents=int(p.total_cents or 0),
            default_deadline=p.deadline,
            default_payment_plan="avista",
            updated_at=_now()
        )
        db.add(s)

    db.commit()
    return RedirectResponse("/services?saved=1", status_code=302)


# ===== PUBLIC (TRACK + ACCEPT) =====
@app.get("/p/{public_id}", response_class=HTMLResponse)
def public_proposal(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    # ===== Etapa 13: rastrear visualizações (sem contar o dono logado) =====
    viewer = get_current_user(request, db)  # se estiver logado
    is_owner_view = bool(viewer and viewer.id == p.owner_id)

    if not is_owner_view:
        now = datetime.utcnow()
        p.view_count = int(getattr(p, "view_count", 0) or 0) + 1
        if getattr(p, "first_viewed_at", None) is None:
            p.first_viewed_at = now
        p.last_viewed_at = now
        db.add(p)
        db.commit()
        db.refresh(p)

    base_url = base_url_from_request(request)

    view_cookie = f"pv_{public_id}"
    last_seen = request.cookies.get(view_cookie)

    should_count = True
    if last_seen:
        try:
            last_dt = datetime.fromisoformat(last_seen)
            if (_now() - last_dt) < timedelta(minutes=10):
                should_count = False
        except Exception:
            should_count = True

    if should_count:
        p.view_count = (p.view_count or 0) + 1
        if not p.first_viewed_at:
            p.first_viewed_at = _now()
        p.last_viewed_at = _now()
        p.last_activity_at = _now()
        if p.status in ("sent", "created") and p.accepted_at is None:
            p.status = "viewed"
        db.add(p)
        db.commit()

    stages = db.query(PaymentStage).filter(PaymentStage.proposal_id == p.id).order_by(PaymentStage.id.asc()).all()

    terms_src = (getattr(p, "terms_text", None) or "") or ((getattr(owner, "default_terms", "") or "") if owner else "")
    terms_lines = terms_to_list(terms_src)

    # ===== Itens formatados para o link público =====
    def _fmt_qty(q):
        try:
            f = float(q)
            if f.is_integer():
                return str(int(f))
            s = str(f).rstrip("0").rstrip(".")
            return s
        except Exception:
            return str(q or "")

    display_items = []
    items_subtotal_cents = 0
    for it in getattr(p, "items", []) or []:
        line = int(getattr(it, "line_total_cents", 0) or 0)
        items_subtotal_cents += line
        display_items.append({
            "desc": getattr(it, "description", "") or "",
            "qty": _fmt_qty(getattr(it, "qty", 1)),
            "unit_price_brl": cents_to_brl(int(getattr(it, "unit_price_cents", 0) or 0)),
            "line_total_brl": cents_to_brl(line),
        })

    items_subtotal_brl = cents_to_brl(items_subtotal_cents)
    final_total_brl = cents_to_brl(int(getattr(p, "total_cents", 0) or 0))

    # ===== Condições: sempre mostrar (fallback) =====
    if not terms_lines:
        valid_txt = ""
        if getattr(p, "valid_until", None):
            valid_txt = f"Validade: até {p.valid_until.strftime('%d/%m/%Y')}."
        else:
            valid_txt = "Validade: conforme combinado."

        terms_lines = [
            valid_txt,
            "Pagamento: conforme “Como pagar” acima.",
            "O que não estiver descrito no orçamento não está incluso.",
            "Reagendamento: avisar com antecedência (sujeito à disponibilidade).",
        ]

    resp = templates.TemplateResponse("proposal_public.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url,
        "status_label": status_label,
        "stages": stages,
        "terms_lines": terms_lines,
        "items": display_items,
        "items_subtotal_brl": items_subtotal_brl,
        "final_total_brl": final_total_brl,
        "is_pro": (owner is not None and is_pro_active(owner)),
    })


    resp.set_cookie(view_cookie, _now().isoformat(), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(public_id: str, request: Request, name: str = Form(...), email: str = Form(""), db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = base_url_from_request(request)

    if p.accepted_at is None:
        p.accepted_at = _now()
        p.accepted_name = name.strip()
        p.accepted_email = (email or "").strip() or None
        p.status = "accepted"
        p.last_activity_at = _now()
        db.add(p)
        db.commit()
        db.refresh(p)

    return templates.TemplateResponse("accepted.html", {"request": request, "p": p, "owner": owner, "base_url": base_url})


# ===== PDF =====
@app.get("/p/{public_id}/pdf")
def public_pdf(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    items = [
        {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
        for it in p.items
    ]
    stages = [
        {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents}
        for st in p.payment_stages
    ]

    # termos: usa o congelado do orçamento; se não tiver, cai pro padrão do owner
    terms_src = (getattr(p, "terms_text", None) or "") or (getattr(owner, "default_terms", "") or "")
    payment_terms = terms_to_list(terms_src)

    if not payment_terms:
        valid_txt = ""
        if getattr(p, "valid_until", None):
            valid_txt = f"Validade: até {p.valid_until.strftime('%d/%m/%Y')}."
        else:
            valid_txt = "Validade: conforme combinado."

        payment_terms = [
            valid_txt,
            "Pagamento: conforme definido no orçamento.",
            "O que não estiver descrito no orçamento não está incluso.",
            "Reagendamento: avisar com antecedência (sujeito à disponibilidade).",
        ]

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": owner.email if owner else "",
        "author_name": owner.display_name if owner and owner.display_name else "",
        "company_name": owner.company_name if owner and owner.company_name else "",
        "phone": owner.phone if owner and owner.phone else "",
        "is_pro": (owner is not None and is_pro_active(owner)),
        "items": items,
        "total_cents": p.total_cents,
        "payment_stages": stages,
        "accept_url": accept_url,
        "payment_terms": payment_terms,
        "logo_mime": getattr(owner, "logo_mime", None) if owner else None,
        "logo_b64": getattr(owner, "logo_b64", None) if owner else None,
    })

    filename = f"orcamento_{p.client_name.replace(' ', '')}_{p.public_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

@app.get("/proposals/{proposal_id}/pdf")
def download_pdf(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    items = [
        {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
        for it in p.items
    ]
    stages = [
        {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents}
        for st in p.payment_stages
    ]

    # termos: usa o congelado do orçamento; se não tiver, cai pro padrão do user
    terms_src = (getattr(p, "terms_text", None) or "") or (getattr(user, "default_terms", "") or "")
    payment_terms = terms_to_list(terms_src)

    if not payment_terms:
        valid_txt = ""
        if getattr(p, "valid_until", None):
            valid_txt = f"Validade: até {p.valid_until.strftime('%d/%m/%Y')}."
        else:
            valid_txt = "Validade: conforme combinado."

        payment_terms = [
            valid_txt,
            "Pagamento: conforme definido no orçamento.",
            "O que não estiver descrito no orçamento não está incluso.",
            "Reagendamento: avisar com antecedência (sujeito à disponibilidade).",
        ]

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email,
        "author_name": user.display_name or "",
        "company_name": user.company_name or "",
        "phone": user.phone or "",
        "is_pro": is_pro_active(user),
        "items": items,
        "total_cents": p.total_cents,
        "payment_stages": stages,
        "accept_url": accept_url,
        "payment_terms": payment_terms,
        "logo_mime": getattr(user, "logo_mime", None),
        "logo_b64": getattr(user, "logo_b64", None),
    })

    filename = f"orcamento_{p.client_name.replace(' ', '')}_{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
# ===== PROFILE / BILLING / PRICING / STATIC =====

@app.get("/limit", response_class=HTMLResponse)
def limit_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("limit.html", {"request": request, "user": user})

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": False, "error": None})


@app.post("/profile")
def profile_save(
    request: Request,
    display_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    cpf_cnpj: str = Form(""),
    pix_key: str = Form(""),
    pix_name: str = Form(""),
    remove_logo: str = Form(""),
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    warning = None

    # remover logo (se existir)
    if (remove_logo or "").strip() == "1":
        user.logo_mime = None
        user.logo_b64 = None

    # upload logo (só PRO) — FREE não quebra: ignora e segue
    if logo is not None and getattr(logo, "filename", ""):
        if not is_pro_active(user):
            warning = "Upload de logo é disponível apenas no plano PRO."
        else:
            try:
                file_bytes = logo.file.read()
                mime, b64 = process_logo_upload(file_bytes)
                user.logo_mime = mime
                user.logo_b64 = b64
            except Exception as e:
                return templates.TemplateResponse("profile.html", {
                    "request": request,
                    "user": user,
                    "saved": False,
                    "error": f"Erro ao salvar logo: {str(e)}",
                })

    # salvar dados do perfil
    user.display_name = display_name.strip() or None
    user.company_name = company_name.strip() or None
    user.phone = phone.strip() or None
    user.cpf_cnpj = cpf_cnpj.strip() or None
    user.pix_key = pix_key.strip() or None
    user.pix_name = pix_name.strip() or None

    db.add(user)
    db.commit()
    db.refresh(user)

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "user": user,
        "saved": True,
        "error": warning,  # aparece como aviso; não quebra o fluxo
    })

@app.get("/billing", response_class=HTMLResponse)
def billing(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("billing.html", {"request": request, "user": user})


from fastapi import Query

@app.get("/pricing")
def pricing_page(
    request: Request,
    reason: str | None = Query(default=None),
    used: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    next: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    # fallback inteligente
    if user and not is_pro_active(user):
        if limit is None:
            limit = int(getattr(user, "proposal_limit", 5) or 5)
        if used is None:

            # Conta propostas do usuário (compatível com diferentes modelos)
            q = db.query(Proposal).filter(Proposal.owner_id == user.id)

            # Se existir algum campo de "arquivado/deletado", aplica filtro.
            if hasattr(Proposal, "archived"):
                q = q.filter(Proposal.archived.is_(False))
            elif hasattr(Proposal, "deleted_at"):
                q = q.filter(Proposal.deleted_at.is_(None))
            elif hasattr(Proposal, "is_deleted"):
                q = q.filter(Proposal.is_deleted.is_(False))

            used = q.count()

            track_event(request, "pricing_view", user_id=user.id if user else None)

    return templates.TemplateResponse("pricing.html", {
        "request": request,
        "user": user,
        "reason": reason,
        "used": used,
        "limit": limit,
        "next": next or "/dashboard",
        "pro_price": "R$ 19,90/mês",
        "pro_cta_url": "/upgrade/pro/pix",
    })



@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/support")
def support_page(request: Request):
    return templates.TemplateResponse("support.html", {"request": request})

    SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "guilhermenevesric@gmail.com")
    SUPPORT_WHATSAPP = os.getenv("SUPPORT_WHATSAPP", "").strip()

import os

@app.get("/support")
def support_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    support_email = os.getenv("SUPPORT_EMAIL") or user.email

    return templates.TemplateResponse("support.html", {
        "request": request,
        "user": user,
        "support_email": support_email,
    })

# ===== ASAAS =====
def ensure_asaas_customer(db: Session, user: User) -> str:
    if user.asaas_customer_id:
        # valida se esse customer existe no ambiente atual (prod/sandbox)
        try:
            vr = requests.get(
                f"{asaas_api_base()}/customers/{user.asaas_customer_id}",
                headers=asaas_headers(),
                timeout=15,
            )
            if vr.status_code == 200:
                return user.asaas_customer_id
        except Exception:
            pass

        # se não existe nesse ambiente, zera pra recriar
        user.asaas_customer_id = None
        user.asaas_subscription_id = None
        db.add(user)
        db.commit()
    if not ASAAS_API_KEY:
        raise RuntimeError("ASAAS_API_KEY não configurado.")
    name = user.display_name or user.company_name or user.email.split("@")[0]
    payload = {"name": name, "email": user.email}
    if user.cpf_cnpj:
        payload["cpfCnpj"] = user.cpf_cnpj

    r = requests.post(f"{asaas_api_base()}/customers", headers=asaas_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro Asaas ao criar customer: {r.status_code} - {r.text}")

    data = r.json()
    cid = data.get("id")
    if not cid:
        raise RuntimeError("Asaas não retornou customer id.")

    user.asaas_customer_id = cid
    db.add(user)
    db.commit()
    return cid


from datetime import date
from fastapi import Query

def _asaas_create_pix_payment(customer_id: str, user_id: int, value: float, due_date: str):
    payload = {
        "customer": customer_id,
        "billingType": "PIX",
        "value": value,
        "dueDate": due_date,
        "description": "PropoFlow PRO (Pix - 30 dias)",
        "externalReference": f"user_{user_id}",
    }
    r = requests.post(
        f"{asaas_api_base()}/payments",
        headers=asaas_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro Asaas ao criar cobrança Pix: {r.status_code} - {r.text}")
    data = r.json()
    pay_id = data.get("id")
    if not pay_id:
        raise RuntimeError(f"Asaas não retornou id da cobrança: {data}")
    return data

def _asaas_get_pix_qr(payment_id: str):
    r = requests.get(
        f"{asaas_api_base()}/payments/{payment_id}/pixQrCode",
        headers=asaas_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Erro ao obter QR Pix: {r.status_code} - {r.text}")
    j = r.json()
    # normalmente vem: encodedImage (base64 PNG) + payload (copia e cola)
    return {
        "encodedImage": j.get("encodedImage") or "",
        "payload": j.get("payload") or "",
        "expirationDate": j.get("expirationDate"),
    }

@app.get("/upgrade/pro/pix", response_class=HTMLResponse)
def upgrade_pro_pix_page(
    request: Request,
    new: int = Query(0),
    pay: str | None = Query(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not ASAAS_API_KEY:
        return HTMLResponse("ASAAS_API_KEY não configurado.", status_code=500)

    # Se já é PRO ativo
    if is_pro_active(user):
        return RedirectResponse("/billing", status_code=302)

    # CPF/CNPJ (mantém pra evitar erro no customer)
    if not getattr(user, "cpf_cnpj", None):
        return templates.TemplateResponse("profile.html", {
            "request": request,
            "user": user,
            "saved": False,
            "error": "Para pagar via Pix, preencha seu CPF/CNPJ no perfil e salve.",
        })

    # customer
    try:
        customer_id = ensure_asaas_customer(db, user)
    except Exception as e:
        return HTMLResponse(f"Erro ao criar/obter customer no Asaas:<br><pre>{str(e)}</pre>", status_code=500)

    error = None
    payment = None
    qr = {"encodedImage": "", "payload": "", "expirationDate": None}

    # Se veio ?pay=..., tenta usar esse pagamento
    # Se new=1, força criar outro
    try:
        if (not pay) or new == 1:
            # vence amanhã pra não expirar rápido
            due = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            payment = _asaas_create_pix_payment(customer_id, user.id, 19.90, due)
            pay = payment.get("id")
        else:
            # busca dados do pagamento existente (pra exibir valor/status)
            rp = requests.get(
                f"{asaas_api_base()}/payments/{pay}",
                headers=asaas_headers(),
                timeout=30,
            )
            if rp.status_code == 200:
                payment = rp.json()
            else:
                payment = {"id": pay}

        # gera QR (aqui é onde estava quebrando antes porque a cobrança não era PIX)
        qr = _asaas_get_pix_qr(pay)

        # Garantia extra: se por algum motivo não for PIX, mostra claramente
        billing_type = (payment or {}).get("billingType")
        if billing_type and billing_type != "PIX":
            error = f"Essa cobrança veio como {billing_type}, não PIX. Gere um novo QR."
    except Exception as e:
        error = str(e)

    # status atual do pagamento
    status = (payment or {}).get("status") or "PENDING"
    value = (payment or {}).get("value") or 19.90
    due_date = (payment or {}).get("dueDate") or ""

    return templates.TemplateResponse("upgrade_pro_pix.html", {
        "request": request,
        "user": user,
        "pay_id": pay,
        "status": status,
        "value": value,
        "due_date": due_date,
        "qr_base64": qr.get("encodedImage", ""),
        "pix_payload": qr.get("payload", ""),
        "qr_expiration": qr.get("expirationDate"),
        "error": error,
    })


@app.get("/upgrade/pro/pix/check")
def upgrade_pro_pix_check(request: Request, sub: str, pay: str, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # consulta o pagamento no Asaas
    r = requests.get(
        f"{asaas_api_base()}/payments/{pay}",
        headers=asaas_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        return RedirectResponse("/upgrade/pro/pix", status_code=302)

    pj = r.json()
    status = (pj.get("status") or "").upper()

    # quando pago, libera PRO por 30 dias (webhook também vai fazer, mas aqui é “aceleração”)
    if status in ("RECEIVED", "CONFIRMED"):
        set_user_pro_month(db, user, paid_until=datetime.utcnow() + timedelta(days=32), subscription_id=sub, customer_id=user.asaas_customer_id)
        return RedirectResponse("/billing", status_code=302)

    return RedirectResponse("/upgrade/pro/pix", status_code=302)



@app.get("/upgrade/pro/pix/status")
def upgrade_pro_pix_status(request: Request, pay: str, sub: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return {"ok": False, "status": "UNAUTH"}

    if not ASAAS_API_KEY:
        return {"ok": False, "status": "NO_ASAAS"}

    # consulta o pagamento no Asaas
    try:
        r = requests.get(
            f"{asaas_api_base()}/payments/{pay}",
            headers=asaas_headers(),
            timeout=20,
        )
    except Exception:
        return {"ok": False, "status": "ERROR"}

    if r.status_code != 200:
        return {"ok": False, "status": "ERROR"}

    pj = r.json()
    st = (pj.get("status") or "").upper()

    # pago -> libera pro (aceleração; webhook também faz)
    if st in ("RECEIVED", "CONFIRMED"):
        # tenta usar subscription_id se veio
        if sub:
            user.asaas_subscription_id = user.asaas_subscription_id or sub
        set_user_pro_month(
            db,
            user,
            paid_until=datetime.utcnow() + timedelta(days=32),
            subscription_id=getattr(user, "asaas_subscription_id", None),
            customer_id=getattr(user, "asaas_customer_id", None),
        )
        track_event(request, "pix_checkout_open", user_id=user.id)

        return {"ok": True, "status": st, "paid": True}

    return {"ok": True, "status": st, "paid": False}



@app.get("/upgrade/pro")
def upgrade_pro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not ASAAS_API_KEY:
        return HTMLResponse("ASAAS_API_KEY não configurado.", status_code=500)

    if is_pro_active(user):
        return RedirectResponse("/billing", status_code=302)

    if not getattr(user, "cpf_cnpj", None):
        return templates.TemplateResponse("profile.html", {
            "request": request,
            "user": user,
            "saved": False,
            "error": "Para assinar o PRO, preencha seu CPF/CNPJ no perfil e salve.",
        })

    customer_id = ensure_asaas_customer(db, user)
    next_due = date.today().strftime("%Y-%m-%d")
    payload = {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": 19.90,
        "nextDueDate": next_due,
        "cycle": "MONTHLY",
        "description": "PropoFlow Pro (assinatura mensal)",
        "externalReference": f"user_{user.id}",
    }
    r = requests.post(f"{asaas_api_base()}/subscriptions", headers=asaas_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        return HTMLResponse(f"Erro Asaas ao criar assinatura: {r.status_code}<br><pre>{r.text}</pre>", status_code=500)

    sub = r.json()
    sub_id = sub.get("id")
    user.asaas_subscription_id = sub_id
    db.add(user)
    db.commit()

    rp = requests.get(f"{asaas_api_base()}/subscriptions/{sub_id}/payments", headers=asaas_headers(), timeout=30)
    payments = rp.json()
    data_list = payments.get("data") if isinstance(payments, dict) else None
    first = data_list[0] if data_list else {}
    invoice_url = first.get("invoiceUrl")
    if not invoice_url:
        return HTMLResponse("Assinatura criada, mas não achei invoiceUrl.", status_code=500)
    return RedirectResponse(invoice_url, status_code=302)




@app.get("/admin/metrics")
def admin_metrics(request: Request, key: str = "", days: int = 7, db: Session = Depends(get_db)):
    admin_key = os.getenv("ADMIN_METRICS_KEY", "")
    if not admin_key or key != admin_key:
        return PlainTextResponse("forbidden", status_code=403)

    since = datetime.utcnow() - timedelta(days=days)

    def c(name: str) -> int:
        return db.query(Event).filter(Event.name == name, Event.created_at >= since).count()

    data = {
        "days": days,
        "landing_view": c("landing_view"),
        "register_success": c("register_success"),
        "verify_success": c("verify_success"),
        "wizard_start": c("wizard_start"),
        "proposal_created": c("proposal_created"),
        "pricing_view": c("pricing_view"),
        "pix_checkout_open": c("pix_checkout_open"),
        "pro_activated": c("pro_activated"),
    }

    # taxas básicas
    lv = max(data["landing_view"], 1)
    rs = data["register_success"]
    vs = data["verify_success"]
    pc = data["proposal_created"]
    pro = data["pro_activated"]

    rates = {
        "reg_rate": round((rs/lv)*100, 1),
        "verify_rate": round((vs/max(rs,1))*100, 1),
        "first_proposal_rate": round((pc/max(vs,1))*100, 1),
        "pro_rate_from_reg": round((pro/max(rs,1))*100, 2),
    }

    return templates.TemplateResponse("admin_metrics.html", {
        "request": request,
        "data": data,
        "rates": rates
    })


@app.get("/wizard", response_class=HTMLResponse)
def wizard(request: Request, step: int = 1,
           client_id: int = 0, service_id: int = 0,
           client_name: str = "", client_whatsapp: str = "",
           project_name: str = "", deadline: str = "",
           price: str = "", validity_days: int = 7,
           description: str = "", payment_plan: str = "avista",
           db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    services = db.query(Service).filter(Service.owner_id == user.id, Service.archived.is_(False)).order_by(Service.title.asc()).all()
    clients = db.query(Client).filter(Client.owner_id == user.id, Client.archived.is_(False)).order_by(Client.favorite.desc(), Client.name.asc()).all()

    # Prefill do cliente salvo
    if step >= 2 and client_id:
        c = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
        if c:
            if not client_name:
                client_name = c.name
            if not client_whatsapp and c.whatsapp:
                client_whatsapp = c.whatsapp

    # Prefill do serviço salvo
    if step >= 3 and service_id:
        s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
        if s:
            if not project_name:
                project_name = s.title
            if not deadline and s.default_deadline:
                deadline = s.default_deadline
            if not description and s.default_description:
                description = s.default_description
            if not price and (s.default_price_cents or 0) > 0:
                price = cents_to_brl(s.default_price_cents)
            if payment_plan in ("", "avista") and s.default_payment_plan:
                payment_plan = s.default_payment_plan

    # Validações simples
    error = None
    step = max(1, min(int(step or 1), 3))
    if step == 2 and not (client_id or client_name.strip()):
        error = "Escolha um cliente ou digite o nome."
    if step == 3 and not (service_id or project_name.strip()):
        error = "Escolha um serviço ou digite o nome do serviço."
    if step == 3 and not description.strip():
        error = "Escreva em 1 linha o que será feito."

        track_event(request, "wizard_start", user_id=user.id)


    return templates.TemplateResponse("wizard.html", {
        "request": request,
        "step": step,
        "error": error,
        "services": services,
        "clients": clients,

        "client_id": client_id,
        "service_id": service_id,

        "client_name": client_name,
        "client_whatsapp": client_whatsapp,

        "project_name": project_name,
        "deadline": deadline,
        "price": price,
        "validity_days": validity_days,
        "description": description,
        "payment_plan": payment_plan,
        "default_validity_days": getattr(user, "default_validity_days", 7),
        "default_payment_plan": getattr(user, "default_payment_plan", "avista"),
    })





@app.post("/wizard/create")
def wizard_create(
    request: Request,
    service_id: int = Form(0),
    client_id: int = Form(0),
    client_name: str = Form(...),
    client_whatsapp: str = Form(""),
    project_name: str = Form(...),
    deadline: str = Form(...),
    price: str = Form(""),
    validity_days: int = Form(7),
    description: str = Form(...),
    payment_plan: str = Form("avista"),

    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),

    db: Session = Depends(get_db),
):
    # NÃO tracke aqui pq ainda não existe proposal_id
    return create_proposal(
        request=request,
        service_id=service_id,
        client_id=client_id,
        client_name=client_name,
        client_whatsapp=client_whatsapp,
        project_name=project_name,
        description=description,
        price=price,
        deadline=deadline,
        validity_days=validity_days,
        payment_plan=payment_plan,
        item_desc=item_desc,
        item_qty=item_qty,
        item_unit=item_unit,
        item_unit_price=item_unit_price,
        db=db
    )


from urllib.parse import quote
import re

def normalize_whatsapp(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    # se vier sem DDI, assume BR
    if digits.startswith("55"):
        return digits
    if len(digits) <= 11:
        return "55" + digits
    return digits

def proposal_public_link(request, public_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/p/{public_id}"

def build_whatsapp_text(user, p, link: str) -> str:
    # se você já tem template no user, usa; senão fallback
    template = getattr(user, "default_message_template", None) or (
        "Olá {cliente}! Segue seu orçamento:\n{link}\n\n"
        "Serviço: {servico}\nValor: {valor}\nPrazo: {prazo}\n\n"
        "Se quiser, posso te explicar rapidinho aqui."
    )
    return (template
        .replace("{cliente}", p.client_name or "tudo bem")
        .replace("{link}", link)
        .replace("{servico}", p.project_name or "")
        .replace("{valor}", p.price or "")
        .replace("{prazo}", p.deadline or "")
    )

@app.get("/proposals/{proposal_id}/send")
def proposal_send_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    link = proposal_public_link(request, p.public_id)
    wa_phone = normalize_whatsapp(p.client_whatsapp)
    wa_text = build_whatsapp_text(user, p, link)
    wa_url = None
    if wa_phone:
        wa_url = f"https://wa.me/{wa_phone}?text={quote(wa_text)}"

    return templates.TemplateResponse("send.html", {
        "request": request,
        "user": user,
        "p": p,
        "link": link,
        "wa_url": wa_url,
        "wa_text": wa_text,
    })


@app.post("/wizard/step2", response_class=HTMLResponse)
def wizard_step2(
    request: Request,
    client_id: int = Form(0),
    client_name: str = Form(""),
    client_whatsapp: str = Form(""),
    service_id: int = Form(0),

    project_name: str = Form(""),
    deadline: str = Form(""),
    price: str = Form(""),
    validity_days: int = Form(7),
    description: str = Form(""),
    payment_plan: str = Form("avista"),

    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),

    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    services = db.query(Service).filter(Service.owner_id == user.id, Service.archived.is_(False)).order_by(Service.title.asc()).all()
    clients = db.query(Client).filter(Client.owner_id == user.id, Client.archived.is_(False)).order_by(Client.favorite.desc(), Client.name.asc()).all()

    # prefill cliente salvo
    if client_id and not client_name.strip():
        c = db.query(Client).filter(Client.id == client_id, Client.owner_id == user.id, Client.archived.is_(False)).first()
        if c:
            client_name = c.name
            if not client_whatsapp and c.whatsapp:
                client_whatsapp = c.whatsapp

    # prefill serviço salvo
    if service_id:
        s = db.query(Service).filter(Service.id == service_id, Service.owner_id == user.id, Service.archived.is_(False)).first()
        if s:
            if not project_name.strip():
                project_name = s.title
            if not deadline.strip() and s.default_deadline:
                deadline = s.default_deadline
            if not description.strip() and s.default_description:
                description = s.default_description
            if not price.strip() and (s.default_price_cents or 0) > 0:
                price = cents_to_brl(s.default_price_cents)
            if (payment_plan in ("", "avista")) and s.default_payment_plan:
                payment_plan = s.default_payment_plan

    # validações simples
    error = None
    if not (client_id or client_name.strip()):
        error = "Escolha um cliente ou digite o nome."
    elif not (service_id or project_name.strip()):
        error = "Escolha um serviço ou digite o nome do serviço."
    elif not (description or "").strip():
        error = "Escreva em 1 linha a descrição do serviço."

    # montar itens limpos
    items = []
    total_cents = 0
    n = min(len(item_desc), len(item_qty), len(item_unit_price))
    for i in range(n):
        d = (item_desc[i] or "").strip()
        if not d:
            continue
        try:
            q = float(str(item_qty[i] or "1").replace(",", "."))
            if q <= 0:
                q = 1.0
        except Exception:
            q = 1.0
        up = brl_to_cents(item_unit_price[i] or "0")
        line = int(round(q * up))
        total_cents += line
        items.append({
            "desc": d,
            "qty": str(q).rstrip("0").rstrip(".") if "." in str(q) else str(q),
            "unit_price": (item_unit_price[i] or "").strip(),
            "total_brl": cents_to_brl(line),
        })

    return templates.TemplateResponse("wizard.html", {
        "request": request,
        "step": 3,
        "error": error,
        "services": services,
        "clients": clients,

        "client_id": client_id,
        "service_id": service_id,

        "client_name": client_name,
        "client_whatsapp": client_whatsapp,

        "project_name": project_name,
        "deadline": deadline,
        "price": price,
        "validity_days": validity_days,
        "description": description,
        "payment_plan": payment_plan,

        "items": items,
        "items_total_brl": cents_to_brl(total_cents),

        "default_validity_days": getattr(user, "default_validity_days", 7),
        "default_payment_plan": getattr(user, "default_payment_plan", "avista"),
    })

@app.post("/webhooks/asaas")
async def webhooks_asaas(request: Request, db: Session = Depends(get_db)):
    if ASAAS_WEBHOOK_TOKEN:
        token = request.headers.get("asaas-access-token") or request.headers.get("Asaas-Access-Token")
        if token != ASAAS_WEBHOOK_TOKEN:
            return HTMLResponse("unauthorized", status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    event = (body.get("event") or "").upper()
    payment = body.get("payment") or {}
    subscription = body.get("subscription") or {}

    external_ref = ""
    if isinstance(payment, dict):
        external_ref = payment.get("externalReference") or ""
    if not external_ref and isinstance(subscription, dict):
        external_ref = subscription.get("externalReference") or ""

    if not external_ref.startswith("user_"):
        return {"ok": True}

    try:
        user_id = int(external_ref.replace("user_", ""))
    except Exception:
        return {"ok": True}

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"ok": True}

    if event in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_APPROVED"):
        paid_until = _now() + timedelta(days=32)
        set_user_pro_month(db, user, paid_until, subscription_id=user.asaas_subscription_id, customer_id=user.asaas_customer_id)
        return {"ok": True}

    return {"ok": True}

@app.head("/")
def head_root():
    return Response(status_code=200)

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

def send_email(to_email: str, subject: str, body: str):
    brevo_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.getenv("BREVO_SENDER_NAME", "PropoFlow").strip()

    if not (brevo_key and sender_email):
        raise RuntimeError("Brevo API não configurada (BREVO_API_KEY e BREVO_SENDER_EMAIL).")

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }

    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": brevo_key,
            "Content-Type": "application/json",
            "accept": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo API erro {r.status_code}: {r.text}")