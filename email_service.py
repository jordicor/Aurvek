import os
import requests
import logging
from typing import Dict
from html import escape

from i18n import Translator
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Default branding values
DEFAULT_BRANDING = {
    'company_name': 'Aurvek',
    'logo_url': None,
    'brand_color_primary': '#6366f1',
    'brand_color_secondary': '#10B981',
    'footer_text': None,
    'email_signature': None,
    'hide_platform_branding': False
}


class EmailService:
    def __init__(self):
        use_email_service = os.getenv('USE_EMAIL_SERVICE')
        if use_email_service is None:
            raise RuntimeError(
                "USE_EMAIL_SERVICE must be explicitly set to 'true' or 'false'"
            )
        normalized = use_email_service.strip().lower()
        if normalized not in {'true', 'false'}:
            raise RuntimeError(
                "USE_EMAIL_SERVICE must be explicitly set to 'true' or 'false'"
            )
        self.use_email_service = normalized == 'true'
        self.postmark_token = os.getenv('POSTMARK_SERVER_TOKEN')
        self.from_email = os.getenv('FROM_EMAIL', 'noreply@yourapp.com')

    def send_magic_link_email(self, to_email: str, magic_link: str, username: str, branding: Dict = None, ui_language: str = "en") -> bool:
        """
        Send a magic link via Postmark, or fail closed when delivery is disabled.

        Args:
            to_email: Recipient email address
            magic_link: The magic link URL
            username: Username for personalization
            branding: Optional branding dict from user settings

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.use_email_service:
            # Fail closed without writing the link to logs.
            logger.info(f"Email service disabled. Magic link generated for user '{username}' ({to_email})")
            return False

        if not self.postmark_token:
            logger.error("POSTMARK_SERVER_TOKEN not configured")
            return False

        return self._send_via_postmark(to_email, magic_link, username, branding, ui_language)

    def send_ultra_admin_code(self, to_email: str, code: str, username: str, ui_language: str = "en") -> bool:
        """Send an Ultra Admin+ elevation verification code via Postmark."""
        if not self.use_email_service:
            logger.info(
                "[ULTRA ADMIN+] Email service disabled; elevation code generated for '%s' (%s)",
                username,
                to_email,
            )
            return False

        if not self.postmark_token:
            logger.error("POSTMARK_SERVER_TOKEN not configured")
            return False

        try:
            translator = Translator(ui_language)
            t = translator.render
            subject = t("email.admin.subject")
            html_body = f"""
            <div lang="{translator.language}" style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #333;">{escape(t("email.admin.title"))}</h2>
                <p>{escape(t("email.admin.description"))}</p>
                <div style="background: #f5f5f5; border: 2px solid #e0e0e0; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                    <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #333;">{escape(code)}</span>
                </div>
                <p style="color: #666; font-size: 14px;">{escape(t("email.admin.expiry"))}</p>
                <p style="color: #999; font-size: 12px;">{escape(t("email.admin.warning"))}</p>
            </div>
            """
            text_body = t("email.admin.text", code=code)

            response = requests.post(
                'https://api.postmarkapp.com/email',
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-Postmark-Server-Token': self.postmark_token
                },
                json={
                    'From': self.from_email,
                    'To': to_email,
                    'Subject': subject,
                    'HtmlBody': html_body,
                    'TextBody': text_body,
                    'MessageStream': 'outbound'
                },
                timeout=10,
            )

            if response.status_code == 200:
                logger.info(f"[ULTRA ADMIN+] Elevation code email sent to {to_email}")
                return True
            else:
                logger.error(f"[ULTRA ADMIN+] Failed to send email: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"[ULTRA ADMIN+] Error sending elevation code email: {e}")
            return False

    def send_verification_email(self, to_email: str, verification_url: str, is_user: bool = False,
                                 prompt_name: str = None, branding: Dict = None, ui_language: str = "en") -> bool:
        """
        Send email verification link for new user registration.

        Args:
            to_email: Recipient email address
            verification_url: The verification URL
            is_user: True if registering as user (creator), False for customer
            prompt_name: Name of the prompt (only for customer registration from landing)
            branding: Optional branding dict from user settings

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.use_email_service:
            # Fail closed without writing the verification URL to logs.
            logger.info(
                "[VERIFICATION EMAIL] Email service disabled; %s verification generated for %s",
                'user' if is_user else 'customer',
                to_email,
            )
            if prompt_name:
                logger.info(f"[VERIFICATION EMAIL] Prompt: {prompt_name}")
            return False

        if not self.postmark_token:
            logger.error("POSTMARK_SERVER_TOKEN not configured")
            return False

        return self._send_verification_via_postmark(to_email, verification_url, is_user, prompt_name, branding, ui_language)

    def send_claim_entitlement_email(self, to_email: str, claim_url: str,
                                      product_name: str = None, branding: Dict = None, ui_language: str = "en") -> bool:
        """
        Send entitlement claim email to an existing user who tried to register from a landing page.

        Args:
            to_email: Recipient email address
            claim_url: Secure URL to claim the entitlement
            product_name: Name of the prompt or pack being claimed
            branding: Optional branding dict from user settings

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.use_email_service:
            logger.info(
                "[CLAIM EMAIL] Email service disabled; entitlement claim generated for %s",
                to_email,
            )
            if product_name:
                logger.info(f"[CLAIM EMAIL] Product: {product_name}")
            return False

        if not self.postmark_token:
            logger.error("POSTMARK_SERVER_TOKEN not configured")
            return False

        return self._send_claim_entitlement_via_postmark(to_email, claim_url, product_name, branding, ui_language)

    def _get_branding(self, branding: Dict = None) -> Dict:
        """Merge provided branding with defaults."""
        if branding is None:
            return DEFAULT_BRANDING.copy()
        result = DEFAULT_BRANDING.copy()
        result.update({k: v for k, v in branding.items() if v is not None})
        return result

    def _send_verification_via_postmark(self, to_email: str, verification_url: str, is_user: bool,
                                         prompt_name: str = None, branding: Dict = None, ui_language: str = "en") -> bool:
        """Send verification email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        t = Translator(ui_language).render
        b = self._get_branding(branding)
        html_body = self._create_verification_email_template(verification_url, is_user, prompt_name, b, ui_language)

        # Use branding company name for subject
        company_name = b.get('company_name') or 'Aurvek'
        if is_user:
            subject = t("email.verification.subject_creator", company_name=company_name)
        else:
            display_name = prompt_name or company_name
            subject = t("email.verification.subject_customer", display_name=display_name)

        data = {
            "From": self.from_email,
            "To": to_email,
            "Subject": subject,
            "HtmlBody": html_body,
            "MessageStream": "outbound"
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(f"Verification email sent successfully to {to_email}")
                return True
            else:
                logger.error(f"Failed to send verification email: {response.status_code} - {response.text}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending verification email via Postmark: {e}")
            return False

    def _create_verification_email_template(self, verification_url: str, is_user: bool,
                                             prompt_name: str = None, branding: Dict = None, ui_language: str = "en") -> str:
        """Create HTML email template for verification with branding support"""
        translator = Translator(ui_language)

        def t(key, **params):
            return escape(translator.render(key, **params))

        b = branding or DEFAULT_BRANDING

        company_name = b.get('company_name') or 'Aurvek'
        primary_color = b.get('brand_color_primary') or '#6366f1'
        logo_url = b.get('logo_url')
        footer_text = b.get('footer_text') or ''
        email_signature = b.get('email_signature') or ''
        hide_platform_branding = b.get('hide_platform_branding', False)

        if is_user:
            title = t("email.verification.title", display_name=company_name)
            intro = t("email.verification.intro_creator", company_name=company_name)
            description = t("email.verification.description_creator")
        else:
            display_name = prompt_name or company_name
            title = t("email.verification.title", display_name=display_name)
            intro = (t("email.verification.intro_customer", product_name=prompt_name) if prompt_name
                     else t("email.verification.intro_customer_generic"))
            description = t("email.verification.description_customer")

        # Logo HTML
        logo_html = ''
        if logo_url:
            logo_html = f'''
                <div style="margin-bottom: 15px;">
                    <img src="{escape(logo_url)}" alt="{escape(company_name)}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        # Footer HTML
        footer_content = f"<p>{t('email.footer.experiences', company_name=company_name)}</p>"
        if footer_text:
            footer_content = f"<p>{escape(footer_text)}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{escape(email_signature)}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = f'<p style="font-size: 10px; color: #999; margin-top: 15px;">{t("email.footer.powered_by")}</p>'

        return f"""
        <!DOCTYPE html>
        <html lang="{translator.language}">
        <head>
            <meta charset="utf-8">
            <title>{title}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    text-align: center;
                    padding: 20px 0;
                    border-bottom: 2px solid {escape(primary_color)};
                }}
                .header h1 {{
                    color: {escape(primary_color)};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 14px 32px;
                    background-color: {escape(primary_color)};
                    color: white !important;
                    text-decoration: none;
                    border-radius: 8px;
                    font-weight: bold;
                    margin: 20px 0;
                }}
                .footer {{
                    text-align: center;
                    padding-top: 20px;
                    border-top: 1px solid #ddd;
                    color: #666;
                    font-size: 12px;
                }}
                .warning {{
                    background-color: #fef3c7;
                    border: 1px solid #f59e0b;
                    border-radius: 4px;
                    padding: 12px;
                    margin-top: 20px;
                    font-size: 14px;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                {logo_html}
                <h1>{title}</h1>
            </div>
            <div class="content">
                <p>{intro}</p>
                <p>{description}</p>
                <div style="text-align: center;">
                    <a href="{escape(verification_url)}" class="button">{t("email.verification.button")}</a>
                </div>
                <div class="warning">
                    {t("email.verification.expiry")}
                </div>
                <p>{t("email.verification.ignore")}</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>{t("email.footer.automated")}</p>
                {powered_by}
            </div>
        </body>
        </html>
        """

    def _send_via_postmark(self, to_email: str, magic_link: str, username: str, branding: Dict = None, ui_language: str = "en") -> bool:
        """Send email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        t = Translator(ui_language).render
        b = self._get_branding(branding)
        html_body = self._create_email_template(magic_link, username, b, ui_language)
        company_name = b.get('company_name') or 'Aurvek'

        data = {
            "From": self.from_email,
            "To": to_email,
            "Subject": t("email.magic.subject", company_name=company_name),
            "HtmlBody": html_body,
            "MessageStream": "outbound"
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(f"Magic link email sent successfully to {to_email}")
                return True
            else:
                logger.error(f"Failed to send email: {response.status_code} - {response.text}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending email via Postmark: {e}")
            return False

    def _create_email_template(self, magic_link: str, username: str, branding: Dict = None, ui_language: str = "en") -> str:
        """Create HTML email template with branding support"""
        translator = Translator(ui_language)

        def t(key, **params):
            return escape(translator.render(key, **params))

        b = branding or DEFAULT_BRANDING

        company_name = b.get('company_name') or 'Aurvek'
        primary_color = b.get('brand_color_primary') or '#6366f1'
        logo_url = b.get('logo_url')
        footer_text = b.get('footer_text') or ''
        email_signature = b.get('email_signature') or ''
        hide_platform_branding = b.get('hide_platform_branding', False)

        # Logo HTML
        logo_html = ''
        if logo_url:
            logo_html = f'''
                <div style="margin-bottom: 15px;">
                    <img src="{escape(logo_url)}" alt="{escape(company_name)}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        # Footer HTML
        footer_content = ''
        if footer_text:
            footer_content = f"<p>{escape(footer_text)}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{escape(email_signature)}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = f'<p style="font-size: 10px; color: #999; margin-top: 15px;">{t("email.footer.powered_by")}</p>'

        return f"""
        <!DOCTYPE html>
        <html lang="{translator.language}">
        <head>
            <meta charset="utf-8">
            <title>{t("email.magic.subject", company_name=company_name)}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    text-align: center;
                    padding: 20px 0;
                    border-bottom: 2px solid {escape(primary_color)};
                }}
                .header h1 {{
                    color: {escape(primary_color)};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 12px 30px;
                    background-color: {escape(primary_color)};
                    color: white !important;
                    text-decoration: none;
                    border-radius: 5px;
                    font-weight: bold;
                    margin: 20px 0;
                }}
                .footer {{
                    text-align: center;
                    padding-top: 20px;
                    border-top: 1px solid #ddd;
                    color: #666;
                    font-size: 12px;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                {logo_html}
                <h1>{escape(company_name)}</h1>
            </div>
            <div class="content">
                <p>{t("email.magic.greeting", username=username)}</p>
                <p>{t("email.magic.description")}</p>
                <div style="text-align: center;">
                    <a href="{escape(magic_link)}" class="button">{t("email.magic.button")}</a>
                </div>
                <p>{t("email.magic.expiry")}</p>
                <p>{t("email.magic.ignore")}</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>{t("email.footer.automated")}</p>
                {powered_by}
            </div>
        </body>
        </html>
        """

    def _send_claim_entitlement_via_postmark(self, to_email: str, claim_url: str,
                                              product_name: str = None, branding: Dict = None, ui_language: str = "en") -> bool:
        """Send claim entitlement email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        t = Translator(ui_language).render
        b = self._get_branding(branding)
        html_body = self._create_claim_entitlement_email_template(claim_url, product_name, b, ui_language)

        company_name = b.get('company_name') or 'Aurvek'
        display_name = product_name or company_name
        subject = t("email.claim.subject", display_name=display_name)

        data = {
            "From": self.from_email,
            "To": to_email,
            "Subject": subject,
            "HtmlBody": html_body,
            "MessageStream": "outbound"
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(f"Claim entitlement email sent to {to_email}")
                return True
            else:
                logger.error(f"Failed to send claim entitlement email: {response.status_code} - {response.text}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending claim entitlement email via Postmark: {e}")
            return False

    def _create_claim_entitlement_email_template(self, claim_url: str,
                                                  product_name: str = None, branding: Dict = None, ui_language: str = "en") -> str:
        """Create HTML email template for entitlement claim with branding support"""
        translator = Translator(ui_language)

        def t(key, **params):
            return escape(translator.render(key, **params))

        b = branding or DEFAULT_BRANDING

        company_name = b.get('company_name') or 'Aurvek'
        primary_color = b.get('brand_color_primary') or '#6366f1'
        logo_url = b.get('logo_url')
        footer_text = b.get('footer_text') or ''
        email_signature = b.get('email_signature') or ''
        hide_platform_branding = b.get('hide_platform_branding', False)

        display_name = product_name or company_name
        title = t("email.claim.title")

        logo_html = ''
        if logo_url:
            logo_html = f'''
                <div style="margin-bottom: 15px;">
                    <img src="{escape(logo_url)}" alt="{escape(company_name)}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        footer_content = f"<p>{t('email.footer.experiences', company_name=company_name)}</p>"
        if footer_text:
            footer_content = f"<p>{escape(footer_text)}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{escape(email_signature)}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = f'<p style="font-size: 10px; color: #999; margin-top: 15px;">{t("email.footer.powered_by")}</p>'

        return f"""
        <!DOCTYPE html>
        <html lang="{translator.language}">
        <head>
            <meta charset="utf-8">
            <title>{title}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    text-align: center;
                    padding: 20px 0;
                    border-bottom: 2px solid {escape(primary_color)};
                }}
                .header h1 {{
                    color: {escape(primary_color)};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 14px 32px;
                    background-color: {escape(primary_color)};
                    color: white !important;
                    text-decoration: none;
                    border-radius: 8px;
                    font-weight: bold;
                    margin: 20px 0;
                }}
                .footer {{
                    text-align: center;
                    padding-top: 20px;
                    border-top: 1px solid #ddd;
                    color: #666;
                    font-size: 12px;
                }}
                .warning {{
                    background-color: #fef3c7;
                    border: 1px solid #f59e0b;
                    border-radius: 4px;
                    padding: 12px;
                    margin-top: 20px;
                    font-size: 14px;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                {logo_html}
                <h1>{title}</h1>
            </div>
            <div class="content">
                <p>{t("email.claim.intro", display_name=display_name)}</p>
                <p>{t("email.claim.description")}</p>
                <div style="text-align: center;">
                    <a href="{escape(claim_url)}" class="button">{t("email.claim.button")}</a>
                </div>
                <div class="warning">
                    {t("email.claim.expiry")}
                </div>
                <p>{t("email.claim.ignore")}</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>{t("email.footer.automated")}</p>
                {powered_by}
            </div>
        </body>
        </html>
        """


# Global email service instance
email_service = EmailService()
