# tts_load_balancer.py

import requests
from datetime import datetime, timedelta
import random
import os
from dotenv import load_dotenv


# Own/custom libraries
from log_config import logger

load_dotenv()  # Load variables from .env file

class APIKey:
    def __init__(self, key):
        self.key = key
        self.available_chars = 0
        self.reset_date = None
        self.last_used = None

    def update_info(self):
        url = "https://api.elevenlabs.io/v1/user"
        headers = {"xi-api-key": self.key}
        try:
            response = requests.get(url, headers=headers, timeout=(5, 15))
            response.raise_for_status()
            data = response.json()
            self.available_chars = data["subscription"]["character_limit"] - data["subscription"]["character_count"]
            self.reset_date = datetime.fromtimestamp(data["subscription"]["next_character_count_reset_unix"])
        except requests.RequestException as e:
            logger.error(
                "Error updating information for ElevenLabs API key ending in %s: %s",
                self.key[-4:],
                e,
            )
            self.available_chars = 0
            self.reset_date = None

class APIKeyManager:
    def __init__(self):
        self.api_keys = []
        self.last_update = None
        self.update_interval = timedelta(minutes=30)

    def add_key(self, key):
        if key:
            self.api_keys.append(APIKey(key))

    def update_all_keys(self):
        current_time = datetime.now()
        if self.last_update is None or (current_time - self.last_update) > self.update_interval:
            for api_key in self.api_keys:
                api_key.update_info()
            self.last_update = current_time

    def select_key(self):
        self.update_all_keys()
        
        if not self.api_keys:
            raise ValueError("No API keys available")

        current_time = datetime.now()
        scored_keys = []
        for api_key in self.api_keys:
            if api_key.reset_date is None:
                continue  # Skip this key if reset_date is None
            time_to_reset = max(0, (api_key.reset_date - current_time).total_seconds())
            score = api_key.available_chars * (1 + time_to_reset / (30 * 24 * 3600))
            if api_key.last_used:
                time_since_last_use = (current_time - api_key.last_used).total_seconds()
                score *= (1 + time_since_last_use / 3600)
            scored_keys.append((api_key, score))

        if not scored_keys:
            logger.error("No valid API keys available")
            return None

        total_score = sum(score for _, score in scored_keys)
        random_value = random.uniform(0, total_score)
        cumulative_score = 0
        for api_key, score in scored_keys:
            cumulative_score += score
            if cumulative_score > random_value:
                api_key.last_used = current_time
                return api_key

        return scored_keys[0][0]  # Return the first key if no key was selected

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

def get_elevenlabs_key():
    try:
        selected_key = api_key_manager.select_key()
        if selected_key is None:
            logger.error("No valid Elevenlabs API key available")
            return None
        return selected_key.key
    except Exception as e:
        logger.error(f"Error getting an Elevenlabs API key: {str(e)}")
        return None
        
# Print the number of loaded API keys
logger.info(f"Loaded {len(api_key_manager.api_keys)} API keys.")
