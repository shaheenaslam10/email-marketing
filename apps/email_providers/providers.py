import smtplib
import uuid
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Dict, Tuple, List
import requests
from django.conf import settings
from .base import BaseEmailProvider, SendResult
from apps.sandbox.models import SandboxEmail

logger = logging.getLogger(__name__)


class SandboxProvider(BaseEmailProvider):
    """Captures outgoing emails directly to the sandbox database for instant in-app webmail inspection."""

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        try:
            msg_id = f"sandbox-{uuid.uuid4()}"
            SandboxEmail.objects.create(
                to_email=to_email,
                from_name=self.sender.name,
                from_email=self.sender.email,
                reply_to=reply_to or self.sender.reply_to or "",
                subject=subject,
                html_content=html_content,
                text_content=text_content or "",
                headers=headers or {},
                tags=tags or [],
                provider_type='SANDBOX'
            )
            return SendResult(success=True, provider_message_id=msg_id, status_code=200)
        except Exception as e:
            logger.error("SandboxProvider failed to record email: %s", e)
            return SendResult(success=False, error_message=str(e), status_code=500)

    def test_connection(self) -> Tuple[bool, str]:
        return True, "Sandbox email provider is active and ready."


class SMTPProvider(BaseEmailProvider):
    """Standard SMTP email provider supporting TLS and SSL."""

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender.display_from
            msg['To'] = to_email
            if reply_to:
                msg['Reply-To'] = reply_to
            elif self.sender.reply_to:
                msg['Reply-To'] = self.sender.reply_to

            from email.utils import formatdate, make_msgid
            msg['Date'] = formatdate(localtime=True)
            domain = self.sender.email.split('@')[-1] if '@' in (self.sender.email or '') else 'platform.local'
            msg['Message-ID'] = make_msgid(domain=domain)

            if headers:
                for k, v in headers.items():
                    msg[k] = v

            if text_content:
                msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
            if html_content:
                msg.attach(MIMEText(html_content, 'html', 'utf-8'))

            if self.sender.use_ssl:
                server = smtplib.SMTP_SSL(self.sender.host, self.sender.port, timeout=20)
            else:
                server = smtplib.SMTP(self.sender.host, self.sender.port, timeout=20)
                if self.sender.use_tls:
                    server.starttls()

            if self.sender.username and self.sender.password_or_key:
                server.login(self.sender.username, self.sender.password_or_key)

            server.sendmail(self.sender.email, [to_email], msg.as_string())
            server.quit()
            provider_msg_id = f"smtp-{uuid.uuid4()}"
            return SendResult(success=True, provider_message_id=provider_msg_id, status_code=250)
        except smtplib.SMTPResponseException as e:
            is_transient = e.smtp_code in [421, 450, 451, 452]
            return SendResult(
                success=False,
                error_message=f"SMTP Error {e.smtp_code}: {e.smtp_error.decode('utf-8', errors='ignore')}",
                status_code=e.smtp_code,
                is_transient_error=is_transient
            )
        except Exception as e:
            return SendResult(success=False, error_message=str(e), is_transient_error=True)

    def test_connection(self) -> Tuple[bool, str]:
        try:
            if self.sender.use_ssl:
                server = smtplib.SMTP_SSL(self.sender.host, self.sender.port, timeout=10)
            else:
                server = smtplib.SMTP(self.sender.host, self.sender.port, timeout=10)
                if self.sender.use_tls:
                    server.starttls()
            if self.sender.username and self.sender.password_or_key:
                server.login(self.sender.username, self.sender.password_or_key)
            server.quit()
            return True, "SMTP connection and authentication successful."
        except Exception as e:
            return False, f"SMTP Connection failed: {str(e)}"


