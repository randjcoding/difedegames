"""Outbound email for DiFede Games.

Deliverability notes (Gmail SMTP):
- From must match the authenticated account (EMAIL_SENDER). Using a different
  address in From is a common spam trigger for Yahoo/Gmail.
- Prefer https://games.difedes.com links (not LAN IPs).
- Avoid spammy subject prefixes like "(DO NOT REPLY)" / "[ACTION REQUIRED]".
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import textwrap
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Authenticated Gmail mailbox (must match app-password account)
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "randjcoding@gmail.com").strip()
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "DiFede Games").strip()
# Optional contact line in body only — do NOT put unauthenticated addresses in From
EMAIL_CONTACT = os.getenv("EMAIL_CONTACT", EMAIL_SENDER).strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVERS = ["joe_71@yahoo.com"]
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://games.difedes.com").rstrip("/")


def _normalize_body(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def _build_message(*, to: str | list[str], subject: str, body: str) -> EmailMessage:
    to_header = ", ".join(to) if isinstance(to, list) else to
    em = EmailMessage()
    em["From"] = formataddr((EMAIL_FROM_NAME, EMAIL_SENDER))
    em["Reply-To"] = EMAIL_SENDER
    em["To"] = to_header
    em["Subject"] = subject
    em["Message-ID"] = make_msgid(domain="gmail.com")
    em["X-Auto-Response-Suppress"] = "OOF, AutoReply"
    em.set_content(_normalize_body(body))
    return em


def _smtp_send(em: EmailMessage, recipients: list[str]) -> bool:
    if not EMAIL_PASSWORD:
        logger.error("EMAIL_PASSWORD is not set; cannot send mail")
        return False
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(em, from_addr=EMAIL_SENDER, to_addrs=recipients)
        return True
    except Exception as e:
        logger.error("SMTP send failed: %s", e, exc_info=True)
        return False


def send_email(to_email: str, subject: str, body: str) -> bool:
    em = _build_message(to=to_email, subject=subject, body=body)
    ok = _smtp_send(em, [to_email])
    if ok:
        logger.info("Email sent to %s: %s", to_email, subject)
    else:
        logger.error("Failed to send email to %s: %s", to_email, subject)
    return ok


def send_verification_email(user_email: str, first_name: str, verification_link: str) -> bool:
    subject = "Confirm your DiFede Games email"
    body = f"""
    Hi {first_name},

    Thanks for joining DiFede Games. Please confirm your email by opening this link:

    {verification_link}

    The link expires in 24 hours. If you did not create this account, you can ignore this message.

    — DiFede Games
    {APP_BASE_URL}
    """
    return send_email(user_email, subject, body)


def send_registration_notification(first_name: str, last_name: str, email: str, family_name: str) -> None:
    subject = f"New DiFede Games registration: {first_name} {last_name}"
    body = f"""
    A new user registered on DiFede Games.

    Name: {first_name} {last_name}
    Email: {email}
    Family: {family_name}

    They were sent a confirmation email and must verify before signing in.

    Admin users:
    {APP_BASE_URL}/auth/admin/users
    """
    em = _build_message(to=EMAIL_RECEIVERS, subject=subject, body=body)
    if _smtp_send(em, EMAIL_RECEIVERS):
        logger.info("Registration notification sent for %s %s (%s)", first_name, last_name, email)
    else:
        logger.error("Failed to send registration notification for %s", email)


def send_welcome_email(user_email: str, first_name: str) -> bool:
    subject = "Your DiFede Games email is confirmed"
    body = f"""
    Hi {first_name},

    Your email is confirmed. You can sign in here:

    {APP_BASE_URL}/auth/login

    — DiFede Games
    """
    return send_email(user_email, subject, body)


def send_verification_expired_email(user_email: str, first_name: str) -> bool:
    subject = "DiFede Games confirmation link expired"
    body = f"""
    Hi {first_name},

    Your email confirmation link expired (links are valid for 24 hours).

    Request a new confirmation email from the sign-in page, or contact {EMAIL_CONTACT}.

    — DiFede Games
    """
    return send_email(user_email, subject, body)


def send_approval_email(user_email: str, first_name: str) -> bool:
    subject = "Your DiFede Games account is approved"
    body = f"""
    Hi {first_name},

    An administrator approved your DiFede Games account. You can sign in here:

    {APP_BASE_URL}/auth/login

    — DiFede Games
    """
    return send_email(user_email, subject, body)


def send_approval_needed_notification(first_name: str, last_name: str, email: str, family_name: str) -> None:
    subject = f"DiFede Games user awaiting approval: {first_name} {last_name}"
    body = f"""
    A verified user is waiting for admin approval.

    Name: {first_name} {last_name}
    Email: {email}
    Family: {family_name}

    Review here:
    {APP_BASE_URL}/auth/admin/users
    """
    em = _build_message(to=EMAIL_RECEIVERS, subject=subject, body=body)
    if _smtp_send(em, EMAIL_RECEIVERS):
        logger.info("Approval-needed notification sent for %s %s (%s)", first_name, last_name, email)
    else:
        logger.error("Failed to send approval notification for %s", email)


def send_alliance_request_email(user_email: str, first_name: str, from_family: str, from_user: str) -> bool:
    subject = f"Crew-up request from {from_family}"
    body = f"""
    Hi {first_name},

    {from_user} from the {from_family} family sent a crew-up request on DiFede Games.

    Review it on your dashboard:
    {APP_BASE_URL}/dashboard

    — DiFede Games
    """
    return send_email(user_email, subject, body)


def send_alliance_accepted_email(user_email: str, first_name: str, accepted_family: str) -> bool:
    subject = f"{accepted_family} accepted your crew-up request"
    body = f"""
    Hi {first_name},

    The {accepted_family} family accepted your crew-up request.

    Open your dashboard:
    {APP_BASE_URL}/dashboard

    — DiFede Games
    """
    return send_email(user_email, subject, body)
