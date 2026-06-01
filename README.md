# Paisa — Personal Expense Manager

A lightweight personal finance tracker: FastAPI + SQLite + vanilla HTML/JS.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (for Telegram bot)
export ANTHROPIC_API_KEY=sk-ant-...
export TELEGRAM_BOT_TOKEN=your-bot-token
export TELEGRAM_ALLOWED_CHAT_ID=your-telegram-user-id   # get via @userinfobot

# 3. Run
uvicorn main:app --reload --port 8000

# 4. Open browser
open http://localhost:8000
```

## Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/botfather) → get token
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot)
3. Set the webhook (replace with your public URL or use ngrok):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://yourdomain.com/api/telegram/webhook"
```

**For local dev with ngrok:**
```bash
ngrok http 8000
# then set webhook to: https://xxxx.ngrok.io/api/telegram/webhook
```

## Telegram Usage

Send messages like:
- `Spent 250 on lunch at MTR, HDFC card`
- `Zomato 450 from Amazon Pay`
- `Received 50000 salary in HDFC savings`
- `Metro card recharge 200`
- `Transfer 5000 from HDFC to cash`

The bot will parse with Claude Haiku and:
- Log the transaction automatically
- Warn if you're over daily budget pace
- Ask for missing info if it can't infer account/category

You can also forward invoices or receipts with a caption.

## Features

- **Accounts**: Bank, Credit Card, Cash, Metro Card, Wallet, Loan, Chitty
- **Transactions**: Expense / Income / Transfer with category tagging
- **Budgets**: Monthly budget per category with daily pace tracking
- **Lending**: Track money lent/borrowed from people, partial settlements
- **Dashboard**: Daily spending chart, category donut, budget pace bars
- **Analytics**: 6-month trend, account balances, category breakdown
- **Telegram Bot**: Natural language expense logging via LiteLLM → Claude Haiku

## Project Structure

```
expense-manager/
├── main.py          # FastAPI app + all routes + Telegram webhook
├── models.py        # SQLAlchemy models (SQLite)
├── requirements.txt
├── expense.db       # auto-created on first run
└── static/
    └── index.html   # Full dashboard (responsive, AJAX)
```
