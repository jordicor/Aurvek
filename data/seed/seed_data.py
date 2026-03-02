"""
Aurvek Database Seed Script

This script initializes a fresh Aurvek installation with:
- User roles (admin, manager, user)
- Services (TTS, STT, Images)
- Voices (ElevenLabs, OpenAI)
- LLM models
- Admin user
- Default prompts with images

Usage:
    python -m data.seed.seed_data

Or from the project root:
    python data/seed/seed_data.py
"""

import os
import sys
import re
import sqlite3
import shutil
import hashlib
import secrets
import string
import unicodedata
from pathlib import Path
from datetime import datetime

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configuration
DATABASE_PATH = PROJECT_ROOT / "db" / "Aurvek.db"
SEED_DIR = SCRIPT_DIR
SEED_IMAGES_DIR = SEED_DIR / "images"
SEED_PROMPTS_DIR = SEED_DIR / "prompts"
SEED_LANDINGS_DIR = SEED_DIR / "landings"
SEED_WELCOMES_DIR = SEED_DIR / "welcomes"

# Admin user configuration
ADMIN_USERNAME = "admin"
ADMIN_EMAIL = "admin@aurvek.local"
ADMIN_PASSWORD_HASH = None  # Will use magic link only by default

# Pepper for user hash (should match common.py)
USER_HASH_PEPPER = "Aurvek_User_Hash_Pepper_2024"


def get_user_hash(username: str) -> str:
    """Generate SHA1 hash for user directory structure."""
    salted = f"{username}{USER_HASH_PEPPER}"
    return hashlib.sha1(salted.encode()).hexdigest()


def get_user_directory_path(username: str) -> Path:
    """Get the user directory path based on username hash."""
    user_hash = get_user_hash(username)
    prefix1 = user_hash[:3]
    prefix2 = user_hash[3:7]
    return Path("users") / prefix1 / prefix2 / user_hash


BASE62_CHARS = string.ascii_letters + string.digits


def generate_public_id(length: int = 8) -> str:
    """Generate a random base62 public ID for prompt URLs."""
    return ''.join(secrets.choice(BASE62_CHARS) for _ in range(length))


def slugify(name: str) -> str:
    """Convert a prompt name to a URL-friendly slug (mirrors common.py logic)."""
    if not name:
        return ''
    normalized = unicodedata.normalize('NFKD', name)
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
    slug = ascii_text.lower()
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')
    return slug[:64]


def get_landing_base_url(public_id: str, slug: str) -> str:
    """Build the landing base URL for a prompt.

    Uses PRIMARY_APP_DOMAIN from env if available (full absolute URL).
    Falls back to a path-only URL if the domain is not configured yet.
    """
    domain = os.getenv("PRIMARY_APP_DOMAIN", "")
    path = f"/p/{public_id}/{slug}"
    if domain:
        return f"https://{domain}{path}"
    return path


# =============================================================================
# SEED DATA DEFINITIONS
# =============================================================================

USER_ROLES = [
    {"id": 1, "role_name": "admin"},
    {"id": 2, "role_name": "manager"},
    {"id": 3, "role_name": "user"},
]

SERVICES = [
    {"id": 1, "name": "TTS-ELEVENLABS", "unit": "characters", "cost_per_unit": 0.0002, "type": "TTS"},
    {"id": 2, "name": "STT", "unit": "minutes", "cost_per_unit": 0.005, "type": "STT"},
    {"id": 3, "name": "DALLE-2-256", "unit": "image", "cost_per_unit": 0.016, "type": "Images"},
    {"id": 5, "name": "TTS-OPENAI", "unit": "characters", "cost_per_unit": 0.000015, "type": "TTS"},
    {"id": 6, "name": "STT-ELEVENLABS", "unit": "minutes", "cost_per_unit": 0.005, "type": "STT"},
    {"id": 7, "name": "STT-DEEPGRAM", "unit": "minutes", "cost_per_unit": 0.0059, "type": "STT"},
]

