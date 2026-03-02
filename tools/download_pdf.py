# tools/download_pdf.py

import os
import sys
import logging
import asyncio
import orjson
import aiosqlite
from datetime import datetime
from io import BytesIO
import hashlib
from urllib.parse import urlparse
import html
import markdown2
import emoji
from bs4 import BeautifulSoup, NavigableString, Tag
import xml.sax.saxutils
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    ListFlowable,
    ListItem,
    Paragraph,
    Spacer,
    Preformatted,
    HRFlowable,
    Table,
    TableStyle,
    Image as RLImage,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from PIL import Image as PilImage
from dotenv import load_dotenv

# Own libraries
from database import get_db_connection
from common import generate_user_hash

# =============================
# Logging Configuration
# =============================

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.INFO)
if hasattr(stream_handler, 'setEncoding'):
    stream_handler.setEncoding('utf-8')
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# =============================
# Load Environment Variables
# =============================

load_dotenv()

DB_NAME = os.getenv("DATABASE")
if not DB_NAME:
    logger.error("DATABASE is not defined in .env file")
    sys.exit(1)

# =============================
# Global Variables
# =============================

# Define base user path
BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'users')

# Path where fonts are stored
FONT_PATH = os.path.join("data", "static", "font")
MAIN_FONT_NAME = "Helvetica"
EMOJI_FONT_NAME = "NotoEmoji"  # Internal name of emoji font
EMOJI_FONT_FILE = "NotoEmoji-Regular.ttf"  # TTF file name of emoji font

# Default image path for images not found
image_not_found_path = os.path.join("data", "static", "images", "image_not_found.png")

# Maximum size for images
max_width = 400  # maximum width in pixels
max_height = 500  # maximum height in pixels

# =============================
# Auxiliary Functions
# =============================

def get_styles():
    # Register emoji font
    emoji_font_path = os.path.join(FONT_PATH, EMOJI_FONT_FILE)
    if os.path.exists(emoji_font_path):
        pdfmetrics.registerFont(TTFont(EMOJI_FONT_NAME, emoji_font_path))
        logger.debug(f"Emoji font registered: {EMOJI_FONT_NAME}")
    else:
        logger.error(f"Emoji font not found at: {emoji_font_path}")
        sys.exit(1)

    # Ensure main font is registered
    if MAIN_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        main_font_path = os.path.join(FONT_PATH, f"{MAIN_FONT_NAME}.ttf")
        if os.path.exists(main_font_path):
            pdfmetrics.registerFont(TTFont(MAIN_FONT_NAME, main_font_path))
        else:
            logger.error(f"Main font not found at: {main_font_path}")
            sys.exit(1)

    # Get sample style set
    styles = getSampleStyleSheet()

    # Modify header styles to use main font
    for heading in ['Heading1', 'Heading2', 'Heading3', 'Heading4', 'Heading5', 'Heading6']:
        styles[heading].fontName = MAIN_FONT_NAME

    # Modify normal style to use main font
    styles['Normal'].fontName = MAIN_FONT_NAME

    # Create or modify custom styles
    custom_styles = {
        'small_italic': {
            'parent': styles['Normal'],
            'fontName': MAIN_FONT_NAME,
            'fontSize': 8,
            'leading': 10,
            'italic': True,
        },
        'title': {
            'parent': styles['Normal'],
            'fontName': f"{MAIN_FONT_NAME}-Bold",
            'fontSize': 18,
            'alignment': TA_CENTER,
        },
        'subtitle': {
            'parent': styles['Normal'],
            'fontName': MAIN_FONT_NAME,
            'fontSize': 14,
            'alignment': TA_CENTER,
        },
        'user': {
            'parent': styles['Normal'],
            'fontName': f"{MAIN_FONT_NAME}-Bold",
            'fontSize': 12,
            'leading': 14,
        },
        'bot': {
            'parent': styles['Normal'],
            'fontName': MAIN_FONT_NAME,
            'fontSize': 12,
            'leading': 14,
        },
        'multi_ai_model': {
            'parent': styles['Normal'],
            'fontName': f"{MAIN_FONT_NAME}-Bold",
            'fontSize': 11,
            'leading': 13,
        },
        'multi_ai_error': {
            'parent': styles['Normal'],
            'fontName': MAIN_FONT_NAME,
            'fontSize': 10,
            'leading': 12,
            'textColor': colors.red,
        },
        'Code': {
            'parent': styles['BodyText'],
            'fontName': 'Courier',
            'fontSize': 9,
            'leading': 12,
            'leftIndent': 36,
            'rightIndent': 36,
            'backColor': colors.lightgrey
        },
        'Emoji': {
            'parent': styles['Normal'],
            'fontName': EMOJI_FONT_NAME,
            'fontSize': 12,
            'leading': 14,
        }
    }

    for style_name, style_attrs in custom_styles.items():
        if style_name in styles:
            # Modify existing style
            existing_style = styles[style_name]
            for attr, value in style_attrs.items():
                setattr(existing_style, attr, value)
        else:
            # Add new style
            styles.add(ParagraphStyle(name=style_name, **style_attrs))

    return {
        'header': styles['Normal'],  # Adjust if necessary
        'footer': styles['Normal'],  # Adjust if necessary
        'title': styles['title'],
        'subtitle': styles['subtitle'],
        'normal': styles['Normal'],
        'small_italic': styles['small_italic'],
        'user': styles['user'],
        'bot': styles['bot'],
        'multi_ai_model': styles['multi_ai_model'],
        'multi_ai_error': styles['multi_ai_error'],
        'Code': styles['Code'],
        'Emoji': styles['Emoji'],
        'Heading1': styles['Heading1'],
        'Heading2': styles['Heading2'],
        'Heading3': styles['Heading3'],
        'Heading4': styles['Heading4'],
        'Heading5': styles['Heading5'],
        'Heading6': styles['Heading6'],
        'Normal': styles['Normal'],
    }

