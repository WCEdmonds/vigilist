"""Email notification service via Resend."""

import logging
import re

import resend

from app.config import settings

logger = logging.getLogger(__name__)

MENTION_PATTERN = re.compile(r'@([\w.+-]+@[\w-]+\.[\w.-]+)')


def send_invite_email(
    to_email: str,
    inviter_name: str,
    production_name: str,
    role: str,
) -> bool:
    """Send an email inviting a user to a production."""
    if not settings.resend_api_key:
        logger.warning("Resend API key not configured, skipping invite email")
        return False

    resend.api_key = settings.resend_api_key

    signup_url = settings.app_url

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px;">
      <div style="border-bottom: 2px solid #2c3e6b; padding-bottom: 12px; margin-bottom: 20px;">
        <span style="font-size: 18px; font-weight: 700; color: #2c3e6b;">Vigilist</span>
      </div>
      <p style="color: #333; font-size: 14px; line-height: 1.6;">
        <strong>{inviter_name}</strong> has invited you to review documents in
        <strong>{production_name}</strong> as a <strong>{role}</strong>.
      </p>
      <p style="color: #555; font-size: 14px; line-height: 1.6;">
        Sign in with this email address (<strong>{to_email}</strong>) to get started.
      </p>
      <a href="{signup_url}" style="display: inline-block; background: #2c3e6b; color: #fff; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500; margin-top: 8px;">
        Open Vigilist
      </a>
      <p style="margin-top: 24px; font-size: 11px; color: #aaa;">
        You received this because you were invited to a document review on Vigilist.
        <a href="https://qndary.com" style="color: #999;">Built by QNDARY</a>
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": f"{inviter_name} invited you to review {production_name}",
            "html": html,
        })
        logger.info("Sent invite email to %s for production %s", to_email, production_name)
        return True
    except Exception as e:
        logger.warning("Failed to send invite email to %s: %s", to_email, e)
        return False


def send_access_granted_email(
    to_email: str,
    inviter_name: str,
    production_name: str,
    role: str,
) -> bool:
    """Send an email notifying a user they've been granted access."""
    if not settings.resend_api_key:
        logger.warning("Resend API key not configured, skipping access email")
        return False

    resend.api_key = settings.resend_api_key

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px;">
      <div style="border-bottom: 2px solid #2c3e6b; padding-bottom: 12px; margin-bottom: 20px;">
        <span style="font-size: 18px; font-weight: 700; color: #2c3e6b;">Vigilist</span>
      </div>
      <p style="color: #333; font-size: 14px; line-height: 1.6;">
        <strong>{inviter_name}</strong> has granted you <strong>{role}</strong> access to
        <strong>{production_name}</strong>.
      </p>
      <a href="{settings.app_url}" style="display: inline-block; background: #2c3e6b; color: #fff; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500; margin-top: 8px;">
        Open Vigilist
      </a>
      <p style="margin-top: 24px; font-size: 11px; color: #aaa;">
        You received this because you were granted access on Vigilist.
        <a href="https://qndary.com" style="color: #999;">Built by QNDARY</a>
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": [to_email],
            "subject": f"You now have access to {production_name}",
            "html": html,
        })
        logger.info("Sent access-granted email to %s for production %s", to_email, production_name)
        return True
    except Exception as e:
        logger.warning("Failed to send access email to %s: %s", to_email, e)
        return False


def extract_mentions(text: str) -> list[str]:
    """Extract @email mentions from note text."""
    return MENTION_PATTERN.findall(text)


def send_mention_notification(
    mentioned_email: str,
    author_name: str,
    note_content: str,
    doc_bates: str,
    doc_title: str | None,
    doc_id: str,
    production_name: str,
) -> bool:
    """Send an email notification when someone is @mentioned in a note."""
    if not settings.resend_api_key:
        logger.warning("Resend API key not configured, skipping email")
        return False

    resend.api_key = settings.resend_api_key

    doc_label = doc_title or doc_bates
    doc_url = f"{settings.app_url}?doc={doc_id}"

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px;">
      <div style="border-bottom: 2px solid #2c3e6b; padding-bottom: 12px; margin-bottom: 20px;">
        <span style="font-size: 18px; font-weight: 700; color: #2c3e6b;">Vigilist</span>
        <span style="color: #999; margin-left: 8px; font-size: 13px;">{production_name}</span>
      </div>
      <p style="color: #333; font-size: 14px; line-height: 1.6;">
        <strong>{author_name}</strong> mentioned you in a note on <strong>{doc_label}</strong>:
      </p>
      <div style="background: #f7f8fa; border-left: 3px solid #2c3e6b; padding: 12px 16px; margin: 16px 0; border-radius: 0 6px 6px 0; font-size: 14px; color: #444; line-height: 1.5;">
        {note_content}
      </div>
      <a href="{doc_url}" style="display: inline-block; background: #2c3e6b; color: #fff; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">
        View Document
      </a>
      <p style="margin-top: 24px; font-size: 11px; color: #aaa;">
        You received this because you were @mentioned in Vigilist.
        <a href="https://qndary.com" style="color: #999;">Built by QNDARY</a>
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": [mentioned_email],
            "subject": f"{author_name} mentioned you on {doc_label}",
            "html": html,
        })
        logger.info("Sent mention notification to %s for doc %s", mentioned_email, doc_bates)
        return True
    except Exception as e:
        logger.warning("Failed to send mention email to %s: %s", mentioned_email, e)
        return False