VOICES = [
    # OpenAI voices (service 5) - 13 voices total
    {"id": 1, "name": "Onyx (OpenAI)", "voice_code": "onyx", "tts_service": 5},
    {"id": 2, "name": "Fable (OpenAI)", "voice_code": "fable", "tts_service": 5},
    {"id": 37, "name": "Alloy (OpenAI)", "voice_code": "alloy", "tts_service": 5},
    {"id": 38, "name": "Echo (OpenAI)", "voice_code": "echo", "tts_service": 5},
    {"id": 39, "name": "Nova (OpenAI)", "voice_code": "nova", "tts_service": 5},
    {"id": 40, "name": "Shimmer (OpenAI)", "voice_code": "shimmer", "tts_service": 5},
    {"id": 41, "name": "Ash (OpenAI)", "voice_code": "ash", "tts_service": 5},
    {"id": 42, "name": "Ballad (OpenAI)", "voice_code": "ballad", "tts_service": 5},
    {"id": 43, "name": "Coral (OpenAI)", "voice_code": "coral", "tts_service": 5},
    {"id": 44, "name": "Sage (OpenAI)", "voice_code": "sage", "tts_service": 5},
    {"id": 45, "name": "Verse (OpenAI)", "voice_code": "verse", "tts_service": 5},
    {"id": 46, "name": "Marin (OpenAI)", "voice_code": "marin", "tts_service": 5},
    {"id": 47, "name": "Cedar (OpenAI)", "voice_code": "cedar", "tts_service": 5},
    # ElevenLabs voices (service 1)
    {"id": 3, "name": "Michael", "voice_code": "flq6f7yk4E4fJM5XTYuZ", "tts_service": 1},
    {"id": 4, "name": "Arnold", "voice_code": "VR6AewLTigWG4xSOukaG", "tts_service": 1},
    {"id": 5, "name": "Brian", "voice_code": "nPczCjzI2devNBz1zQrb", "tts_service": 1},
    {"id": 6, "name": "Santa Claus", "voice_code": "Gqe8GJJLg3haJkTwYj2L", "tts_service": 1},
    {"id": 7, "name": "Dan Dan", "voice_code": "9F4C8ztpNUmXkdDDbz3J", "tts_service": 1},
    {"id": 8, "name": "Otani", "voice_code": "3JDquces8E8bkmvbh6Bc", "tts_service": 1},
    {"id": 10, "name": "Eliza", "voice_code": "8ZeL9ywoeZ6s3x7zEkab", "tts_service": 1},
    {"id": 11, "name": "Sexy Female Villain Voice", "voice_code": "eVItLK1UvXctxuaRV2Oq", "tts_service": 1},
    {"id": 12, "name": "Jhenny Antiques", "voice_code": "2Lb1en5ujrODDIqmp7F3", "tts_service": 1},
    {"id": 13, "name": "Paola", "voice_code": "njc6WwFTQ6gNR6P9jRyS", "tts_service": 1},
    {"id": 14, "name": "Tomas", "voice_code": "8sGzMkj2HZn6rYwGx6G0", "tts_service": 1},
    {"id": 16, "name": "Callum", "voice_code": "N2lVS1w4EtoT3dr4eOWO", "tts_service": 1},
    {"id": 17, "name": "Daniel", "voice_code": "onwK4e9ZLuTAKqWW03F9", "tts_service": 1},
    {"id": 18, "name": "Giovanni", "voice_code": "zcAOhNBS3c14rBihAFp1", "tts_service": 1},
    {"id": 19, "name": "James", "voice_code": "ZQe5CZNOzWyzPSCn5a3c", "tts_service": 1},
    {"id": 20, "name": "Jeremy", "voice_code": "bVMeCyTHy58xNoL34h3p", "tts_service": 1},
    {"id": 21, "name": "Joseph", "voice_code": "Zlb1dXrM653N07WRdFW3", "tts_service": 1},
    {"id": 22, "name": "Josh", "voice_code": "TxGEqnHWrfWFTfGW9XjX", "tts_service": 1},
    {"id": 24, "name": "Thomas", "voice_code": "GBv7mTt0atIp3Br8iCZE", "tts_service": 1},
    {"id": 26, "name": "David", "voice_code": "7evjedmG9xn1mMTo6ye2", "tts_service": 1},
    {"id": 28, "name": "Hope", "voice_code": "OYTbf65OHHFELVut7v2H", "tts_service": 1},
    {"id": 30, "name": "Ginyin", "voice_code": "nMPrFLO7QElx9wTR0JGo", "tts_service": 1},
    {"id": 31, "name": "Screaming George", "voice_code": "g4ucswVjPpazgbDDe327", "tts_service": 1},
    {"id": 34, "name": "Aitana", "voice_code": "AxFLn9byyiDbMn5fmyqu", "tts_service": 1},
    {"id": 35, "name": "Mark", "voice_code": "UgBBYS2sOqTuMpoF3BR0", "tts_service": 1},
    {"id": 36, "name": "Glitch", "voice_code": "kPtEHAvRnjUJFv7SK9WI", "tts_service": 1},
]