def strip_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text()

def custom_unescape(text: str) -> str:
    return html.unescape(text)

def markdown_to_html(markdown_text: str) -> str:
    html_content = markdown2.markdown(
        markdown_text,
        extras=[
            "fenced-code-blocks",
            "tables",
            "footnotes",
            "strike",
            "task_list",
            "code-friendly",
            "cuddled-lists",
            "def_list",
        ],
    )
    logger.debug(f"Generated HTML: {html_content}")
    return html_content

def process_inline(element):
    """
    Process inline elements and return a string with ReportLab markup.
    Detects emojis and wraps them in an emoji font tag.
    """
    if isinstance(element, NavigableString):
        text = str(element)
        parts = []
        for char in text:
            if char in emoji.EMOJI_DATA:
                # Wrap emoji in emoji font tag
                parts.append(f'<font name="{EMOJI_FONT_NAME}">{char}</font>')
            else:
                parts.append(xml.sax.saxutils.escape(char))
        return ''.join(parts)
    elif isinstance(element, Tag):
        content = "".join(process_inline(child) for child in element.contents)
        if element.name in ["strong", "b"]:
            return f"<b>{content}</b>"
        elif element.name in ["em", "i"]:
            return f"<i>{content}</i>"
        elif element.name == "u":
            return f"<u>{content}</u>"
        elif element.name == "sub":
            return f"<sub>{content}</sub>"
        elif element.name == "sup":
            return f"<sup>{content}</sup>"
        elif element.name == "a":
            href = element.get("href", "")
            return f'<a href="{href}">{content}</a>'
        elif element.name == "br":
            return "<br/>"
        elif element.name == "code":
            return f'<font face="Courier">{content}</font>'
        elif element.name in ["strike", "s"]:
            return f"<strike>{content}</strike>"
        elif element.name == "input":
            # Handle checkboxes in task lists
            input_type = element.get("type", "")
            if input_type == "checkbox":
                checked = element.has_attr("checked")
                checkbox_char = "☑" if checked else "⬜"
                # Use emoji font for checkbox character
                return f'<font name="{EMOJI_FONT_NAME}">{checkbox_char}</font>'
            else:
                return content
        else:
            return content
    else:
        return ""

