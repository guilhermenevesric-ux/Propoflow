# models.py
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from db import Base
import uuid


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    display_name = Column(String(255), nullable=True)
    company_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)

    cpf_cnpj = Column(String(18), nullable=True)

    # Marca / cobrança Pix (MVP: manual)
    pix_key = Column(String(120), nullable=True)       # chave pix (cpf/cnpj/email/telefone/aleatoria)
    pix_name = Column(String(120), nullable=True)      # nome do recebedor (opcional)

    plan = Column(String(20), default="free")          # free | pro
    proposal_limit = Column(Integer, default=5)
    delete_credits = Column(Integer, default=1)

    paid_until = Column(DateTime, nullable=True)

    asaas_customer_id = Column(String(40), nullable=True)
    asaas_subscription_id = Column(String(40), nullable=True)

    plan_updated_at = Column(DateTime, nullable=True)

    proposals = relationship("Proposal", back_populates="owner")
    sessions = relationship("UserSession", back_populates="user")


class UserSession(Base):
    """
    Sessão segura: cookie guarda token aleatório.
    No banco guardamos o hash (sha256) desse token.
    """
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # sha256 hex = 64 chars
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


class Proposal(Base):
    __tablename__ = "proposals"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(16), unique=True, index=True, nullable=False, default=lambda: uuid.uuid4().hex[:12])

    client_name = Column(String(255), nullable=False)
    client_whatsapp = Column(String(30), nullable=True)   # NOVO
    project_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    price = Column(String(50), nullable=False)
    deadline = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Pipeline
    status = Column(String(20), default="created")         # created | sent | viewed | accepted
    valid_until = Column(DateTime, nullable=True)          # NOVO

    # Tracking
    view_count = Column(Integer, default=0)                # NOVO
    first_viewed_at = Column(DateTime, nullable=True)      # NOVO
    last_viewed_at = Column(DateTime, nullable=True)       # NOVO
    last_activity_at = Column(DateTime, nullable=True)     # NOVO (sent/viewed/accepted)

    # Aceite
    accepted_at = Column(DateTime, nullable=True)
    accepted_name = Column(String(255), nullable=True)
    accepted_email = Column(String(255), nullable=True)

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="proposals")

    followups = relationship("FollowUpSchedule", back_populates="proposal", cascade="all, delete-orphan")


class FollowUpSchedule(Base):
    __tablename__ = "followup_schedules"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)

    step = Column(Integer, nullable=False)  # 1, 3, 7 (dias)
    due_at = Column(DateTime, nullable=False, index=True)

    status = Column(String(20), default="pending")  # pending | sent | skipped
    sent_at = Column(DateTime, nullable=True)

    proposal = relationship("Proposal", back_populates="followups")