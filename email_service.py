"""
email_service.py — Transactional email via Resend.
"""

import os
import logging

log = logging.getLogger(__name__)

_FROM = os.getenv("FROM_EMAIL", "PocketLog <noreply@pocketlog.app>")


def send_email_otp(to_email: str, name: str, otp: str) -> bool:
    """
    Send an email verification OTP via Resend.
    Returns True on success, False on failure.
    """
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — skipping email verification send")
        return False

    import resend
    resend.api_key = api_key

    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
      <h2 style="margin-bottom:8px">Verify your email</h2>
      <p style="color:#555">Hi {name}, use the code below to verify your email for PocketLog:</p>
      <div style="font-size:36px;font-weight:bold;letter-spacing:8px;
                  background:#f4f4f5;border-radius:8px;padding:20px 32px;
                  text-align:center;margin:24px 0">{otp}</div>
      <p style="color:#888;font-size:13px">This code expires in 10 minutes. If you didn't request this, ignore this email.</p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": _FROM,
            "to": [to_email],
            "subject": f"{otp} is your PocketLog verification code",
            "html": html,
        })
        return True
    except Exception:
        log.exception("Resend email failed to %s", to_email)
        return False


def send_group_invite_email(
    to_email: str,
    member_name: str,
    group_name: str,
    inviter_name: str,
    telegram_bot: str,
) -> bool:
    """Send a group invite email to a new member (registered or guest)."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — skipping group invite email")
        return False

    import resend
    resend.api_key = api_key

    bot_url = f"https://t.me/{telegram_bot.lstrip('@')}" if telegram_bot else "https://t.me/"

    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
      <h2 style="margin-bottom:8px">You've been added to a group</h2>
      <p style="color:#555">Hi {member_name},</p>
      <p style="color:#555">
        <strong>{inviter_name}</strong> added you to the group
        <strong>{group_name}</strong> on PocketLog.
      </p>
      <p style="color:#555;margin-top:16px">
        PocketLog is a personal finance tracker that works right inside Telegram —
        log expenses, split bills, and track balances without leaving your chat.
      </p>
      <a href="{bot_url}" style="display:inline-block;margin-top:24px;background:#2aabee;
         color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600">
        Open PocketLog on Telegram
      </a>
      <p style="color:#aaa;font-size:12px;margin-top:28px">
        You received this because {inviter_name} added your email to a PocketLog group.
        If this was a mistake, you can ignore this email.
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": _FROM,
            "to": [to_email],
            "subject": f"{inviter_name} added you to \"{group_name}\" on PocketLog",
            "html": html,
        })
        return True
    except Exception:
        log.exception("Resend group invite email failed to %s", to_email)
        return False