"""External service client initialization.

Separated into its own module so Python's sys.modules caching prevents
double-execution per worker on Windows.  The multiprocessing "spawn" method
re-runs app.py as ``__mp_main__`` and then uvicorn imports it again as
``"app"``, causing all module-level code in app.py to execute TWICE per
worker.  Code in a separate module only executes once (the second import
finds it already cached in sys.modules).
"""

import os
import logging

import stripe
from twilio.request_validator import RequestValidator
from twilio_async import AsyncTwilioClient
from deepgram import DeepgramClient, DeepgramClientOptions

from common import twilio_sid, twilio_auth, STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------
if twilio_sid and twilio_auth:
    async_twilio = AsyncTwilioClient(twilio_sid, twilio_auth)
    twilio_validator = RequestValidator(twilio_auth)
    logger.info("Twilio client initialized successfully")
else:
    async_twilio = None
    twilio_validator = None
    logger.warning(
        "Twilio credentials not configured - WhatsApp and SMS verification disabled"
    )

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
from telegram_async import AsyncTelegramClient
from common import TELEGRAM_BOT_TOKEN

if TELEGRAM_BOT_TOKEN:
    async_telegram = AsyncTelegramClient(TELEGRAM_BOT_TOKEN)
    logger.info("Telegram client initialized successfully")
else:
    async_telegram = None
    logger.warning("Telegram bot token not configured - Telegram integration disabled")

# ---------------------------------------------------------------------------
# Deepgram (STT)
# ---------------------------------------------------------------------------
_deepgram_key = os.getenv("DEEPGRAM_KEY")
_deepgram_opts = DeepgramClientOptions(verbose=logging.SPAM)
deepgram: DeepgramClient = DeepgramClient(_deepgram_key, _deepgram_opts)

# ---------------------------------------------------------------------------
# STT engine selection
# ---------------------------------------------------------------------------
stt_engine: str = os.getenv("STT_ENGINE", "deepgram")
stt_fallback_enabled: bool = os.getenv("STT_FALLBACK_ENABLED", "0") == "1"
