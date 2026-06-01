"""
routers/api_v1.py — Public, OAuth-protected REST API (v1).

Every endpoint requires a Bearer token:
  Authorization: Bearer <token>

Tokens are obtained via:
  · POST /api/oauth/token  (client_credentials grant — for integrations)
  · /apikey command in the Telegram bot (personal access token — for quick scripting)

All data is scoped to the authenticated user.
"""

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

import oauth_service
import services
from models import AccountType, LendingType, SessionLocal, TransactionType, User

router = APIRouter(prefix="/api/v1")
_bearer = HTTPBearer(auto_error=False)


# ── DB + auth dependencies ─────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    user = oauth_service.get_user_from_token(creds.credentials, db)
    if not user:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return user


# ── Pydantic schemas ───────────────────────────────────────────────────────

class AccountIn(BaseModel):
    name:         str
    type:         AccountType
    balance:      float = 0.0
    currency:     str   = "INR"
    color:        str   = "#6366f1"
    total_amount: Optional[float] = None
    monthly_emi:  Optional[float] = None
    due_date:     Optional[int]   = None
    notes:        Optional[str]   = None


class CategoryIn(BaseModel):
    name:  str
    icon:  str = "💰"
    color: str = "#6366f1"


class TransactionIn(BaseModel):
    amount:        float
    type:          TransactionType = TransactionType.expense
    description:   str
    note:          Optional[str]  = None
    date:          date
    account_id:    int
    category_id:   Optional[int] = None
    to_account_id: Optional[int] = None


class BudgetIn(BaseModel):
    category_id: int
    month:       int
    year:        int
    amount:      float


class LendingIn(BaseModel):
    person_name:    str
    type:           LendingType
    amount:         float
    amount_settled: float = 0.0
    date:           date
    due_date:       Optional[date] = None
    note:           Optional[str]  = None
    is_settled:     bool           = False


# ── Me ─────────────────────────────────────────────────────────────────────

@router.get("/me", summary="Get authenticated user profile")
def me(user: User = Depends(current_user)):
    """Return the profile of the currently authenticated user."""
    return {
        "id":          user.id,
        "name":        user.name,
        "email":       user.email,
        "primary_bot": user.primary_bot,
        "created_at":  str(user.created_at),
    }


# ── Accounts ───────────────────────────────────────────────────────────────

@router.get("/accounts", summary="List accounts")
def list_accounts(
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Return all accounts visible to the authenticated user (system + personal)."""
    return services.get_accounts(db, user_id=user.id)


@router.post("/accounts", status_code=201, summary="Create an account")
def create_account(
    data: AccountIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Create a new personal account owned by the authenticated user."""
    return services.create_account(db, user_id=user.id, **data.model_dump())


@router.put("/accounts/{aid}", summary="Update an account")
def update_account(
    aid:  int,
    data: AccountIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    try:
        return services.update_account(db, aid, **data.model_dump())
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/accounts/{aid}", summary="Deactivate an account")
def delete_account(
    aid:  int,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    try:
        services.delete_account(db, aid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Categories ─────────────────────────────────────────────────────────────

@router.get("/categories", summary="List categories")
def list_categories(
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Return all global expense categories."""
    return services.get_categories(db)


@router.post("/categories", status_code=201, summary="Create a category")
def create_category(
    data: CategoryIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    return services.create_category(db, **data.model_dump())


# ── Transactions ───────────────────────────────────────────────────────────

@router.get("/transactions", summary="List transactions")
def list_transactions(
    month:       Optional[int] = None,
    year:        Optional[int] = None,
    account_id:  Optional[int] = None,
    category_id: Optional[int] = None,
    limit:       int           = Query(100, le=500),
    offset:      int           = 0,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    List transactions for the authenticated user with optional filters.

    - **month** + **year**: filter to a specific month
    - **account_id**: filter by account
    - **category_id**: filter by category
    - **limit** / **offset**: pagination (max 500 per request)
    """
    return services.list_transactions(
        db, month, year, account_id, category_id, limit, offset, user_id=user.id
    )


@router.post("/transactions", status_code=201, summary="Log a transaction")
def create_transaction(
    data: TransactionIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Record a new expense, income, or transfer."""
    return services.create_transaction(
        db,
        amount        = data.amount,
        description   = data.description,
        date          = data.date,
        account_id    = data.account_id,
        type          = data.type.value,
        category_id   = data.category_id,
        to_account_id = data.to_account_id,
        note          = data.note,
        source_plugin = "api",
        user_id       = user.id,
    )


@router.put("/transactions/{tid}", summary="Update a transaction")
def update_transaction(
    tid:  int,
    data: TransactionIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Update an existing transaction. Account balances are recalculated automatically."""
    try:
        return services.update_transaction(
            db, tid,
            amount        = data.amount,
            description   = data.description,
            date          = data.date,
            account_id    = data.account_id,
            type          = data.type.value,
            category_id   = data.category_id,
            to_account_id = data.to_account_id,
            note          = data.note,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/transactions/{tid}", summary="Delete a transaction")
def delete_transaction(
    tid:  int,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    try:
        services.delete_transaction(db, tid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Budgets ────────────────────────────────────────────────────────────────

@router.get("/budgets", summary="List budgets for a month")
def list_budgets(
    month: int,
    year:  int,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Return all budgets and their current spend status for the given month."""
    return services.list_budgets(db, month, year, user_id=user.id)


@router.post("/budgets", status_code=201, summary="Create or update a budget")
def upsert_budget(
    data: BudgetIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Set a monthly budget for a category. Updates the existing one if it exists."""
    return services.upsert_budget(
        db, data.category_id, data.month, data.year, data.amount, user_id=user.id
    )


# ── Lending ────────────────────────────────────────────────────────────────

@router.get("/lending", summary="List lending records")
def list_lending(
    settled: Optional[bool] = None,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    List money lent or borrowed.

    - **settled=true** — only settled records
    - **settled=false** — only outstanding records
    - omit to return all
    """
    return services.list_lending(db, settled, user_id=user.id)


@router.post("/lending", status_code=201, summary="Add a lending record")
def create_lending(
    data: LendingIn,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    return services.create_lending(db, user_id=user.id, **data.model_dump())


@router.put("/lending/{lid}/settle", summary="Settle (part of) a lending record")
def settle_lending(
    lid:    int,
    amount: float = Query(..., description="Amount being settled"),
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    try:
        return services.settle_lending(db, lid, amount)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/lending/{lid}", summary="Delete a lending record")
def delete_lending(
    lid:  int,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    try:
        services.delete_lending(db, lid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Analytics ──────────────────────────────────────────────────────────────

@router.get("/analytics/summary", summary="Monthly income / expense summary")
def monthly_summary(
    month: int,
    year:  int,
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    Aggregate income, expenses, net, net worth, spending by category,
    and daily spending totals for the given month.
    """
    return services.monthly_summary(db, month, year, user_id=user.id)


@router.get("/analytics/trend", summary="Spending trend (last N months)")
def spending_trend(
    months: int   = Query(6, ge=1, le=24),
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """Return monthly income and expense totals for the last N months (max 24)."""
    return services.spending_trend(db, months, user_id=user.id)


# ── Export ─────────────────────────────────────────────────────────────────

@router.get("/export/csv", summary="Export all data as CSV")
def export_csv(
    user: User    = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    Download all your data (transactions, accounts, budgets, lending)
    as a multi-section CSV file.
    """
    content  = services.export_csv(db, user_id=user.id)
    filename = f"hisaab_export_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
