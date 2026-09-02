# tools/qr_code.py

import os
import orjson
import asyncio
import aiohttp
import dramatiq
import time
from dotenv import load_dotenv
from datetime import datetime
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Response, HTTPException, Depends, Request, Form, status, UploadFile, File
from urllib.parse import urlparse

import qrcode
from PIL import Image as PilImage
import io

# Own libraries
from rediscfg import redis_client, broker
from common import estimate_message_tokens, get_runtime_request_url
from models import User, ConnectionManager
from integrations.conversations import is_whatsapp_conversation
from tools import register_tool, register_dramatiq_task, register_function_handler
from save_images import save_image_locally, get_or_generate_img_token, resize_image
from auth import hash_password, verify_password, get_user_by_username, get_current_user, create_access_token, get_user_by_id, get_user_from_phone_number
from auth import get_current_user_from_websocket, get_user_id_from_conversation, get_user_by_token, create_user_info, create_login_response, generate_magic_link


load_dotenv()

# If you have a specific key for QR, you can define it here or use an existing one
# QR_API_KEY = os.getenv('QR_API_KEY')

async def generate_qr_code(text: str) -> bytes:
    print("enters generate_qr_code")
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    
    return buffer.getvalue()

def incorporate_qr_to_template(qr_bytes: bytes, template_path: str) -> bytes:
    print("enters incorporate_qr_to_template")
    # Open the template
    template = PilImage.open(template_path)

    # Open the QR code
    qr_img = PilImage.open(io.BytesIO(qr_bytes))

    # Get the template dimensions
    template_width, template_height = template.size

    # Calculate the desired size for the large QR (approximately 60% of the template width)
    qr_size = int(template_width * 0.6)

    # Calculate the position to center the QR horizontally and place it at the top
    left = (template_width - qr_size) // 2
    top = int(template_height * 0.25)  # Place the QR approximately at 25% from the top

    # Resize the QR code
    qr_img = qr_img.resize((qr_size, qr_size))

    # Paste the QR code at the calculated position
    template.paste(qr_img, (left, top))
    
    # Save the resulting image in a buffer
    buffer = io.BytesIO()
    template.save(buffer, format="PNG")
    buffer.seek(0)

    print("exits incorporate_qr_to_template")
    
    return buffer.getvalue()

async def generate_qr_task(channel_name: str, text: str, conversation_id: int, user_id: int, is_whatsapp: bool, request_url: str):
    """
    Dramatiq task to generate the QR, save it and publish the URL in the Redis channel.
    """
    try:
        print("enters generate_qr_task")
        # Generate the QR code
        qr_bytes = await generate_qr_code(text)
        
        # Template path
        template_path = os.path.join("data", "static", "images", "qr_template.png")
        
        # Incorporate the QR into the template
        final_qr_bytes = incorporate_qr_to_template(qr_bytes, template_path)
        
        # Define the filename
        filename = f"qr_code_{conversation_id}_{int(time.time())}.png"
        source = "bot"
        
        # Determine the format based on whether it's WhatsApp
        file_format = 'png' if is_whatsapp else 'webp'

        # Get the user from the user_id
        user = await get_user_by_id(user_id)
        
        # Extract scheme, host and port from request_url
        parsed_url = urlparse(request_url)
        scheme = parsed_url.scheme
        host = parsed_url.hostname
        port = parsed_url.port

        # Save the image locally and get the URLs
        _, _, qr_code_local_url, qr_code_token_url = await save_image_locally(
            request=None,  # We don't have access to the Request object here
            image_data=final_qr_bytes,
            current_user=user,
            conversation_id=conversation_id,
            filename=filename,
            source=source,
            format=file_format,
            scheme=scheme,
            host=host,
            port=port
        )
        
        # Create the content to send and save
        content_to_show = f"![QR Code]({qr_code_token_url})"
        
        content_to_save = orjson.dumps([
            {
                "type": "image_url",
                "image_url": {
                    "url": qr_code_local_url,
                    "alt": f"QR Code for: {text}"
                }
            }
        ]).decode()

        # Publish the content to show and save
        await redis_client.publish(channel_name, orjson.dumps({
            'content_to_show': content_to_show,
            'content_to_save': content_to_save
        }).decode())
        
        # Indicate the end of transmission
        await redis_client.publish(channel_name, 'END')
        
    except Exception as e:
        print(f"Error in generate_qr_task: {e}")
        await redis_client.publish(channel_name, orjson.dumps({'error': str(e)}).decode())
        await redis_client.publish(channel_name, 'END')

@dramatiq.actor
def generate_qr_code_task(channel_name: str, text: str, conversation_id: int, user_id: int, is_whatsapp: bool, request_url: str):
    asyncio.run(generate_qr_task(channel_name, text, conversation_id, user_id, is_whatsapp, request_url))

async def handle_generate_qr_code(function_arguments, messages, model, temperature, max_tokens, content, conversation_id, current_user, request, input_tokens, output_tokens, total_tokens, message_id, user_id, client, prompt, user_message=None):
    """
    Handler for the generateQRCode function. Sends the task to Dramatiq and waits for the response.
    """
    try:
        print("enters handle_generate_qr_code")
        text = function_arguments['text']
        channel_name = f"generate_qr_response_{conversation_id}_{user_id}_{int(time.time())}"
        
        # Determine if the conversation is from WhatsApp
        is_whatsapp = await is_whatsapp_conversation(conversation_id)
        
        # Get the request URL as a string
        request_url = get_runtime_request_url(request)
        
        # Send the task to Dramatiq with request_url instead of request
        generate_qr_code_task.send(channel_name, text, conversation_id, user_id, is_whatsapp, request_url)
        
        yield f"data: {orjson.dumps({'content': 'Generating QR code...', 'save_to_db': False, 'yield': True}).decode()}\n\n"
        
        # Subscribe to the channel and yield the messages
        async with redis_client.pubsub() as pubsub:
            await pubsub.subscribe(channel_name)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10.0)
                if message:
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    if data == 'END':
                        break
                    json_data = orjson.loads(data)
                    if 'error' in json_data:
                        yield f"data: {orjson.dumps({'content': json_data['error'], 'save_to_db': True, 'yield': True}).decode()}\n\n"
                    elif 'content_to_show' in json_data and 'content_to_save' in json_data:
                        # Send the content to show to the user
                        yield f"data: {orjson.dumps({'content': json_data['content_to_show'], 'save_to_db': False, 'yield': True}).decode()}\n\n"
                        # Send the content to save in the database
                        yield f"data: {orjson.dumps({'content': json_data['content_to_save'], 'save_to_db': True, 'yield': False}).decode()}\n\n"
                else:
                    await asyncio.sleep(0.1)
        
    except Exception as e:
        print(f"Error in handle_generate_qr_code: {e}")
        yield f"data: {orjson.dumps({'content': f'Error generating QR code: {str(e)}', 'save_to_db': True, 'yield': True}).decode()}\n\n"

# Tool definition for the semantic router
register_tool({
    "type": "function",
    "function": {
        "name": "generateQRCode",
        "description": "Generate a QR code based on the provided text",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to encode in the QR code"
                }
            },
            "required": ["text"],
            "additionalProperties": False
        }
    },
    "strict": True
})

# Register the task for Dramatiq
register_dramatiq_task("generate_qr_code_task", generate_qr_code_task)

# Register the function handler
register_function_handler("generateQRCode", handle_generate_qr_code)