def process_p_tag(element, styles, hash_prefixes):
    flowables = []
    for child in element.contents:
        if isinstance(child, NavigableString) or (isinstance(child, Tag) and child.name != "img"):
            inline_html = process_inline(child)
            if inline_html:
                paragraph = Paragraph(inline_html, styles["Normal"])
                flowables.append(paragraph)
        elif isinstance(child, Tag) and child.name == "img":
            flowables.extend(process_element(child, styles, hash_prefixes))
    return flowables

def process_element(element, styles, hash_prefixes):
    """
    Process a BeautifulSoup element and convert it into a list of ReportLab flowables.
    """
    elements = []
    if isinstance(element, NavigableString):
        text = str(element)
        safe_text = xml.sax.saxutils.escape(text)
        if safe_text.strip():
            elements.append(Paragraph(safe_text, styles["Normal"]))
        return elements
    elif isinstance(element, Tag):
        if element.name == "p":
            # Use helper function to handle mixed content
            return process_p_tag(element, styles, hash_prefixes)
        elif element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            content = "".join(process_inline(child) for child in element.contents)
            heading_level = int(element.name[1])
            style_key = f"Heading{heading_level}"
            elements.append(Paragraph(content, styles[style_key]))
            return elements
        elif element.name in ["ul", "ol"]:
            bullet_type = "bullet" if element.name == "ul" else "1"
            items = []
            for li in element.find_all("li", recursive=False):
                # Detect if it's a task list
                first_child = li.contents[0] if li.contents else None
                is_task_item = False
                checkbox_char = ""
                if first_child and first_child.name == "input" and first_child.get("type") == "checkbox":
                    is_task_item = True
                    checked = first_child.has_attr("checked")
                    checkbox_char = "☑" if checked else "⬜"
                    # Remove <input> element from content
                    li.contents.pop(0)

                li_flowables = []
                inline_content = "".join(process_inline(child) for child in li.contents if isinstance(child, NavigableString) or (isinstance(child, Tag) and child.name not in ["ul", "ol"]))
                if inline_content.strip():
                    if is_task_item:
                        # Add checkbox at the beginning of text
                        inline_content = f'<font name="{EMOJI_FONT_NAME}">{checkbox_char}</font> {inline_content}'
                    paragraph = Paragraph(inline_content, styles["Normal"])
                    li_flowables.append(paragraph)

                # Process nested lists if any
                nested_lists = li.find_all(["ul", "ol"], recursive=False)
                for nested_list in nested_lists:
                    nested_flowables = process_element(nested_list, styles, hash_prefixes)
                    li_flowables.extend(nested_flowables)

                if li_flowables:
                    items.append(ListItem(li_flowables, leftIndent=20))

            if items:
                list_flowable = ListFlowable(
                    items,
                    bulletType=bullet_type,
                    leftIndent=20,
                    bulletFontName=MAIN_FONT_NAME,
                    bulletFontSize=10,
                    spaceBefore=6,
                    spaceAfter=6,
                    bulletDedent=10  # To properly align checkboxes
                )
                elements.extend([Spacer(1, 6), list_flowable, Spacer(1, 6)])
            return elements
        elif element.name in ["pre", "code"]:
            code_text = element.get_text()
            elements.append(Preformatted(code_text, styles["Code"]))
            return elements
        elif element.name == "hr":
            elements.append(HRFlowable(width="100%", thickness=1, lineCap="round", spaceBefore=10, spaceAfter=10, color=colors.grey))
            return elements
        elif element.name == "blockquote":
            content = "".join(process_inline(child) for child in element.contents)
            quote_style = ParagraphStyle(
                "Quote",
                parent=styles["Normal"],
                leftIndent=30,
                rightIndent=30,
                fontStyle="italic",
            )
            elements.append(Paragraph(content, quote_style))
            return elements
        elif element.name == "table":
            return process_table(element, styles)
        elif element.name == "img":
            # Handle images outside <p> tags
            src = element.get("src", "")
            alt = element.get("alt", "")

            # Check if filename has '_256.webp' suffix
            if "_256.webp" in src:
                # Replace with '_fullsize.webp'
                src = src.replace("_256.webp", "_fullsize.webp")

            # Parse URL to extract path
            parsed_url = urlparse(src)
            # Remove '/sk/' from beginning of path if it exists
            if parsed_url.path.startswith("/sk/"):
                relative_image_path = parsed_url.path[len("/sk/"):]
            else:
                relative_image_path = parsed_url.path.lstrip("/")
            logger.info(f"Relative image path extracted: {relative_image_path}")

            # Generate full path using hash prefixes
            hash_prefix1, hash_prefix2, user_hash = hash_prefixes
            full_image_path = os.path.join(BASE_DIR, hash_prefix1, hash_prefix2, user_hash, relative_image_path)

            logger.debug(f"Full image path: {full_image_path}")

            if os.path.exists(full_image_path):
                logger.debug(f"Image found at path: {full_image_path}")
                try:
                    with PilImage.open(full_image_path) as img:
                        width, height = img.size
                        if width > max_width or height > max_height:
                            img.thumbnail((max_width, max_height))
                            image_bytes = BytesIO()
                            img.save(image_bytes, format="PNG")
                            image_bytes.seek(0)
                            img_rl = RLImage(image_bytes)
                        else:
                            img_rl = RLImage(full_image_path)
                        img_rl.hAlign = "LEFT"
                        elements.append(img_rl)
                except Exception as e:
                    logger.error(f"Failed to load image: {e}")
            else:
                logger.warning(f"Image not found: {full_image_path}")
                if os.path.exists(image_not_found_path):
                    img_rl = RLImage(image_not_found_path, width=3 * inch, height=3 * inch)
                else:
                    logger.error(f"Image not found placeholder does not exist: {image_not_found_path}")
                    img_rl = Spacer(1, 3 * inch)  # Blank space if replacement image doesn't exist
                img_rl.hAlign = "LEFT"
                elements.append(img_rl)
            return elements
        else:
            for child in element.contents:
                elements.extend(process_element(child, styles, hash_prefixes))
            return elements
    else:
        return elements

