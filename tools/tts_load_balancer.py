# tts_load_balancer.py

import requests
from datetime import datetime, timedelta
import random
import os
import threading
import time
from urllib.parse import quote
from dotenv import load_dotenv


# Own/custom libraries
from log_config import logger

load_dotenv()  # Load variables from .env file

ELEVENLABS_VOICE_URL = "https://api.elevenlabs.io/v1/voices/{voice_id}"
VOICE_ACCESS_CACHE_TTL_SECONDS = 30 * 60
VOICE_ACCESS_CACHE_MAX_ENTRIES = 512


def _normalize_voice_id(voice_id):
    if not isinstance(voice_id, str):
        raise ValueError("ElevenLabs voice ID must be a string")
    normalized = voice_id.strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 33 for character in normalized)
    ):
        raise ValueError("ElevenLabs voice ID is invalid")
    return normalized


class APIKey:
    def __init__(self, key):
        self.key = key
        self.available_chars = 0
        self.reset_date = None
        self.last_used = None
        self._voice_access_cache = {}
        self._voice_access_lock = threading.Lock()

    def update_info(self):
        url = "https://api.elevenlabs.io/v1/user"
        headers = {"xi-api-key": self.key}
        try:
            response = requests.get(url, headers=headers, timeout=(5, 15))
            response.raise_for_status()
            data = response.json()
            self.available_chars = (
                data["subscription"]["character_limit"]
                - data["subscription"]["character_count"]
            )
            self.reset_date = datetime.fromtimestamp(
                data["subscription"]["next_character_count_reset_unix"]
            )
        except requests.RequestException as exc:
            logger.error(
                "Error updating one ElevenLabs API key (%s)",
                type(exc).__name__,
            )
            self.available_chars = 0
            self.reset_date = None

    def can_access_voice(self, voice_id):
        """Return only proven, cached-or-live access to one exact voice."""

        normalized = _normalize_voice_id(voice_id)
        with self._voice_access_lock:
            now = time.monotonic()
            cached = self._voice_access_cache.get(normalized)
            if cached is not None:
                compatible, expires_at = cached
                if expires_at > now:
                    return compatible
                self._voice_access_cache.pop(normalized, None)

            try:
                response = requests.get(
                    ELEVENLABS_VOICE_URL.format(
                        voice_id=quote(normalized, safe="")
                    ),
                    headers={"xi-api-key": self.key},
                    timeout=(5, 15),
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Could not verify ElevenLabs voice access (%s)",
                    type(exc).__name__,
                )
                return False

            status = int(response.status_code)
            if status == 200:
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    logger.warning("ElevenLabs voice access returned invalid JSON")
                    return False
                if not isinstance(payload, dict):
                    logger.warning("ElevenLabs voice access returned invalid JSON")
                    return False
                resolved_voice_id = payload.get("voice_id")
                try:
                    normalized_resolution = _normalize_voice_id(
                        resolved_voice_id
                    )
                except ValueError:
                    logger.warning(
                        "ElevenLabs voice access returned no valid voice identity"
                    )
                    return False
                if resolved_voice_id != normalized_resolution:
                    logger.warning(
                        "ElevenLabs voice access returned no valid voice identity"
                    )
                    return False
                # ElevenLabs documents that retired Legacy IDs resolve to a
                # replacement Voice object.  This response proves key access;
                # it does not replace the requested ID, which remains the ID
                # sent by the TTS caller in every channel.
                compatible = True
            elif status in {400, 404, 422}:
                compatible = False
            else:
                logger.warning(
                    "Could not verify ElevenLabs voice access (HTTP %d)",
                    status,
                )
                return False

            self._remember_voice_access(normalized, compatible, now=now)
            return compatible

    def _remember_voice_access(self, voice_id, compatible, *, now):
        expired = [
            cached_voice
            for cached_voice, cached in self._voice_access_cache.items()
            if cached[1] <= now
        ]
        for cached_voice in expired:
            self._voice_access_cache.pop(cached_voice, None)
        while len(self._voice_access_cache) >= VOICE_ACCESS_CACHE_MAX_ENTRIES:
            oldest = next(iter(self._voice_access_cache))
            self._voice_access_cache.pop(oldest, None)
        self._voice_access_cache[voice_id] = (
            bool(compatible),
            now + VOICE_ACCESS_CACHE_TTL_SECONDS,
        )


