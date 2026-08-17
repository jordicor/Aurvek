import os
import requests
import logging
from typing import Optional, Dict
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

    def send_magic_link_email(self, to_email: str, magic_link: str, username: str, branding: Dict = None) -> bool:
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

        return self._send_via_postmark(to_email, magic_link, username, branding)

    def send_ultra_admin_code(self, to_email: str, code: str, username: str) -> bool:
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
            subject = "Aurvek - Ultra Admin+ Verification Code"
            html_body = f"""
            <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #333;">Ultra Admin+ Verification</h2>
                <p>Your elevation code is:</p>
                <div style="background: #f5f5f5; border: 2px solid #e0e0e0; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                    <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #333;">{code}</span>
                </div>
                <p style="color: #666; font-size: 14px;">This code expires in 5 minutes. Do not share it.</p>
                <p style="color: #999; font-size: 12px;">If you did not request this, someone may have access to your account.</p>
            </div>
            """
            text_body = f"Your Ultra Admin+ elevation code is: {code}. This code expires in 5 minutes."

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
                                 prompt_name: str = None, branding: Dict = None) -> bool:
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

        return self._send_verification_via_postmark(to_email, verification_url, is_user, prompt_name, branding)

    def send_claim_entitlement_email(self, to_email: str, claim_url: str,
                                      product_name: str = None, branding: Dict = None) -> bool:
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

        return self._send_claim_entitlement_via_postmark(to_email, claim_url, product_name, branding)

    def _get_branding(self, branding: Dict = None) -> Dict:
        """Merge provided branding with defaults."""
        if branding is None:
            return DEFAULT_BRANDING.copy()
        result = DEFAULT_BRANDING.copy()
        result.update({k: v for k, v in branding.items() if v is not None})
        return result

    def _send_verification_via_postmark(self, to_email: str, verification_url: str, is_user: bool,
                                         prompt_name: str = None, branding: Dict = None) -> bool:
        """Send verification email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        b = self._get_branding(branding)
        html_body = self._create_verification_email_template(verification_url, is_user, prompt_name, b)

        # Use branding company name for subject
        company_name = b.get('company_name') or 'Aurvek'
        if is_user:
            subject = f"Verify your {company_name} account"
        else:
            display_name = prompt_name or company_name
            subject = f"Verify your account for {display_name}"

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
                                             prompt_name: str = None, branding: Dict = None) -> str:
        """Create HTML email template for verification with branding support"""
        b = branding or DEFAULT_BRANDING

        company_name = b.get('company_name') or 'Aurvek'
        primary_color = b.get('brand_color_primary') or '#6366f1'
        logo_url = b.get('logo_url')
        footer_text = b.get('footer_text') or ''
        email_signature = b.get('email_signature') or ''
        hide_platform_branding = b.get('hide_platform_branding', False)

        if is_user:
            title = f"Welcome to {company_name}"
            intro = f"Thank you for signing up as a creator on {company_name}!"
            description = "You're one step away from creating AI-powered experiences."
        else:
            display_name = prompt_name or company_name
            title = f"Welcome to {display_name}"
            intro = f"Thank you for signing up for {prompt_name or 'this experience'}!"
            description = "Click the button below to verify your email and get started."

        # Logo HTML
        logo_html = ''
        if logo_url:
            logo_html = f'''
                <div style="margin-bottom: 15px;">
                    <img src="{logo_url}" alt="{company_name}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        # Footer HTML
        footer_content = f"<p>{company_name} - AI-Powered Experiences</p>"
        if footer_text:
            footer_content = f"<p>{footer_text}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{email_signature}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = '<p style="font-size: 10px; color: #999; margin-top: 15px;">Powered by Aurvek</p>'

        return f"""
        <!DOCTYPE html>
        <html>
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
                    border-bottom: 2px solid {primary_color};
                }}
                .header h1 {{
                    color: {primary_color};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 14px 32px;
                    background-color: {primary_color};
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
                    <a href="{verification_url}" class="button">Verify Email</a>
                </div>
                <div class="warning">
                    <strong>Note:</strong> This link will expire in 24 hours.
                </div>
                <p>If you didn't create this account, you can safely ignore this email.</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>This is an automated message. Please do not reply to this email.</p>
                {powered_by}
            </div>
        </body>
        </html>
        """

    def _send_via_postmark(self, to_email: str, magic_link: str, username: str, branding: Dict = None) -> bool:
        """Send email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        b = self._get_branding(branding)
        html_body = self._create_email_template(magic_link, username, b)
        company_name = b.get('company_name') or 'Aurvek'

        data = {
            "From": self.from_email,
            "To": to_email,
            "Subject": f"Your {company_name} Magic Link",
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

    def _create_email_template(self, magic_link: str, username: str, branding: Dict = None) -> str:
        """Create HTML email template with branding support"""
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
                    <img src="{logo_url}" alt="{company_name}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        # Footer HTML
        footer_content = ''
        if footer_text:
            footer_content = f"<p>{footer_text}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{email_signature}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = '<p style="font-size: 10px; color: #999; margin-top: 15px;">Powered by Aurvek</p>'

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Your {company_name} Magic Link</title>
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
                    border-bottom: 2px solid {primary_color};
                }}
                .header h1 {{
                    color: {primary_color};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 12px 30px;
                    background-color: {primary_color};
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
                <h1>{company_name}</h1>
            </div>
            <div class="content">
                <p>Hello {username},</p>
                <p>Click the button below to access your account:</p>
                <div style="text-align: center;">
                    <a href="{magic_link}" class="button">Access Your Account</a>
                </div>
                <p><strong>Important:</strong> This magic link will expire in 3 days for security reasons.</p>
                <p>If you didn't request this, please ignore this email.</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>This is an automated message. Please do not reply to this email.</p>
                {powered_by}
            </div>
        </body>
        </html>
        """

    def _send_claim_entitlement_via_postmark(self, to_email: str, claim_url: str,
                                              product_name: str = None, branding: Dict = None) -> bool:
        """Send claim entitlement email via Postmark API"""
        url = "https://api.postmarkapp.com/email"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.postmark_token
        }

        b = self._get_branding(branding)
        html_body = self._create_claim_entitlement_email_template(claim_url, product_name, b)

        company_name = b.get('company_name') or 'Aurvek'
        display_name = product_name or company_name
        subject = f"Claim your access to {display_name}"

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
                                                  product_name: str = None, branding: Dict = None) -> str:
        """Create HTML email template for entitlement claim with branding support"""
        b = branding or DEFAULT_BRANDING

        company_name = b.get('company_name') or 'Aurvek'
        primary_color = b.get('brand_color_primary') or '#6366f1'
        logo_url = b.get('logo_url')
        footer_text = b.get('footer_text') or ''
        email_signature = b.get('email_signature') or ''
        hide_platform_branding = b.get('hide_platform_branding', False)

        display_name = product_name or company_name
        title = "Claim Your Access"

        logo_html = ''
        if logo_url:
            logo_html = f'''
                <div style="margin-bottom: 15px;">
                    <img src="{logo_url}" alt="{company_name}" style="max-width: 150px; max-height: 60px;">
                </div>
            '''

        footer_content = f"<p>{company_name} - AI-Powered Experiences</p>"
        if footer_text:
            footer_content = f"<p>{footer_text}</p>"
        if email_signature:
            footer_content += f"<p style='margin-top: 10px;'>{email_signature}</p>"

        powered_by = ''
        if not hide_platform_branding:
            powered_by = '<p style="font-size: 10px; color: #999; margin-top: 15px;">Powered by Aurvek</p>'

        return f"""
        <!DOCTYPE html>
        <html>
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
                    border-bottom: 2px solid {primary_color};
                }}
                .header h1 {{
                    color: {primary_color};
                    margin: 0;
                }}
                .content {{
                    padding: 30px 0;
                }}
                .button {{
                    display: inline-block;
                    padding: 14px 32px;
                    background-color: {primary_color};
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
                <p>You already have an account with us. Someone (possibly you) tried to create a new account with your email for <strong>{display_name}</strong>.</p>
                <p>Click the button below to add this product to your existing account:</p>
                <div style="text-align: center;">
                    <a href="{claim_url}" class="button">Claim Access</a>
                </div>
                <div class="warning">
                    <strong>Note:</strong> This link will expire in 24 hours. You will need to log in to complete the claim.
                </div>
                <p>If you didn't request this, you can safely ignore this email.</p>
            </div>
            <div class="footer">
                {footer_content}
                <p>This is an automated message. Please do not reply to this email.</p>
                {powered_by}
            </div>
        </body>
        </html>
        """


# Global email service instance
email_service = EmailService()
