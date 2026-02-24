from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from passlib.hash import pbkdf2_sha256
from datetime import datetime, timedelta, date
import os
import requests
import subprocess
import sys

from db import SessionLocal, engine, Base
from models import User, Proposal
from pdf_gen import generate_proposal_pdf


# ====== roda migração leve (SQLite/Postgres) ======
try:
    subprocess.run([sys.executable, "migrate.py"], check=False)
except Exception:
    pass

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


COOKIE_NAME = "user_id"

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")

# ====== ASAAS CONFIG ======
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()  # sandbox | prod
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()

def asaas_api_base() -> str:
    # Asaas usa api-sandbox no sandbox e api.asaas.com em prod
    if ASAAS_ENV == "prod":
        return "https://api.asaas.com/v3"
    return "https://api-sandbox.asaas.com/v3"

def asaas_headers():
    # Asaas autentica com header access_token (não é Bearer)
    return {
        "access_token": ASAAS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_current_user(request: Request, db: Session):
    user_id = request.cookies.get(COOKIE_NAME)
    if not user_id or user_id in ("None", "null", ""):
        return None
    try:
        user_id_int = int(user_id)
    except ValueError:
        return None
    return db.query(User).filter(User.id == user_id_int).first()


def is_pro_active(user: User) -> bool:
    if user.plan == "pro":
        # pro "flag" pode existir, mas a validade é no paid_until
        if user.paid_until and user.paid_until >= datetime.utcnow():
            return True
        # Se plan=pro e paid_until vazio (caso antigo), considera ativo
        if not user.paid_until:
            return True
        return False
    # caso plan não pro, ainda pode estar pago (se você quiser usar só paid_until)
    if user.paid_until and user.paid_until >= datetime.utcnow():
        return True
    return False


def set_user_pro_month(db: Session, user: User, paid_until: datetime, subscription_id: str | None = None, customer_id: str | None = None):
    user.plan = "pro"
    user.proposal_limit = 999999
    user.delete_credits = 999999
    user.plan_updated_at = datetime.utcnow()
    user.paid_until = paid_until
    if subscription_id:
        user.asaas_subscription_id = subscription_id
    if customer_id:
        user.asaas_customer_id = customer_id
    db.add(user)
    db.commit()


def set_user_free(db: Session, user: User):
    user.plan = "free"
    user.proposal_limit = 5
    user.delete_credits = user.delete_credits if (user.delete_credits is not None) else 1
    user.plan_updated_at = datetime.utcnow()
    db.add(user)
    db.commit()


def parse_asaas_date(d: str) -> datetime | None:
    # Asaas normalmente usa YYYY-MM-DD
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        return None


# ====== HOME ======
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user_id = request.cookies.get(COOKIE_NAME)
    user = get_current_user(request, db)

    if not user:
        resp = RedirectResponse("/login", status_code=302)
        if user_id in ("None", "null", ""):
            resp.delete_cookie(COOKIE_NAME)
        return resp

    return RedirectResponse("/dashboard", status_code=302)


# ====== AUTH ======
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if (not user) or (not pbkdf2_sha256.verify(password, user.password_hash)):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email ou senha inválidos."},
        )

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
    return resp


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Esse email já existe. Faça login."},
        )

    user = User(
        email=email,
        password_hash=pbkdf2_sha256.hash(password),
        proposal_limit=5,
        plan="free",
        delete_credits=1,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ====== DASHBOARD ======
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    q = db.query(Proposal).filter(Proposal.owner_id == user.id)

    if status == "accepted":
        q = q.filter(Proposal.accepted_at.isnot(None))
    elif status == "pending":
        q = q.filter(Proposal.accepted_at.is_(None))

    proposals = q.order_by(Proposal.created_at.desc()).all()

    total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    accepted = db.query(Proposal).filter(
        Proposal.owner_id == user.id,
        Proposal.accepted_at.isnot(None)
    ).count()
    rate = round((accepted / total) * 100) if total else 0

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "proposals": proposals,
        "total": total,
        "accepted": accepted,
        "rate": rate,
        "status": status,
    })


# ====== PROPOSALS ======
@app.get("/proposals/new", response_class=HTMLResponse)
def new_proposal_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("new_proposal.html", {"request": request, "error": None})


@app.post("/proposals/new")
def create_proposal(
    request: Request,
    client_name: str = Form(...),
    project_name: str = Form(...),
    description: str = Form(...),
    price: str = Form(...),
    deadline: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # se for PRO ativo, ignora limite
    if not is_pro_active(user):
        count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
        if count >= (user.proposal_limit or 5):
            proposals = (
                db.query(Proposal)
                .filter(Proposal.owner_id == user.id)
                .order_by(Proposal.created_at.desc())
                .all()
            )

            total = count
            accepted = db.query(Proposal).filter(
                Proposal.owner_id == user.id,
                Proposal.accepted_at.isnot(None)
            ).count()
            rate = round((accepted / total) * 100) if total else 0

            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "proposals": proposals,
                "total": total,
                "accepted": accepted,
                "rate": rate,
                "status": "all",
                "error": f"Você atingiu o limite do plano gratuito ({user.proposal_limit or 5} propostas).",
                "show_upgrade": True,
            })

    p = Proposal(
        client_name=client_name.strip(),
        project_name=project_name.strip(),
        description=description.strip(),
        price=price.strip(),
        deadline=deadline.strip(),
        owner_id=user.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    return RedirectResponse(f"/proposals/{p.id}/pdf", status_code=302)


@app.post("/proposals/{proposal_id}/delete")
def delete_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()

    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    # Free: só 1 exclusão via delete_credits (PRO ativo ignora)
    if not is_pro_active(user) and user.plan == "free":
        credits = user.delete_credits or 0
        if credits <= 0:
            proposals = (
                db.query(Proposal)
                .filter(Proposal.owner_id == user.id)
                .order_by(Proposal.created_at.desc())
                .all()
            )
            total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
            accepted = db.query(Proposal).filter(
                Proposal.owner_id == user.id,
                Proposal.accepted_at.isnot(None)
            ).count()
            rate = round((accepted / total) * 100) if total else 0

            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "proposals": proposals,
                "total": total,
                "accepted": accepted,
                "rate": rate,
                "status": "all",
                "error": "No plano gratuito você só pode excluir 1 proposta. Faça upgrade para excluir ilimitado.",
                "show_upgrade": True,
            })

        user.delete_credits = credits - 1
        db.add(user)

    db.delete(p)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


