from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.hash import pbkdf2_sha256
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import os
import requests

from db import SessionLocal, engine, Base
from models import User, Proposal
from pdf_gen import generate_proposal_pdf

Base.metadata.create_all(bind=engine)

import subprocess, sys, os
try:
    subprocess.run([sys.executable, "migrate.py"], check=False)
except Exception:
    pass

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

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_API = "https://api.mercadopago.com"

def get_current_user(request: Request, db: Session):
    user_id = request.cookies.get(COOKIE_NAME)
    if not user_id or user_id in ("None", "null", ""):
        return None
    try:
        user_id_int = int(user_id)
    except ValueError:
        return None
    return db.query(User).filter(User.id == user_id_int).first()


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
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if (not user) or (not pbkdf2_sha256.verify(password, user.password_hash)):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email ou senha inválidos."}
        )

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
    return resp


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Esse email já existe. Faça login."}
        )

    user = User(
        email=email,
        password_hash=pbkdf2_sha256.hash(password),
        proposal_limit=5,
        plan="free"
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
        # ajuda o template a saber se mostra o aviso do 1 delete
        "free_delete_available": (user.plan == "free")
    })


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(
    public_id: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db)
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
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

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
            "free_delete_available": (user.plan == "free")
        })

    p = Proposal(
        client_name=client_name.strip(),
        project_name=project_name.strip(),
        description=description.strip(),
        price=price.strip(),
        deadline=deadline.strip(),
        owner_id=user.id
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    return RedirectResponse(f"/proposals/{p.id}/pdf", status_code=302)


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
    })

    filename = f"proposta_{p.client_name.replace(' ', '_')}_{p.public_id}.pdf"
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

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
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
    })

    filename = f"proposta_{p.client_name.replace(' ', '_')}_{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


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

    # Free: 1 exclusão total -> depois vira free_used_delete
    if user.plan.startswith("free"):
        if user.plan == "free_used_delete":
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
                "free_delete_available": False
            })

        # primeira exclusão no free
        user.plan = "free_used_delete"

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


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": False})


@app.post("/profile")
def profile_save(
    request: Request,
    display_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.display_name = display_name.strip() or None
    user.company_name = company_name.strip() or None
    user.phone = phone.strip() or None
    db.add(user)
    db.commit()

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": True})


@app.get("/checkout/pro")
def checkout_pro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not MP_ACCESS_TOKEN:
        return HTMLResponse("MP_ACCESS_TOKEN não configurado no Render (Environment).", status_code=500)

    base_url = str(request.base_url).rstrip("/")

    # Preço do PRO (MVP) - ajuste aqui
    price_brl = 19.90

    payload = {
        "items": [
            {
                "title": "PropoFlow PRO (mensal)",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": price_brl
            }
        ],
        "payer": {"email": user.email},
        "external_reference": f"user:{user.id}:pro",
        "back_urls": {
            "success": f"{base_url}/payment/success",
            "pending": f"{base_url}/payment/pending",
            "failure": f"{base_url}/payment/failure"
        },
        "auto_return": "approved",
        "notification_url": f"{base_url}/webhooks/mercadopago"
    }

    r = requests.post(
        f"{MP_API}/checkout/preferences",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        json=payload,
        timeout=30
    )

    if r.status_code not in (200, 201):
        return HTMLResponse(f"Erro ao criar checkout MP: {r.status_code}<br>{r.text}", status_code=500)

    data = r.json()
    init_point = data.get("init_point") or data.get("sandbox_init_point")

    if not init_point:
        return HTMLResponse("Mercado Pago não retornou init_point.", status_code=500)

    return RedirectResponse(init_point, status_code=302)

@app.get("/payment/success", response_class=HTMLResponse)
def payment_success(request: Request):
    return templates.TemplateResponse("payment_success.html", {"request": request})

@app.get("/payment/pending", response_class=HTMLResponse)
def payment_pending(request: Request):
    return templates.TemplateResponse("payment_pending.html", {"request": request})

@app.get("/payment/failure", response_class=HTMLResponse)
def payment_failure(request: Request):
    return templates.TemplateResponse("payment_failure.html", {"request": request})

@app.post("/webhooks/mercadopago")
async def webhook_mercadopago(request: Request, db: Session = Depends(get_db)):
    if not MP_ACCESS_TOKEN:
        return {"ok": True}

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Mercado Pago pode mandar "type":"payment" e "data":{"id":"..."}
    payment_id = None
    if isinstance(body, dict):
        if "data" in body and isinstance(body["data"], dict) and body["data"].get("id"):
            payment_id = str(body["data"]["id"])
        elif body.get("id"):
            payment_id = str(body["id"])

    if not payment_id:
        return {"ok": True}

    # Busca detalhes do pagamento diretamente na API do MP (mais confiável que o payload)
    r = requests.get(
        f"{MP_API}/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        timeout=30
    )

    if r.status_code != 200:
        return {"ok": True}

    pay = r.json()
    status = (pay.get("status") or "").lower()
    external_ref = pay.get("external_reference") or ""

    # Só libera se aprovado e for do nosso plano PRO
    if status == "approved" and external_ref.startswith("user:") and ":pro" in external_ref:
        try:
            user_id = int(external_ref.split(":")[1])
        except Exception:
            return {"ok": True}

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {"ok": True}

        # Idempotência: se já processou esse payment_id, não repete
        if user.mp_last_payment_id == str(payment_id) and user.plan == "pro":
            return {"ok": True}

        user.plan = "pro"
        user.proposal_limit = 999999
        user.delete_credits = 999999
        user.plan_updated_at = datetime.utcnow()
        user.mp_last_payment_id = str(payment_id)

        db.add(user)
        db.commit()

    return {"ok": True}

import os
import mercadopago

PRO_PRICE = 19.00  # preço do Pro

@app.post("/mp/create_pro_checkout")
def mp_create_pro_checkout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        return HTMLResponse("MP_ACCESS_TOKEN não configurado no Render.", status_code=500)

    sdk = mercadopago.SDK(access_token)

    base_url = str(request.base_url).rstrip("/")

    preference_data = {
        "items": [
            {
                "title": "PropoFlow PRO (mensal)",
                "quantity": 1,
                "unit_price": float(PRO_PRICE),
                "currency_id": "BRL",
            }
        ],
        "payer": {
            "email": user.email
        },
        "notification_url": f"{base_url}/webhooks/mercadopago",
        "external_reference": str(user.id),  # IMPORTANTÍSSIMO: liga o pagamento ao usuário
        "back_urls": {
            "success": f"{base_url}/dashboard",
            "failure": f"{base_url}/pricing",
            "pending": f"{base_url}/dashboard"
        },
        "auto_return": "approved",
    }

    preference_response = sdk.preference().create(preference_data)
    pref = preference_response.get("response", {})

    init_point = pref.get("init_point")
    if not init_point:
        return HTMLResponse("Não foi possível criar checkout no Mercado Pago.", status_code=500)

    return RedirectResponse(init_point, status_code=302)


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse("pricing.html", {"request": request})

from fastapi import Header
import json

@app.post("/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    x_signature: str = Header(default=None),
    x_request_id: str = Header(default=None),
):
    # Mercado Pago pode mandar JSON ou form. Vamos tentar ler tudo.
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        payload = {"raw": raw.decode("utf-8", errors="ignore")}

    # Log simples pra você ver no Render Logs
    print("=== MERCADOPAGO WEBHOOK RECEBIDO ===")
    print("x-signature:", x_signature)
    print("x-request-id:", x_request_id)
    print("payload:", payload)

    # IMPORTANTE: responder 200 rápido
    return {"ok": True}






