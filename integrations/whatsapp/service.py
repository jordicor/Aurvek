import os

from chat.services.localization import chat_translator


phone_user_not_found = os.getenv(
    "WHATSAPP_UNKNOWN_USER_MESSAGE",
    "",
)


def get_phone_user_not_found(translator=None) -> str:
    return phone_user_not_found or chat_translator(translator).t("channel_notices.whatsapp_unknown")


def set_phone_user_not_found(message: str) -> None:
    global phone_user_not_found
    if message:
        phone_user_not_found = message