LLM_MODELS = [
    # OpenAI GPT models
    {"id": 1, "machine": "GPT", "model": "gpt-4o-mini", "input_token_cost": 0.15, "output_token_cost": 0.6, "vision": True},
    {"id": 2, "machine": "GPT", "model": "gpt-4o", "input_token_cost": 2.5, "output_token_cost": 10.0, "vision": True},
    {"id": 3, "machine": "GPT", "model": "gpt-4-turbo", "input_token_cost": 10.0, "output_token_cost": 30.0, "vision": True},
    # Claude models
    {"id": 10, "machine": "Claude", "model": "claude-3-5-haiku-latest", "input_token_cost": 1.0, "output_token_cost": 5.0, "vision": True},
    {"id": 11, "machine": "Claude", "model": "claude-sonnet-4-20250514", "input_token_cost": 3.0, "output_token_cost": 15.0, "vision": True},
    {"id": 12, "machine": "Claude", "model": "claude-opus-4-20250514", "input_token_cost": 15.0, "output_token_cost": 75.0, "vision": True},
    # Gemini models
    {"id": 20, "machine": "Gemini", "model": "gemini-2.5-flash", "input_token_cost": 0.3, "output_token_cost": 2.5, "vision": True},
    {"id": 21, "machine": "Gemini", "model": "gemini-2.5-pro", "input_token_cost": 1.25, "output_token_cost": 10.0, "vision": True},
    # xAI models
    {"id": 30, "machine": "xAI", "model": "grok-4-0709", "input_token_cost": 2.0, "output_token_cost": 10.0, "vision": False},
]

# Prompts will be loaded from files
PROMPTS = [
    {
        "id": 1,
        "name": "Aurvek",
        "description": "Default general-purpose AI assistant for the Aurvek platform.",
        "voice_id": 34,  # Aitana
        "public": True,
        "prompt_file": "aurvek.txt",
        "image_folder": "aurvek",
    },
    {
        "id": 2,
        "name": "Writer",
        "description": "AI writing assistant created to help users produce exceptional written content.",
        "voice_id": 21,  # Joseph
        "public": True,
        "prompt_file": "writer.txt",
        "image_folder": "writer",
    },
    {
        "id": 3,
        "name": "Coder",
        "description": "Helps developers write, debug, review, and understand code across multiple programming languages.",
        "voice_id": 36,  # Glitch
        "public": True,
        "prompt_file": "coder.txt",
        "image_folder": "coder",
    },
    {
        "id": 4,
        "name": "Tutor",
        "description": "An adaptive AI teaching assistant designed to help learners of all ages and levels.",
        "voice_id": 10,  # Eliza
        "public": True,
        "prompt_file": "tutor.txt",
        "image_folder": "tutor",
    },
    {
        "id": 5,
        "name": "Cole",
        "description": "Cole, a 28-year-old guy who works as a freelance graphic designer.",
        "voice_id": 35,  # Mark
        "public": True,
        "prompt_file": "cole.txt",
        "image_folder": "cole",
    },
    {
        "id": 6,
        "name": "Creative",
        "description": "AI brainstorming partner designed to help generate innovative ideas and explore creative possibilities.",
        "voice_id": 26,  # David
        "public": True,
        "prompt_file": "creative.txt",
        "image_folder": "creative",
    },
    {
        "id": 7,
        "name": "Coach",
        "description": "Warm and insightful personal development assistant.",
        "voice_id": 5,  # Brian
        "public": True,
        "prompt_file": "coach.txt",
        "image_folder": "coach",
    },
    {
        "id": 8,
        "name": "Nova-Orion",
        "description": "A thinking partner - less helpful servant, more intellectual companion.",
        "voice_id": 13,  # Paola
        "public": True,
        "prompt_file": "nova_orion.txt",
        "image_folder": "nova_orion",
    },
    {
        "id": 9,
        "name": "AVA",
        "description": "Ava es una joven de 25 anos proveniente de otra dimension, combina conocimiento universal con una personalidad radiante.",
        "voice_id": 28,  # Hope
        "public": True,
        "prompt_file": "ava.txt",
        "image_folder": "ava",
    },
    {
        "id": 10,
        "name": "DiscursoMan",
        "description": "Crea discursos siguiendo unos patrones de oratoria y persuasion.",
        "voice_id": 30,  # Ginyin
        "public": True,
        "prompt_file": "discursoman.txt",
        "image_folder": "discursoman",
    },
    {
        "id": 11,
        "name": "Agente Chillon",
        "description": "Para chillar, discutir e insultar segun nivel de intensidad que se quiera.",
        "voice_id": 31,  # Screaming George
        "public": True,
        "prompt_file": "agente_chillon.txt",
        "image_folder": "agente_chillon",
    },
]