class APIKeyManager:
    def __init__(self):
        self.api_keys = []
        self.last_update = None
        self.last_attempt = None
        self.update_interval = timedelta(minutes=30)
        self.failure_retry_interval = timedelta(seconds=30)
        self._ready = False
        self._update_lock = threading.Lock()

    def add_key(self, key):
        if key:
            self.api_keys.append(APIKey(key))

    def update_all_keys(self):
        current_time = datetime.now()
        if not self._update_is_due(current_time):
            return self.is_ready()
        with self._update_lock:
            current_time = datetime.now()
            if not self._update_is_due_from(
                current_time,
                last_attempt=self.last_attempt,
                ready=self._ready,
            ):
                return self._ready
            for api_key in self.api_keys:
                try:
                    api_key.update_info()
                except Exception as exc:
                    logger.error(
                        "Error updating one ElevenLabs API key (%s)",
                        type(exc).__name__,
                    )
                    api_key.available_chars = 0
                    api_key.reset_date = None
            self._ready = any(
                api_key.reset_date is not None for api_key in self.api_keys
            )
            self.last_attempt = current_time
            if self._ready:
                self.last_update = current_time
            return self._ready

    def _update_is_due(self, current_time):
        with self._update_lock:
            last_attempt = self.last_attempt
            ready = self._ready
        return self._update_is_due_from(
            current_time,
            last_attempt=last_attempt,
            ready=ready,
        )

    def _update_is_due_from(self, current_time, *, last_attempt, ready):
        if last_attempt is None:
            return True
        interval = self.update_interval if ready else self.failure_retry_interval
        return (current_time - last_attempt) > interval

    def is_ready(self):
        """Return the last published pool readiness without provider I/O."""

        # ``_ready`` is published only after a complete refresh.  Reading the
        # boolean directly is intentional: the refresh lock is held while the
        # provider requests run, and a cached readiness check must never wait
        # behind that network I/O (notably at the final Twilio dispatch fence).
        return bool(self._ready)

    def prepare(self):
        """Refresh the pool and report readiness without returning a secret."""

        return bool(self.update_all_keys())

    def select_key(self, voice_id=None):
        self.update_all_keys()

        if not self.api_keys:
            raise ValueError("No API keys available")

        current_time = datetime.now()
        scored_keys = []
        for api_key in self.api_keys:
            if api_key.reset_date is None:
                continue  # Skip this key if reset_date is None
            time_to_reset = max(
                0,
                (api_key.reset_date - current_time).total_seconds(),
            )
            score = api_key.available_chars * (
                1 + time_to_reset / (30 * 24 * 3600)
            )
            if api_key.last_used:
                time_since_last_use = (
                    current_time - api_key.last_used
                ).total_seconds()
                score *= (1 + time_since_last_use / 3600)
            scored_keys.append((api_key, score))

        if not scored_keys:
            logger.error("No valid API keys available")
            return None

        normalized_voice_id = (
            None if voice_id is None else _normalize_voice_id(voice_id)
        )
        remaining = list(scored_keys)
        while remaining:
            selected = _weighted_choice(remaining)
            remaining.remove(selected)
            api_key, _score = selected
            if (
                normalized_voice_id is not None
                and not api_key.can_access_voice(normalized_voice_id)
            ):
                continue
            api_key.last_used = current_time
            return api_key

        if normalized_voice_id is not None:
            logger.error("No API key has verified access to the ElevenLabs voice")
        return None


def _weighted_choice(scored_keys):
    total_score = sum(max(0, score) for _, score in scored_keys)
    if total_score <= 0:
        return scored_keys[0]
    random_value = random.uniform(0, total_score)
    cumulative_score = 0
    for item in scored_keys:
        cumulative_score += max(0, item[1])
        if cumulative_score > random_value:
            return item
    return scored_keys[-1]


# Initialize the API key manager
api_key_manager = APIKeyManager()

# Load API keys from the .env file
i = 1
while True:
    key = os.getenv(f"ELEVEN_KEY_{i}")
    if key:
        api_key_manager.add_key(key)
        i += 1
    else:
        break


def get_elevenlabs_key(voice_id=None):
    try:
        selected_key = api_key_manager.select_key(voice_id=voice_id)
        if selected_key is None:
            logger.error("No valid Elevenlabs API key available")
            return None
        return selected_key.key
    except Exception as e:
        logger.error(
            "Error getting an ElevenLabs API key (%s)",
            type(e).__name__,
        )
        return None


def has_elevenlabs_keys():
    """Report configured ElevenLabs capacity without consuming/probing a key."""

    return any(
        isinstance(api_key.key, str) and bool(api_key.key.strip())
        for api_key in api_key_manager.api_keys
    )


def prepare_elevenlabs_keys():
    """Warm provider metadata and expose only whether one key is selectable."""

    try:
        return bool(api_key_manager.prepare())
    except Exception as exc:
        logger.error(
            "Error preparing ElevenLabs API key pool (%s)",
            type(exc).__name__,
        )
        return False


def elevenlabs_keys_ready():
    """Return cached pool readiness without network I/O or secret material."""

    try:
        return bool(api_key_manager.is_ready())
    except Exception as exc:
        logger.error(
            "Error reading ElevenLabs API key pool readiness (%s)",
            type(exc).__name__,
        )
        return False


# Print the number of loaded API keys
logger.info(f"Loaded {len(api_key_manager.api_keys)} API keys.")
