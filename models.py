import enum
import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Boolean,
    ForeignKey, Text, Enum, create_engine, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


# ── Auth / User models ─────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(100), nullable=False)
    email       = Column(String(255), unique=True, nullable=False, index=True)
    primary_bot = Column(String(50), default="telegram", nullable=False)
    created_at  = Column(DateTime, server_default=func.now())

    bot_identities = relationship("BotIdentity", back_populates="user")
    sessions       = relationship("UserSession",  back_populates="user")


class BotIdentity(Base):
    """Links a platform + chat_id to a User."""
    __tablename__ = "bot_identities"
    __table_args__ = (UniqueConstraint("platform", "chat_id", name="uq_bot_identity"),)

    id       = Column(Integer, primary_key=True, index=True)
    user_id  = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String(50),  nullable=False)   # "telegram", "whatsapp", etc.
    chat_id  = Column(String(200), nullable=False)

    user = relationship("User", back_populates="bot_identities")


class OTPSession(Base):
    """Short-lived OTP for bot login."""
    __tablename__ = "otp_sessions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    otp_code   = Column(String(10), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False, nullable=False)


class UserSession(Base):
    """Long-lived session token issued after OTP verification."""
    __tablename__ = "user_sessions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token      = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


class BotConversationState(Base):
    """Tracks multi-step auth conversation state per bot identity."""
    __tablename__ = "bot_conversation_states"
    __table_args__ = (UniqueConstraint("platform", "chat_id", name="uq_bot_conv_state"),)

    id         = Column(Integer, primary_key=True, index=True)
    platform   = Column(String(50),  nullable=False)
    chat_id    = Column(String(200), nullable=False)
    # states: awaiting_name | awaiting_email | awaiting_otp | awaiting_merge_otp
    state      = Column(String(50),  nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Scratch space for multi-step flows
    temp_name  = Column(String(100), nullable=True)   # collected name before email confirmed
    temp_email = Column(String(255), nullable=True)   # email being verified in merge flow
    updated_at = Column(DateTime, server_default=func.now())


class OAuthClient(Base):
    """Third-party app registered by a user to access the API via client credentials."""
    __tablename__ = "oauth_clients"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name               = Column(String(100), nullable=False)
    client_id          = Column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash = Column(String(64), nullable=False)   # sha256 hex of the secret
    scopes             = Column(String(200), default="read write")
    created_at         = Column(DateTime, server_default=func.now())

    user   = relationship("User")
    tokens = relationship("OAuthAccessToken", back_populates="client", cascade="all, delete-orphan")


class OAuthAccessToken(Base):
    """Short-lived access token issued to an OAuthClient via client_credentials grant."""
    __tablename__ = "oauth_access_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    client_id  = Column(Integer, ForeignKey("oauth_clients.id"), nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    scopes     = Column(String(200))
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    client = relationship("OAuthClient", back_populates="tokens")


# ── Enums ──────────────────────────────────────────────────────────────────

class AccountType(str, enum.Enum):
    bank         = "bank"
    credit_card  = "credit_card"
    cash         = "cash"
    metro_card   = "metro_card"
    wallet       = "wallet"
    loan         = "loan"
    chitty       = "chitty"
    other        = "other"


class TransactionType(str, enum.Enum):
    expense  = "expense"
    income   = "income"
    transfer = "transfer"


class LendingType(str, enum.Enum):
    lent     = "lent"       # I lent money to someone
    borrowed = "borrowed"   # I borrowed from someone


# ── Models ─────────────────────────────────────────────────────────────────

class Account(Base):
    __tablename__ = "accounts"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # None = system/global
    name         = Column(String(100), nullable=False)
    type         = Column(Enum(AccountType), nullable=False)
    balance      = Column(Float, default=0.0, nullable=False)
    currency     = Column(String(10), default="INR")
    color        = Column(String(20), default="#6366f1")
    is_active    = Column(Boolean, default=True, nullable=False)
    created_at   = Column(DateTime, server_default=func.now())
    # Loan / Chitty extras
    total_amount = Column(Float, nullable=True)
    monthly_emi  = Column(Float, nullable=True)
    due_date     = Column(Integer, nullable=True)   # day-of-month
    notes        = Column(Text, nullable=True)

    transactions = relationship(
        "Transaction", back_populates="account",
        foreign_keys="Transaction.account_id"
    )


class Category(Base):
    __tablename__ = "categories"

    id    = Column(Integer, primary_key=True, index=True)
    name  = Column(String(100), nullable=False, unique=True)
    icon  = Column(String(10), default="💰")
    color = Column(String(20), default="#6366f1")

    transactions = relationship("Transaction", back_populates="category")
    budgets      = relationship("Budget",      back_populates="category")


class Transaction(Base):
    __tablename__ = "transactions"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    amount      = Column(Float, nullable=False)
    type        = Column(Enum(TransactionType), default=TransactionType.expense, nullable=False)
    description = Column(String(255), nullable=False)
    note        = Column(Text, nullable=True)
    date        = Column(Date, nullable=False, index=True)
    created_at  = Column(DateTime, server_default=func.now())

    account_id    = Column(Integer, ForeignKey("accounts.id"),  nullable=False, index=True)
    category_id   = Column(Integer, ForeignKey("categories.id"), nullable=True,  index=True)
    to_account_id = Column(Integer, ForeignKey("accounts.id"),  nullable=True)

    # Plugin source tracking — which channel created this record
    source_plugin  = Column(String(50), nullable=True)   # e.g. "telegram", "whatsapp", "email", "web"
    source_ref     = Column(String(200), nullable=True)  # plugin-specific message id / email id etc.

    account  = relationship("Account",  back_populates="transactions", foreign_keys=[account_id])
    category = relationship("Category", back_populates="transactions")


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("user_id", "category_id", "month", "year", name="uq_budget_cat_month"),
    )

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    month       = Column(Integer, nullable=False)
    year        = Column(Integer, nullable=False)
    amount      = Column(Float,   nullable=False)

    category = relationship("Category", back_populates="budgets")


