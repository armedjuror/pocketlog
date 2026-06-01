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