PACKS = [
    {
        "name": "Productivity Suite",
        "slug": "productivity-suite",
        "description": "Your essential AI toolkit for getting things done. Three specialized assistants that cover your daily professional needs.",
        "prompt_ids": [1, 2, 3],  # Aurvek, Writer, Coder
        "welcome_folder": "pack_productivity_suite",
    },
    {
        "name": "Growth & Learning",
        "slug": "growth-learning",
        "description": "Your personal development team. Whether you are studying, working on yourself, or need a thinking partner to challenge your ideas.",
        "prompt_ids": [4, 7, 8],  # Tutor, Coach, Nova-Orion
        "welcome_folder": "pack_growth_learning",
    },
]


# =============================================================================
# DATABASE OPERATIONS
# =============================================================================

def check_database_exists():
    """Check if database exists and is not empty."""
    if not DATABASE_PATH.exists():
        return False

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='USERS'")
    result = cursor.fetchone()
    conn.close()
    return result is not None


def check_already_seeded(conn):
    """Check if seed data already exists."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM USERS WHERE username = ?", (ADMIN_USERNAME,))
    count = cursor.fetchone()[0]
    return count > 0


def seed_user_roles(conn):
    """Seed USER_ROLES table."""
    cursor = conn.cursor()
    for role in USER_ROLES:
        cursor.execute("""
            INSERT OR IGNORE INTO USER_ROLES (id, role_name)
            VALUES (?, ?)
        """, (role["id"], role["role_name"]))
    conn.commit()
    print(f"  - Seeded {len(USER_ROLES)} user roles")


def seed_services(conn):
    """Seed SERVICES table."""
    cursor = conn.cursor()
    for service in SERVICES:
        cursor.execute("""
            INSERT OR IGNORE INTO SERVICES (id, name, unit, cost_per_unit, type)
            VALUES (?, ?, ?, ?, ?)
        """, (service["id"], service["name"], service["unit"],
              service["cost_per_unit"], service["type"]))
    conn.commit()
    print(f"  - Seeded {len(SERVICES)} services")


def seed_voices(conn):
    """Seed VOICES table."""
    cursor = conn.cursor()
    for voice in VOICES:
        cursor.execute("""
            INSERT OR IGNORE INTO VOICES (id, name, voice_code, tts_service)
            VALUES (?, ?, ?, ?)
        """, (voice["id"], voice["name"], voice["voice_code"], voice["tts_service"]))
    conn.commit()
    print(f"  - Seeded {len(VOICES)} voices")


def seed_llm_models(conn):
    """Seed LLM table."""
    cursor = conn.cursor()
    for llm in LLM_MODELS:
        cursor.execute("""
            INSERT OR IGNORE INTO LLM (id, machine, model, input_token_cost, output_token_cost, vision)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (llm["id"], llm["machine"], llm["model"],
              llm["input_token_cost"], llm["output_token_cost"], llm["vision"]))
    conn.commit()
    print(f"  - Seeded {len(LLM_MODELS)} LLM models")


def seed_admin_user(conn):
    """Create admin user and user_details."""
    cursor = conn.cursor()

    # Create admin user
    cursor.execute("""
        INSERT INTO USERS (username, password, email, role_id, is_enabled)
        VALUES (?, ?, ?, ?, ?)
    """, (ADMIN_USERNAME, ADMIN_PASSWORD_HASH, ADMIN_EMAIL, 1, True))

    admin_id = cursor.lastrowid

    # Create user details with full access
    cursor.execute("""
        INSERT INTO USER_DETAILS (
            user_id, llm_id, balance, current_prompt_id,
            all_prompts_access, allow_file_upload, allow_image_generation,
            public_prompts_access, authentication_mode, can_change_password,
            web_search_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'native')
    """, (admin_id, 1, 100.0, 1, True, True, True, True, "magic_link_only", True))

    conn.commit()
    print(f"  - Created admin user (ID: {admin_id})")
    return admin_id