class Lending(Base):
    __tablename__ = "lendings"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    person_name     = Column(String(100), nullable=False)
    type            = Column(Enum(LendingType), nullable=False)
    amount          = Column(Float, nullable=False)
    amount_settled  = Column(Float, default=0.0, nullable=False)
    date            = Column(Date,  nullable=False, index=True)
    due_date        = Column(Date,  nullable=True)
    note            = Column(Text,  nullable=True)
    is_settled      = Column(Boolean, default=False, nullable=False)
    created_at      = Column(DateTime, server_default=func.now())


# ── Engine & init ──────────────────────────────────────────────────────────

def _make_engine():
    url = os.getenv("DATABASE_URL", "sqlite:///./expense.db")
    # SQLite needs check_same_thread; Postgres doesn't (and rejects that kwarg)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # psycopg2 URLs from Postgres env vars sometimes use postgres:// — fix it
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine()

from sqlalchemy.orm import sessionmaker
SessionLocal = sessionmaker(bind=engine)


def init_db():
    # Ensure all model classes are registered before create_all
    _ = (User, BotIdentity, OTPSession, UserSession, BotConversationState,
         OAuthClient, OAuthAccessToken,
         Account, Category, Transaction, Budget, Lending)
    Base.metadata.create_all(bind=engine)
    _seed(engine)


def _seed(eng):
    from sqlalchemy.orm import Session
    with Session(eng) as s:
        if s.query(Category).count() == 0:
            s.add_all([
                Category(name="Food & Dining",  icon="🍽️", color="#f59e0b"),
                Category(name="Groceries",       icon="🛒", color="#10b981"),
                Category(name="Transport",        icon="🚗", color="#3b82f6"),
                Category(name="Shopping",         icon="🛍️", color="#ec4899"),
                Category(name="Entertainment",    icon="🎬", color="#8b5cf6"),
                Category(name="Health",           icon="🏥", color="#ef4444"),
                Category(name="Utilities",        icon="💡", color="#f97316"),
                Category(name="Rent",             icon="🏠", color="#6366f1"),
                Category(name="Education",        icon="📚", color="#06b6d4"),
                Category(name="Travel",           icon="✈️", color="#14b8a6"),
                Category(name="Subscriptions",    icon="📱", color="#a855f7"),
                Category(name="Investment",       icon="📈", color="#22c55e"),
                Category(name="EMI",              icon="🏦", color="#64748b"),
                Category(name="Chitty",           icon="🤝", color="#d97706"),
                Category(name="Miscellaneous",    icon="📦", color="#94a3b8"),
            ])
            s.commit()
        if s.query(Account).count() == 0:
            s.add_all([
                Account(name="HDFC Savings",     type=AccountType.bank,        balance=0, color="#003f8a"),
                Account(name="HDFC Credit Card", type=AccountType.credit_card, balance=0, color="#e63946"),
                Account(name="Cash",             type=AccountType.cash,        balance=0, color="#2d6a4f"),
                Account(name="Metro Card",       type=AccountType.metro_card,  balance=0, color="#e76f51"),
                Account(name="Amazon Pay",       type=AccountType.wallet,      balance=0, color="#f4a261"),
            ])
            s.commit()
