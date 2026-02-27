from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
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

    pix_key = Column(String(120), nullable=True)
    pix_name = Column(String(120), nullable=True)
    pix_city = Column(String(60), nullable=True)

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
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


class Proposal(Base):
    __tablename__ = "proposals"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(16), unique=True, index=True, nullable=False, default=lambda: uuid.uuid4().hex[:12])

    client_name = Column(String(255), nullable=False)
    client_whatsapp = Column(String(30), nullable=True)

    project_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)

    # legacy (mantém)
    price = Column(String(50), nullable=False, default="")     # pode ficar vazio agora
    deadline = Column(String(100), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    # pipeline
    status = Column(String(20), default="created")         # created | sent | viewed | accepted
    valid_until = Column(DateTime, nullable=True)

    # tracking
    view_count = Column(Integer, default=0)
    first_viewed_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)

    # versão
    revision = Column(Integer, default=1)
    updated_at = Column(DateTime, nullable=True)

    # regras do orçamento
    overhead_percent = Column(Integer, default=10)         # imprevistos (%)
    margin_percent = Column(Integer, default=0)            # margem simples (%)
    total_cents = Column(Integer, default=0)               # total calculado em centavos

    # aceite
    accepted_at = Column(DateTime, nullable=True)
    accepted_name = Column(String(255), nullable=True)
    accepted_email = Column(String(255), nullable=True)

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="proposals")

    items = relationship("ProposalItem", back_populates="proposal", cascade="all, delete-orphan")
    versions = relationship("ProposalVersion", back_populates="proposal", cascade="all, delete-orphan")
    payment_stages = relationship("PaymentStage", back_populates="proposal", cascade="all, delete-orphan")


class ProposalItem(Base):
    __tablename__ = "proposal_items"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)

    sort = Column(Integer, default=0)
    description = Column(String(255), nullable=False)
    unit = Column(String(30), nullable=True)
    qty = Column(Float, default=1.0)

    # valor unitário em centavos
    unit_price_cents = Column(Integer, default=0)
    line_total_cents = Column(Integer, default=0)

    proposal = relationship("Proposal", back_populates="items")


class ProposalVersion(Base):
    __tablename__ = "proposal_versions"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)

    revision = Column(Integer, nullable=False)
    snapshot_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    proposal = relationship("Proposal", back_populates="versions")


class PaymentStage(Base):
    __tablename__ = "payment_stages"

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)

    title = Column(String(80), nullable=False)           # Sinal / Etapa 1 / Etapa 2
    percent = Column(Integer, default=0)                 # % do total
    amount_cents = Column(Integer, default=0)            # calculado
    due_at = Column(DateTime, nullable=True)             # só usamos p/ sinal no MVP
    status = Column(String(20), default="pending")       # pending | paid
    paid_at = Column(DateTime, nullable=True)

    proposal = relationship("Proposal", back_populates="payment_stages")

    reminders = relationship("PaymentReminder", back_populates="stage", cascade="all, delete-orphan")


class PaymentReminder(Base):
    __tablename__ = "payment_reminders"

    id = Column(Integer, primary_key=True, index=True)
    stage_id = Column(Integer, ForeignKey("payment_stages.id"), nullable=False, index=True)

    due_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(20), default="pending")       # pending | sent | skipped
    sent_at = Column(DateTime, nullable=True)

    stage = relationship("PaymentStage", back_populates="reminders")