def process_table(table, styles):
    """
    Process an HTML table and convert it to a ReportLab table.
    """
    data = []
    for row in table.find_all('tr'):
        row_data = []
        for cell in row.find_all(['td', 'th']):
            cell_content = "".join(process_inline(child) for child in cell.contents)
            row_data.append(Paragraph(cell_content, styles["Normal"]))
        data.append(row_data)

    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), f"{MAIN_FONT_NAME}-Bold"),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
        ('ALIGN', (0, 1), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 1), (-1, -1), MAIN_FONT_NAME),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ])

    return [Table(data, style=table_style)]

def html_to_reportlab(html_text, styles, hash_prefixes):
    elements = []
    if "<body>" not in html_text:
        html_text = f"<body>{html_text}</body>"
    soup = BeautifulSoup(html_text, "html.parser")
    for element in soup.body.contents:
        elements.extend(process_element(element, styles, hash_prefixes))
    return elements

# =============================
# Function to Generate and Save PDF
# =============================

async def generate_and_save_pdf(conversation_id: int, user_id: int, is_admin: bool):
    logger.debug(f"Starting PDF generation for conversation_id: {conversation_id}")

    # Use get_db_connection from database.py
    async with get_db_connection(readonly=True) as conn:
        # Verify permissions and conversation existence
        query_convo = """
            SELECT c.id, u.username, llm.machine, llm.model, p.name AS prompt_name
            FROM conversations c
            JOIN users u ON c.user_id = u.id
            LEFT JOIN llm ON c.llm_id = llm.id
            LEFT JOIN prompts p ON c.role_id = p.id
            WHERE c.id = ? AND (c.user_id = ? OR ?)
        """
        logger.debug(f"Verifying permissions and conversation existence")
        async with conn.execute(query_convo, (conversation_id, user_id, is_admin)) as cursor:
            conversation = await cursor.fetchone()
            if not conversation:
                logger.warning(f"Unauthorized access or conversation not found for conversation_id: {conversation_id}")
                return

        # Get messages
        query_messages = """
            SELECT id, date, message, type FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC, date ASC
        """
        logger.debug(f"Executing message query with id={conversation_id}")
        async with conn.execute(query_messages, (conversation_id,)) as cursor:
            messages = await cursor.fetchall()

    # Generate PDF
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=50, bottomMargin=50)
    elements = []
    styles = get_styles()

    # Calculate hash prefixes
    username = conversation["username"]
    hash_prefixes = generate_user_hash(username)
    logger.debug(f"Hash Prefixes for '{username}': {hash_prefixes}")

    # Add elements to PDF
    elements.append(
        HRFlowable(width="100%", thickness=1, lineCap="round", spaceBefore=10, spaceAfter=10, color=colors.grey)
    )
    elements.append(Paragraph(f'Conversation by: {conversation["username"]}', styles["title"]))
    elements.append(Spacer(1, 0.2 * inch))
    elements.append(Paragraph(f'LLM: {conversation["machine"]} {conversation["model"]}', styles["subtitle"]))
    elements.append(Spacer(1, 0.1 * inch))
    elements.append(Paragraph(f'Prompt: {conversation["prompt_name"]}', styles["subtitle"]))
    elements.append(Spacer(1, 0.3 * inch))

    for message in messages:
        date, text, sender_type = message["date"], message["message"], message["type"]
        sender_type_upper = sender_type.upper()

        try:
            date_obj = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
            date_str = date_obj.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            date_str = date

        text = html.unescape(custom_unescape(text))
        logger.info(f"Processing message: {text}")

        try:
            parsed_json = None
            stripped_raw = text.strip()
            if ((stripped_raw.startswith("[") and stripped_raw.endswith("]"))
                    or (stripped_raw.startswith("{") and stripped_raw.endswith("}"))):
                try:
                    parsed_json = orjson.loads(stripped_raw)
                except orjson.JSONDecodeError:
                    parsed_json = None

            if isinstance(parsed_json, dict) and parsed_json.get("multi_ai") and isinstance(parsed_json.get("responses"), list):
                elements.append(Spacer(1, 0.1 * inch))
                elements.append(Paragraph(f"{sender_type_upper}:", styles[sender_type.lower()]))
                elements.append(Spacer(1, 0.05 * inch))

                responses = parsed_json.get("responses", [])
                for idx, response in enumerate(responses):
                    if not isinstance(response, dict):
                        continue
                    model_label = response.get("model") or response.get("machine") or f"Model {idx + 1}"
                    model_label = html.escape(str(model_label))
                    response_content = response.get("content", "")
                    if response_content is None:
                        response_content = ""
                    response_text = str(response_content)

                    elements.append(Paragraph(f"{model_label}:", styles["multi_ai_model"]))

                    if response.get("error"):
                        safe_error = xml.sax.saxutils.escape(response_text)
                        elements.append(Paragraph(f"<i>{safe_error}</i>", styles["multi_ai_error"]))
                    else:
                        cleaned_response = strip_html(response_text)
                        if cleaned_response.strip():
                            html_text = markdown_to_html(response_text)
                            message_elements = html_to_reportlab(html_text, styles, hash_prefixes)
                            elements.extend(message_elements)
                    elements.append(Spacer(1, 0.04 * inch))

                elements.append(Paragraph(date_str, styles["small_italic"]))

            elif isinstance(parsed_json, list):
                # Message in JSON list format (text/images/videos)
                elements.append(Spacer(1, 0.1 * inch))
                elements.append(Paragraph(f"{sender_type_upper}:", styles[sender_type.lower()]))
                elements.append(Spacer(1, 0.05 * inch))
                for element_json in parsed_json:
                    if not isinstance(element_json, dict):
                        continue
                    if element_json.get("type") == "text":
                        html_text = markdown_to_html(str(element_json.get("text", "")))
                        message_elements = html_to_reportlab(html_text, styles, hash_prefixes)
                        elements.extend(message_elements)
                        elements.append(Spacer(1, 0.05 * inch))
                        elements.append(Paragraph(date_str, styles["small_italic"]))
                    elif element_json.get("type") == "image_url":
                        image_url = element_json.get("image_url", {}).get("url")
                        if image_url:
                            img_tag = f'<img src="{image_url}" alt="Image"/>'
                            html_text = markdown_to_html(img_tag)
                            message_elements = html_to_reportlab(html_text, styles, hash_prefixes)
                            elements.extend(message_elements)
                            elements.append(Spacer(1, 0.05 * inch))
                            elements.append(Paragraph(date_str, styles["small_italic"]))

            else:
                # Normal text message, possibly with Markdown
                stripped_text = strip_html(text)
                if stripped_text.strip() == "":
                    # If text is empty after removing HTML, skip
                    continue
                html_text = markdown_to_html(text)
                message_elements = html_to_reportlab(html_text, styles, hash_prefixes)
                elements.append(Spacer(1, 0.1 * inch))
                elements.append(Paragraph(f"{sender_type_upper}:", styles[sender_type.lower()]))
                elements.extend(message_elements)
                elements.append(Spacer(1, 0.05 * inch))
                elements.append(Paragraph(date_str, styles["small_italic"]))

        except Exception as e:
            logger.error(f"Error processing message ID {message['id']}: {e}")
            continue

    # Build PDF
    try:
        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        logger.debug(f"PDF generated successfully for conversation_id: {conversation_id}")
    except Exception as e:
        logger.error(f"Error building PDF: {e}")
        return

    # =============================
    # Changes Made Here
    # =============================

    # 1. Define base folder for PDFs within user structure
    # Base path: data/users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/{prefix1}/{prefix2}/pdf/
    user_hash = hash_prefixes[2]
    prefix1 = f"{conversation_id:07d}"[:3]
    prefix2 = f"{conversation_id:07d}"[3:]
    pdf_convo_folder = os.path.join(BASE_DIR, hash_prefixes[0], hash_prefixes[1], user_hash, "files", prefix1, prefix2, "pdf")
    os.makedirs(pdf_convo_folder, exist_ok=True)

    # 2. Generate timestamp
    timestamp = datetime.utcnow().strftime("%Y_%m_%d_%H_%M_%S")

    # 3. Define PDF filename with timestamp
    # We use 'prompt_name' as name. You can adjust this according to your needs.
    prompt_name_safe = ''.join(c for c in conversation["prompt_name"] if c.isalnum() or c in (' ', '_')).rstrip()
    prompt_name_safe = prompt_name_safe.replace(' ', '_')  # Replace spaces with underscores
    pdf_filename = f"{prompt_name_safe}_{timestamp}.pdf"

    # 4. Build full PDF file path
    pdf_file_path = os.path.join(pdf_convo_folder, pdf_filename)

    # 5. Save PDF to specified path
    try:
        with open(pdf_file_path, 'wb') as f:
            f.write(pdf_bytes)
        logger.debug(f"PDF saved successfully at {pdf_file_path} for conversation_id: {conversation_id}")
    except Exception as e:
        logger.error(f"Error saving PDF: {e}")