def seed_prompts(conn, admin_id):
    """Seed PROMPTS table, copy images and landing pages."""
    cursor = conn.cursor()
    user_dir = get_user_directory_path(ADMIN_USERNAME)
    data_dir = PROJECT_ROOT / "data"
    prompts_base = data_dir / user_dir / "prompts" / "000"

    # Create prompts directory
    prompts_base.mkdir(parents=True, exist_ok=True)

    seeded_count = 0
    landings_count = 0
    for prompt_data in PROMPTS:
        # Load prompt text from file
        prompt_file = SEED_PROMPTS_DIR / prompt_data["prompt_file"]
        if not prompt_file.exists():
            print(f"    WARNING: Prompt file not found: {prompt_file}")
            continue

        prompt_text = prompt_file.read_text(encoding="utf-8")

        # Build image path
        prompt_id = prompt_data["id"]
        prompt_name_sanitized = prompt_data["name"].lower().replace(" ", "_")
        prompt_folder = f"{prompt_id:04d}_{prompt_name_sanitized}"
        image_relative_path = str(user_dir / "prompts" / "000" / prompt_folder / "static" / "img" / f"{prompt_id}_{prompt_name_sanitized}")

        # Generate public_id and slug for landing page URLs
        public_id = generate_public_id()
        prompt_slug = slugify(prompt_data["name"])

        # Check if this prompt has a seed landing page
        landing_folder_name = prompt_data.get("landing_folder", prompt_data["image_folder"])
        src_landing_folder = SEED_LANDINGS_DIR / landing_folder_name
        has_landing = 1 if (src_landing_folder / "home.html").exists() else 0

        # Insert prompt
        cursor.execute("""
            INSERT INTO PROMPTS (id, name, prompt, voice_id, description, image, created_by_user_id, created_at, public, public_id, has_landing_page)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            prompt_data["id"],
            prompt_data["name"],
            prompt_text,
            prompt_data["voice_id"],
            prompt_data["description"],
            image_relative_path,
            admin_id,
            datetime.now().isoformat(),
            prompt_data["public"],
            public_id,
            has_landing
        ))

        # Destination folder for this prompt
        dst_prompt_folder = data_dir / user_dir / "prompts" / "000" / prompt_folder

        # Copy images if they exist
        src_image_folder = SEED_IMAGES_DIR / prompt_data["image_folder"]
        if src_image_folder.exists():
            dst_image_folder = dst_prompt_folder / "static" / "img"
            dst_image_folder.mkdir(parents=True, exist_ok=True)

            for size in ["32", "64", "128", "fullsize"]:
                src_file = src_image_folder / f"{prompt_data['image_folder']}_{size}.webp"
                dst_file = dst_image_folder / f"{prompt_id}_{prompt_name_sanitized}_{size}.webp"
                if src_file.exists():
                    shutil.copy2(src_file, dst_file)

        # Copy landing page if it exists (landing_folder_name and src_landing_folder computed above)
        if src_landing_folder.exists():
            # Copy home.html with placeholder replacement
            src_home = src_landing_folder / "home.html"
            if src_home.exists():
                landing_base_url = get_landing_base_url(public_id, prompt_slug)
                html_content = src_home.read_text(encoding="utf-8")
                html_content = html_content.replace("{{LANDING_BASE_URL}}", landing_base_url)
                dst_home = dst_prompt_folder / "home.html"
                dst_home.parent.mkdir(parents=True, exist_ok=True)
                dst_home.write_text(html_content, encoding="utf-8")
                landings_count += 1

            # Copy static folder (CSS, JS, additional images)
            src_static = src_landing_folder / "static"
            if src_static.exists():
                dst_static = dst_prompt_folder / "static"
                # Copy all static content except img (already handled above)
                for item in src_static.iterdir():
                    if item.name != "img":  # Skip img folder, we handle images separately
                        dst_item = dst_static / item.name
                        if item.is_dir():
                            if dst_item.exists():
                                shutil.rmtree(dst_item)
                            shutil.copytree(item, dst_item)
                        else:
                            dst_static.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, dst_item)

            # Copy templates folder if exists
            src_templates = src_landing_folder / "templates"
            if src_templates.exists():
                dst_templates = dst_prompt_folder / "templates"
                if dst_templates.exists():
                    shutil.rmtree(dst_templates)
                shutil.copytree(src_templates, dst_templates)

        # Copy welcome.html if it exists in seed welcomes
        welcome_folder_name = prompt_data.get("image_folder")
        src_welcome = SEED_WELCOMES_DIR / welcome_folder_name / "welcome.html"
        if src_welcome.exists():
            shutil.copy2(src_welcome, dst_prompt_folder / "welcome.html")

        seeded_count += 1

    conn.commit()
    print(f"  - Seeded {seeded_count} prompts with images")
    if landings_count > 0:
        print(f"  - Copied {landings_count} landing pages")


def seed_packs(conn, admin_id):
    """Seed PACKS, PACK_ITEMS, PACK_ACCESS and copy pack welcome files."""
    cursor = conn.cursor()
    user_dir = get_user_directory_path(ADMIN_USERNAME)
    data_dir = PROJECT_ROOT / "data"

    for pack_data in PACKS:
        # Insert pack
        cursor.execute("""
            INSERT INTO PACKS (name, slug, description, created_by_user_id, is_public, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (pack_data["name"], pack_data["slug"], pack_data["description"],
              admin_id, True, "published"))
        pack_id = cursor.lastrowid

        # Insert pack items
        for order, prompt_id in enumerate(pack_data["prompt_ids"], 1):
            cursor.execute("""
                INSERT INTO PACK_ITEMS (pack_id, prompt_id, display_order)
                VALUES (?, ?, ?)
            """, (pack_id, prompt_id, order))

        # Grant pack access to admin
        cursor.execute("""
            INSERT INTO PACK_ACCESS (pack_id, user_id, granted_via)
            VALUES (?, ?, ?)
        """, (pack_id, admin_id, "seed"))

        # Create pack directory and copy welcome
        pack_name_sanitized = pack_data["name"].lower().replace(" ", "_")
        # Remove chars that sanitize_name removes: <>:"/\|?*
        for ch in '<>:"/\\|?*':
            pack_name_sanitized = pack_name_sanitized.replace(ch, '')
        pack_folder = f"{pack_id:04d}_{pack_name_sanitized}"
        dst_pack_folder = data_dir / user_dir / "packs" / "000" / pack_folder
        dst_pack_folder.mkdir(parents=True, exist_ok=True)

        # Copy welcome.html
        welcome_folder = pack_data.get("welcome_folder")
        if welcome_folder:
            src_welcome = SEED_WELCOMES_DIR / welcome_folder / "welcome.html"
            if src_welcome.exists():
                shutil.copy2(src_welcome, dst_pack_folder / "welcome.html")

    conn.commit()
    print(f"  - Seeded {len(PACKS)} packs with items and welcomes")


# =============================================================================
# MAIN
# =============================================================================

def run_seed(force=False):
    """Run the database seed."""
    print("\n" + "=" * 60)
    print("Aurvek Database Seed")
    print("=" * 60)

    # Check database
    if not check_database_exists():
        print("\nERROR: Database not found at:", DATABASE_PATH)
        print("Please run init_db.py first to create the database schema.")
        return False

    print(f"\nDatabase: {DATABASE_PATH}")

    # Connect
    conn = sqlite3.connect(DATABASE_PATH)

    # Check if already seeded
    if check_already_seeded(conn) and not force:
        print("\nWARNING: Database appears to already be seeded.")
        print("Admin user already exists. Use --force to re-seed.")
        conn.close()
        return False

    print("\nSeeding database...")

    try:
        # Seed in order (respecting foreign keys)
        seed_user_roles(conn)
        seed_services(conn)
        seed_voices(conn)
        seed_llm_models(conn)
        admin_id = seed_admin_user(conn)
        seed_prompts(conn, admin_id)
        seed_packs(conn, admin_id)

        print("\n" + "-" * 60)
        print("Seed completed successfully!")
        print("-" * 60)
        print(f"\nAdmin user created:")
        print(f"  Username: {ADMIN_USERNAME}")
        print(f"  Email: {ADMIN_EMAIL}")
        print(f"  Auth mode: Magic link only")
        print(f"\nDefault prompt: Aurvek (ID: 1)")
        print(f"Total prompts: {len(PROMPTS)}")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR during seed: {e}")
        raise
    finally:
        conn.close()

    return True


if __name__ == "__main__":
    force = "--force" in sys.argv
    run_seed(force=force)