class BrevoProvider(BaseEmailProvider):
    """Brevo (Sendinblue) implementation supporting both API v3 (REST) and SMTP relay."""

    API_URL = "https://api.brevo.com/v3/smtp/email"

    def _is_smtp_config(self) -> bool:
        key = (self.sender.password_or_key or "").strip()
        host = (self.sender.host or "").strip().lower()
        username = (self.sender.username or "").strip().lower()
        return key.startswith("xsmtps-") or "smtp-relay.brevo.com" in host or "@smtp-brevo.com" in username

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        if self._is_smtp_config():
            if not self.sender.host:
                self.sender.host = "smtp-relay.brevo.com"
            if not self.sender.port:
                self.sender.port = 587
            self.sender.use_tls = True
            return SMTPProvider(self.sender).send_email(to_email, subject, html_content, text_content, reply_to, headers, tags)

        # NOTE: Brevo offers no per-message, API, SMTP-header, or per-link
        # opt-out from its transactional link rewriting. Whatever branded
        # URLs we put in htmlContent are wrapped into sendibt*.com links by
        # Brevo after receipt. Stopping that rewrite is only possible at the
        # Brevo account level (support ticket -> compliance review); it
        # cannot be done from application code. Our own /c/ tracking still
        # records the click (with recipient/campaign attribution) when the
        # recipient follows Brevo's redirect through to us.
        payload = {
            "sender": {"name": self.sender.name, "email": self.sender.email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_content,
        }
        if text_content:
            payload["textContent"] = text_content
        if reply_to or self.sender.reply_to:
            payload["replyTo"] = {"email": reply_to or self.sender.reply_to}
        if headers:
            payload["headers"] = headers
        if tags:
            payload["tags"] = tags

        try:
            resp = requests.post(
                self.API_URL,
                json=payload,
                headers={"api-key": self.sender.password_or_key, "Content-Type": "application/json"},
                timeout=15
            )
            if resp.status_code in [200, 201]:
                data = resp.json()
                return SendResult(success=True, provider_message_id=data.get("messageId"), status_code=resp.status_code)
            is_transient = resp.status_code in [429, 500, 502, 503]
            return SendResult(
                success=False,
                error_message=f"Brevo API error: {resp.text}",
                status_code=resp.status_code,
                is_transient_error=is_transient
            )
        except Exception as e:
            return SendResult(success=False, error_message=str(e), is_transient_error=True)

    def test_connection(self) -> Tuple[bool, str]:
        if self._is_smtp_config():
            if not self.sender.host:
                self.sender.host = "smtp-relay.brevo.com"
            if not self.sender.port:
                self.sender.port = 587
            self.sender.use_tls = True
            return SMTPProvider(self.sender).test_connection()
        try:
            resp = requests.get(
                "https://api.brevo.com/v3/account",
                headers={"api-key": self.sender.password_or_key},
                timeout=10
            )
            if resp.status_code == 200:
                return True, "Brevo API connection authenticated successfully."
            return False, f"Brevo API error {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class MailgunProvider(BaseEmailProvider):
    """Mailgun API v3 implementation."""

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        domain = self.sender.api_domain or self.sender.email.split('@')[-1]
        url = f"https://api.mailgun.net/v3/{domain}/messages"
        data = {
            "from": self.sender.display_from,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
        if text_content:
            data["text"] = text_content
        if reply_to or self.sender.reply_to:
            data["h:Reply-To"] = reply_to or self.sender.reply_to
        if headers:
            for k, v in headers.items():
                data[f"h:{k}"] = v
        if tags:
            data["o:tag"] = tags
        if getattr(self.sender, 'disable_provider_click_tracking', False):
            # Per-message opt-out: Mailgun leaves our links untouched while
            # our own tracking stays fully active.
            data["o:tracking-clicks"] = "no"

        try:
            resp = requests.post(url, auth=("api", self.sender.password_or_key), data=data, timeout=15)
            if resp.status_code == 200:
                res_data = resp.json()
                return SendResult(success=True, provider_message_id=res_data.get("id"), status_code=200)
            is_transient = resp.status_code in [429, 500, 502, 503]
            return SendResult(
                success=False,
                error_message=f"Mailgun error: {resp.text}",
                status_code=resp.status_code,
                is_transient_error=is_transient
            )
        except Exception as e:
            return SendResult(success=False, error_message=str(e), is_transient_error=True)

    def test_connection(self) -> Tuple[bool, str]:
        domain = self.sender.api_domain or self.sender.email.split('@')[-1]
        try:
            resp = requests.get(
                f"https://api.mailgun.net/v3/domains/{domain}",
                auth=("api", self.sender.password_or_key),
                timeout=10
            )
            if resp.status_code == 200:
                return True, "Mailgun domain and API key validated successfully."
            return False, f"Mailgun error {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class SendGridProvider(BaseEmailProvider):
    """SendGrid API v3 implementation."""

    API_URL = "https://api.sendgrid.com/v3/mail/send"

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": self.sender.email, "name": self.sender.name},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_content}],
        }
        if text_content:
            payload["content"].insert(0, {"type": "text/plain", "value": text_content})
        if reply_to or self.sender.reply_to:
            payload["reply_to"] = {"email": reply_to or self.sender.reply_to}
        if headers:
            payload["headers"] = headers
        if tags:
            payload["categories"] = tags
        if getattr(self.sender, 'disable_provider_click_tracking', False):
            # Per-message opt-out: SendGrid leaves our links untouched while
            # our own tracking stays fully active.
            payload["tracking_settings"] = {
                "click_tracking": {"enable": False, "enable_text": False}
            }

        try:
            resp = requests.post(
                self.API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.sender.password_or_key}", "Content-Type": "application/json"},
                timeout=15
            )
            if resp.status_code in [200, 202]:
                msg_id = resp.headers.get("X-Message-Id", f"sendgrid-{uuid.uuid4()}")
                return SendResult(success=True, provider_message_id=msg_id, status_code=resp.status_code)
            is_transient = resp.status_code in [429, 500, 502, 503]
            return SendResult(
                success=False,
                error_message=f"SendGrid error: {resp.text}",
                status_code=resp.status_code,
                is_transient_error=is_transient
            )
        except Exception as e:
            return SendResult(success=False, error_message=str(e), is_transient_error=True)

    def test_connection(self) -> Tuple[bool, str]:
        try:
            resp = requests.get(
                "https://api.sendgrid.com/v3/user/profile",
                headers={"Authorization": f"Bearer {self.sender.password_or_key}"},
                timeout=10
            )
            if resp.status_code == 200:
                return True, "SendGrid API key verified successfully."
            return False, f"SendGrid error {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class PostmarkProvider(BaseEmailProvider):
    """Postmark API implementation."""

    API_URL = "https://api.postmarkapp.com/email"

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        payload = {
            "From": self.sender.display_from,
            "To": to_email,
            "Subject": subject,
            "HtmlBody": html_content,
        }
        if text_content:
            payload["TextBody"] = text_content
        if reply_to or self.sender.reply_to:
            payload["ReplyTo"] = reply_to or self.sender.reply_to
        if headers:
            payload["Headers"] = [{"Name": k, "Value": v} for k, v in headers.items()]
        if tags and len(tags) > 0:
            payload["Tag"] = tags[0]
        if getattr(self.sender, 'disable_provider_click_tracking', False):
            # Explicit opt-out (Postmark never rewrites links unless link
            # tracking was enabled at the server level).
            payload["TrackLinks"] = "None"

        try:
            resp = requests.post(
                self.API_URL,
                json=payload,
                headers={"X-Postmark-Server-Token": self.sender.password_or_key, "Content-Type": "application/json"},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                return SendResult(success=True, provider_message_id=data.get("MessageID"), status_code=200)
            is_transient = resp.status_code in [429, 500, 502, 503]
            return SendResult(
                success=False,
                error_message=f"Postmark error: {resp.text}",
                status_code=resp.status_code,
                is_transient_error=is_transient
            )
        except Exception as e:
            return SendResult(success=False, error_message=str(e), is_transient_error=True)

    def test_connection(self) -> Tuple[bool, str]:
        try:
            resp = requests.get(
                "https://api.postmarkapp.com/server",
                headers={"X-Postmark-Server-Token": self.sender.password_or_key},
                timeout=10
            )
            if resp.status_code == 200:
                return True, "Postmark Server Token validated successfully."
            return False, f"Postmark error {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"


class AmazonSESProvider(BaseEmailProvider):
    """Amazon SES REST API fallback / connection tester."""

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
    ) -> SendResult:
        # If host is provided, route via SMTP endpoint for Amazon SES
        if self.sender.host:
            smtp_prov = SMTPProvider(self.sender)
            return smtp_prov.send_email(to_email, subject, html_content, text_content, reply_to, headers, tags)
        # Sandbox fallback for simulated SES
        return SandboxProvider(self.sender).send_email(to_email, subject, html_content, text_content, reply_to, headers, tags)

    def test_connection(self) -> Tuple[bool, str]:
        if self.sender.host:
            return SMTPProvider(self.sender).test_connection()
        return True, "Amazon SES provider configured."


def get_email_provider(sender) -> BaseEmailProvider:
    """Factory function returning the corresponding concrete BaseEmailProvider."""
    if not sender or sender.provider_type == sender.ProviderType.SANDBOX:
        return SandboxProvider(sender)

    mapping = {
        sender.ProviderType.SMTP: SMTPProvider,
        sender.ProviderType.BREVO: BrevoProvider,
        sender.ProviderType.MAILGUN: MailgunProvider,
        sender.ProviderType.SENDGRID: SendGridProvider,
        sender.ProviderType.POSTMARK: PostmarkProvider,
        sender.ProviderType.AMAZON_SES: AmazonSESProvider,
    }
    cls = mapping.get(sender.provider_type, SandboxProvider)
    return cls(sender)