@app.get("/proposals/{proposal_id}/duplicate")
def duplicate_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    original = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()

    if not original:
        return RedirectResponse("/dashboard", status_code=302)

    new_p = Proposal(
        client_name=original.client_name,
        project_name=original.project_name,
        description=original.description,
        price=original.price,
        deadline=original.deadline,
        owner_id=user.id
    )

    db.add(new_p)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


# ====== PUBLIC PROPOSAL ======
@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(
    public_id: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    if p.accepted_at is not None:
        owner = db.query(User).filter(User.id == p.owner_id).first()
        base_url = str(request.base_url).rstrip("/")
        return templates.TemplateResponse("accepted.html", {
            "request": request,
            "p": p,
            "owner": owner,
            "base_url": base_url
        })

    p.accepted_at = datetime.utcnow()
    p.accepted_name = name.strip()
    p.accepted_email = email.strip()
    db.commit()
    db.refresh(p)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse("accepted.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url
    })


@app.get("/p/{public_id}/pdf")
def public_pdf(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    user = db.query(User).filter(User.id == p.owner_id).first()
    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email if user else "",
        "author_name": user.display_name if user and user.display_name else "",
        "company_name": user.company_name if user and user.company_name else "",
        "phone": user.phone if user and user.phone else "",
        "is_pro": (user is not None and is_pro_active(user)),
    })

    filename = f"proposta_{p.client_name.replace(' ', '')}{p.public_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/p/{public_id}", response_class=HTMLResponse)
def public_proposal(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = str(request.base_url).rstrip("/")

    return templates.TemplateResponse("proposal_public.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url
    })


@app.get("/proposals/{proposal_id}/pdf")
def download_pdf(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()

    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email if user else "",
        "author_name": user.display_name if user and user.display_name else "",
        "company_name": user.company_name if user and user.company_name else "",
        "phone": user.phone if user and user.phone else "",
        "is_pro": (user is not None and is_pro_active(user)),
    })

    filename = f"proposta_{p.client_name.replace(' ', '')}{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# ====== PROFILE ======
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
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.display_name = display_name.strip() or None
    user.company_name = company_name.strip() or None
    user.phone = phone.strip() or None
    user.cpf_cnpj = cpf_cnpj.strip() or None
    db.add(user)
    db.commit()

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": True, "error": None})


# ====== BILLING & PRICING ======
@app.get("/billing", response_class=HTMLResponse)
def billing(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    return templates.TemplateResponse("billing.html", {"request": request, "user": user})


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse("pricing.html", {"request": request, "user": user})


# ====== ASAAS: CREATE/GET CUSTOMER ======
def ensure_asaas_customer(db: Session, user: User) -> str:
    if user.asaas_customer_id:
        return user.asaas_customer_id

    if not ASAAS_API_KEY:
        raise RuntimeError("ASAAS_API_KEY não configurado no Render.")

    # Asaas geralmente precisa de name/email e (muitas vezes) cpfCnpj
    name = user.display_name or user.company_name or user.email.split("@")[0]
    payload = {
        "name": name,
        "email": user.email,
    }
    if user.cpf_cnpj:
        payload["cpfCnpj"] = user.cpf_cnpj

    r = requests.post(
        f"{asaas_api_base()}/customers",
        headers=asaas_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro Asaas ao criar customer: {r.status_code} - {r.text}")

    data = r.json()
    customer_id = data.get("id")
    if not customer_id:
        raise RuntimeError(f"Asaas não retornou customer id: {data}")

    user.asaas_customer_id = customer_id
    db.add(user)
    db.commit()

    return customer_id


# ====== UPGRADE PRO (ASSINATURA ASAAS) ======
@app.get("/upgrade/pro")
def upgrade_pro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not ASAAS_API_KEY:
        return HTMLResponse("ASAAS_API_KEY não configurado no Render.", status_code=500)

    # Se já está ativo, manda pro billing
    if is_pro_active(user):
        return RedirectResponse("/billing", status_code=302)

    # CPF/CNPJ recomendado/necessário para cobrança
    if not user.cpf_cnpj:
        return templates.TemplateResponse("profile.html", {
            "request": request,
            "user": user,
            "saved": False,
            "error": "Para assinar o PRO, preencha seu CPF/CNPJ (necessário para cobrança) e salve.",
        })

    try:
        customer_id = ensure_asaas_customer(db, user)
    except Exception as e:
        return HTMLResponse(f"Erro ao criar cliente no Asaas: <pre>{str(e)}</pre>", status_code=500)

    # cria assinatura mensal
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