import asyncio
import os
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

import aiofiles.os
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from auth import get_current_user
from common import (
    CDN_FILES_URL,
    CLOUDFLARE_BASE_URL,
    ENABLE_CDN,
    GOOGLE_CLIENT_ID,
    SECRET_KEY,
    _get_marketplace_template_flags,
    decode_jwt_cached,
    generate_user_hash,
    get_template_context,
    templates,
    validate_path_within_directory,
    verify_token_expiration,
)
from captcha_service import get_captcha_config
from database import get_db_connection
from file_storage import (
    THUMB_VARIANT,
    attachment_content_url,
    attachment_download_url,
    delete_attachment_and_rewrite_message,
    ensure_file_storage_schema,
    prune_unreferenced_blobs,
)
from log_config import logger
from models import User
from prompts import get_user_directory
from save_images import get_or_generate_img_token
from storage_quota import delete_generated_file_rows
from chat.services.message_rendering import process_message
from chat.services.privacy import ensure_conversation_privacy_schema

router = APIRouter()


async def scan_pdf_directory(base_path: Path, conversation_id: int) -> List[Dict[str, str]]:
    pdfs = []
    try:
        prefix1 = f"{conversation_id:07d}"[:3]
        prefix2 = f"{conversation_id:07d}"[3:]
        pdf_path = base_path / prefix1 / prefix2 / "pdf"

        if await aiofiles.os.path.exists(str(pdf_path)):
            files = await aiofiles.os.listdir(str(pdf_path))
            for file in files:
                if file.endswith(".pdf"):
                    full_path = pdf_path / file
                    nginx_path = str(full_path).replace(os.sep, "/")
                    if not nginx_path.startswith("/users/"):
                        nginx_path = "/users/" + nginx_path.split("users/")[-1]

                    pdfs.append({
                        "path": str(full_path),
                        "nginx_path": nginx_path,
                        "name": file,
                    })
    except Exception as exc:
        logger.error("Error scanning PDF directory: %s", exc, exc_info=True)

    return pdfs


async def scan_audio_directory(base_path: Path, conversation_id: int) -> List[Dict[str, str]]:
    mp3s = []
    try:
        prefix1 = f"{conversation_id:07d}"[:3]
        prefix2 = f"{conversation_id:07d}"[3:]
        mp3_path = base_path / prefix1 / prefix2 / "mp3"

        if await aiofiles.os.path.exists(str(mp3_path)):
            files = await aiofiles.os.listdir(str(mp3_path))
            for file in files:
                if file.endswith(".mp3"):
                    full_path = mp3_path / file
                    nginx_path = str(full_path).replace(os.sep, "/")
                    if not nginx_path.startswith("/users/"):
                        nginx_path = "/users/" + nginx_path.split("users/")[-1]
                    mp3s.append({
                        "path": str(full_path),
                        "nginx_path": nginx_path,
                        "name": file,
                    })
    except Exception as exc:
        logger.error("Error scanning MP3 directory: %s", exc, exc_info=True)

    return mp3s


async def generate_file_url(file_path: str, token: str) -> str:
    return f"{CLOUDFLARE_BASE_URL}{quote(file_path)}?token={token}"


@router.get("/media-gallery", response_class=HTMLResponse)
async def media_gallery(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    images = []

    try:
        await ensure_conversation_privacy_schema()
        await ensure_file_storage_schema()
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()

            await cursor.execute(
                """
                SELECT c.id, u.username
                FROM CONVERSATIONS c
                JOIN USERS u ON c.user_id = u.id
                WHERE c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                """,
                (current_user.id,),
            )

            await cursor.execute(
                """
                SELECT fa.public_id, fa.original_filename, fa.message_id,
                       m.type, m.date
                FROM FILE_ATTACHMENTS fa
                JOIN CONVERSATIONS c ON c.id = fa.conversation_id
                JOIN MESSAGES m ON m.id = fa.message_id
                WHERE c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                  AND fa.attachment_type = 'image'
                  AND fa.status = 'active'
                ORDER BY m.date DESC, fa.id DESC
                """,
                (current_user.id,),
            )

            async for row in cursor:
                images.append({
                    "id": row["message_id"],
                    "attachment_ref": row["public_id"],
                    "url": attachment_content_url(row["public_id"], variant=THUMB_VARIANT),
                    "fullsize_url": attachment_content_url(row["public_id"]),
                    "name": row["original_filename"],
                    "type": row["type"],
                    "date": row["date"],
                })

            await cursor.execute(
                """
                SELECT m.id, m.message, m.type, m.date
                FROM MESSAGES m
                JOIN CONVERSATIONS c ON m.conversation_id = c.id
                WHERE c.user_id = ? AND m.message LIKE '%"type": "image_url"%'
                  AND COALESCE(c.hidden_from_history, 0) = 0
                ORDER BY m.date DESC
                """,
                (current_user.id,),
            )

            async for row in cursor:
                try:
                    processed_message = await process_message(row["message"], request, current_user)
                    processed_message_data = orjson.loads(processed_message)

                    def add_image_if_valid(image_url, row):
                        if not re.match(r"^http://localhost", image_url):
                            images.append({
                                "id": row["id"],
                                "url": image_url,
                                "type": row["type"],
                                "date": row["date"],
                            })

                    if isinstance(processed_message_data, list):
                        for item in processed_message_data:
                            if isinstance(item, dict) and item.get("type") == "image_url":
                                if item.get("image_url", {}).get("attachment_ref"):
                                    continue
                                add_image_if_valid(item["image_url"]["url"], row)
                    elif isinstance(processed_message_data, dict) and processed_message_data.get("type") == "image_url":
                        if processed_message_data.get("image_url", {}).get("attachment_ref"):
                            continue
                        add_image_if_valid(processed_message_data["image_url"]["url"], row)
                except orjson.JSONDecodeError:
                    continue

        context = await get_template_context(request, current_user)
        context.update({
            "images": images,
            "cdn_files_url": CDN_FILES_URL if ENABLE_CDN else "",
        })
        return templates.TemplateResponse("media_gallery.html", context)

    except Exception as exc:
        logger.error("Error in media_gallery: %s", exc)
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error_message": "Error loading the gallery",
            "marketplace": _get_marketplace_template_flags(),
        })


@router.get("/get-pdfs")
async def get_pdfs(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        from auth import unauthenticated_response
        return unauthenticated_response()

    pdfs = []
    pdf_token = None
    try:
        await ensure_conversation_privacy_schema()
        await ensure_file_storage_schema()
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT c.id, u.username
                FROM CONVERSATIONS c
                JOIN USERS u ON c.user_id = u.id
                WHERE c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                """,
                (current_user.id,),
            )

            conversations = []
            async for row in cursor:
                conversations.append({"id": row["id"], "username": row["username"]})

            await cursor.execute(
                """
                SELECT fa.public_id, fa.original_filename, fa.message_id,
                       fa.created_at, fb.page_count
                FROM FILE_ATTACHMENTS fa
                JOIN FILE_BLOBS fb ON fb.id = fa.blob_id
                JOIN CONVERSATIONS c ON c.id = fa.conversation_id
                WHERE c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                  AND fa.attachment_type = 'pdf'
                  AND fa.status = 'active'
                  AND fb.status = 'ready'
                ORDER BY fa.created_at DESC, fa.id DESC
                """,
                (current_user.id,),
            )

            async for row in cursor:
                public_id = row["public_id"]
                pdfs.append({
                    "path": public_id,
                    "nginx_path": attachment_download_url(public_id),
                    "url": attachment_download_url(public_id),
                    "name": row["original_filename"],
                    "attachment_ref": public_id,
                    "message_id": row["message_id"],
                    "pages": row["page_count"] or 0,
                    "kind": "attachment",
                })

        if conversations:
            username = conversations[0]["username"]
            files_path = Path(get_user_directory(username)) / "files"
            pdf_results = await asyncio.gather(
                *[scan_pdf_directory(files_path, conv["id"]) for conv in conversations],
                return_exceptions=True,
            )
            pdf_token = await get_or_generate_img_token(current_user)
            for result in pdf_results:
                if isinstance(result, list):
                    for pdf in result:
                        pdf["url"] = await generate_file_url(pdf["nginx_path"], pdf_token)
                    pdfs.extend(result)

        return JSONResponse(content={"pdfs": pdfs, "pdf_token": pdf_token})
    except Exception as exc:
        logger.error("Error in get_pdfs: %s", exc)
        return JSONResponse(status_code=500, content={"error": "Error loading PDFs"})


@router.get("/get-mp3s")
async def get_mp3s(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        from auth import unauthenticated_response
        return unauthenticated_response()

    mp3s = []
    mp3_token = None
    try:
        await ensure_conversation_privacy_schema()
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT c.id, u.username
                FROM CONVERSATIONS c
                JOIN USERS u ON c.user_id = u.id
                WHERE c.user_id = ?
                  AND COALESCE(c.hidden_from_history, 0) = 0
                """,
                (current_user.id,),
            )
            conversations = [{"id": row["id"], "username": row["username"]} async for row in cursor]

        if conversations:
            username = conversations[0]["username"]
            files_path = Path(get_user_directory(username)) / "files"
            mp3_results = await asyncio.gather(
                *[scan_audio_directory(files_path, conv["id"]) for conv in conversations],
                return_exceptions=True,
            )
            mp3_token = await get_or_generate_img_token(current_user)
            for result in mp3_results:
                if isinstance(result, list):
                    for mp3 in result:
                        mp3["url"] = await generate_file_url(mp3["nginx_path"], mp3_token)
                    mp3s.extend(result)

        return JSONResponse(content={"mp3s": mp3s, "mp3_token": mp3_token})
    except Exception as exc:
        logger.error("Error in get_mp3s: %s", exc)
        return JSONResponse(status_code=500, content={"error": "Error loading MP3s"})


@router.get("/download-pdf")
async def download_pdf(path: str, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    decoded_path = urllib.parse.unquote(path)
    if not decoded_path.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Invalid file type")

    user_base_path = Path(get_user_directory(current_user.username))
    validated_path = validate_path_within_directory(decoded_path, user_base_path)
    if not validated_path.is_file():
        raise HTTPException(status_code=404, detail="PDF not found")

    return FileResponse(
        path=validated_path,
        media_type="application/pdf",
        filename=validated_path.name,
    )


@router.get("/download-mp3")
async def download_mp3(path: str, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    decoded_path = urllib.parse.unquote(path).replace("/", os.path.sep)
    if not decoded_path.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Invalid file type")

    user_base_path = Path(get_user_directory(current_user.username))
    validated_path = validate_path_within_directory(decoded_path, user_base_path)
    if not validated_path.is_file():
        raise HTTPException(status_code=404, detail="MP3 not found")

    return FileResponse(
        path=validated_path,
        media_type="audio/mpeg",
        filename=validated_path.name,
    )


@router.get("/list-files")
async def list_files(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    user_base_path = Path(get_user_directory(current_user.username))
    files_path = user_base_path / "files"
    mp3_files = []
    pdf_files = []

    for root, dirs, files in os.walk(str(files_path)):
        for file in files:
            file_path = Path(root) / file
            relative_path = file_path.relative_to(user_base_path)
            if file.endswith(".mp3"):
                mp3_files.append(str(relative_path))
            elif file.endswith(".pdf"):
                pdf_files.append(str(relative_path))

    return JSONResponse(content={"mp3_files": mp3_files, "pdf_files": pdf_files})


@router.get("/auth-file")
async def auth_file(request: Request, request_uri: str, token: str):
    import jwt
    from fastapi.exceptions import HTTPException as FastAPIHTTPException

    if not token:
        logger.error("[auth_file] No token provided")
        raise HTTPException(status_code=401, detail="No token provided")

    try:
        logger.info("request_uri: %s", request_uri)
        payload = decode_jwt_cached(token, SECRET_KEY)
        if not verify_token_expiration(payload):
            logger.warning("[auth_file] Token expired")
            raise HTTPException(status_code=401, detail="Token expired")

        username = payload.get("username")
        if not username:
            logger.error("[auth_file] No username in token")
            raise HTTPException(status_code=401, detail="Invalid token")

        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
        user_base = Path(f"data/users/{hash_prefix1}/{hash_prefix2}/{user_hash}")
        expected_prefix = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/"

        request_uri = request_uri.strip()
        if request_uri.startswith("/"):
            request_uri = request_uri[1:]

        if request_uri.startswith("users/"):
            if not request_uri.startswith(expected_prefix):
                logger.warning("[auth_file] Token user does not match requested file path")
                raise HTTPException(status_code=403, detail="Access denied")
            relative_path = request_uri[len(expected_prefix):]
        else:
            relative_path = request_uri

        validate_path_within_directory(relative_path, user_base)
        return Response(status_code=200)

    except jwt.PyJWTError as exc:
        logger.error("[auth_file] JWT Error: %s", str(exc))
        raise HTTPException(status_code=401, detail="Invalid token")
    except FastAPIHTTPException:
        raise
    except Exception as exc:
        logger.error("[auth_file] Unexpected error: %s", str(exc))
        raise HTTPException(status_code=500, detail="An unexpected error occurred")


@router.post("/delete-pdf")
async def delete_pdf(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
        decoded_path = urllib.parse.unquote(body.get("pdf_path", ""))
        pdf_path = Path(decoded_path)

        if pdf_path.suffix != ".pdf":
            raise HTTPException(status_code=400, detail="Invalid PDF path")
        if not pdf_path.exists():
            raise HTTPException(status_code=404, detail="PDF not found")

        user_base_path = Path(get_user_directory(current_user.username))
        if not pdf_path.resolve().is_relative_to(user_base_path.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        os.remove(str(pdf_path))

        # Drop the ledger row for the file we just unlinked so it stops counting
        # against storage quota.
        async with get_db_connection() as conn:
            await delete_generated_file_rows(conn, [str(pdf_path.resolve())])
            await conn.commit()

        def remove_empty_dirs(path: Path):
            try:
                current = path
                while current != user_base_path:
                    if current.exists() and not any(current.iterdir()):
                        current.rmdir()
                    current = current.parent
            except Exception as exc:
                logger.error("Error removing empty directories: %s", exc)

        return JSONResponse(
            content={"message": "PDF deleted successfully", "path": str(pdf_path)},
            background=BackgroundTask(remove_empty_dirs, pdf_path.parent),
        )
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as exc:
        logger.error("Error deleting PDF: %s", exc)
        raise HTTPException(status_code=500, detail=f"Error deleting PDF: {str(exc)}")


@router.post("/delete-mp3")
async def delete_mp3(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
        raw_path = body.get("mp3_path", "")
        logger.info("Attempting to raw MP3 at path: %s", raw_path)
        decoded_path = urllib.parse.unquote(raw_path)
        mp3_path = Path(decoded_path.replace("/", os.path.sep))
        logger.info("Attempting to delete MP3 at path: %s", mp3_path)
        if not mp3_path or mp3_path.suffix != ".mp3":
            raise HTTPException(status_code=400, detail="Invalid MP3 path")
        if not os.path.exists(str(mp3_path)):
            raise HTTPException(status_code=404, detail="MP3 not found")

        user_base_path = Path(get_user_directory(current_user.username))
        if not mp3_path.resolve().is_relative_to(user_base_path.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        os.remove(str(mp3_path))

        # Drop the ledger row for the file we just unlinked so it stops counting
        # against storage quota.
        async with get_db_connection() as conn:
            await delete_generated_file_rows(conn, [str(mp3_path.resolve())])
            await conn.commit()

        def remove_empty_dirs(path: Path):
            try:
                current = path
                while str(current) > str(user_base_path):
                    if os.path.exists(str(current)) and not os.listdir(str(current)):
                        os.rmdir(str(current))
                    current = current.parent
            except Exception as exc:
                logger.error("Error removing empty directories: %s", exc)

        return JSONResponse(
            content={"message": "MP3 deleted successfully", "path": str(mp3_path)},
            background=BackgroundTask(remove_empty_dirs, mp3_path.parent),
        )
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as exc:
        logger.error("Error deleting MP3: %s", exc)
        raise HTTPException(status_code=500, detail=f"Error deleting MP3: {str(exc)}")


@router.post("/delete-pdfs")
async def delete_pdfs(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
        pdf_paths = body.get("pdf_paths", [])
        attachment_refs = body.get("attachment_refs", [])

        if not pdf_paths and not attachment_refs:
            raise HTTPException(status_code=400, detail="No PDF paths provided")

        user_base_path = Path(get_user_directory(current_user.username))
        deleted_count = 0
        failed_count = 0

        for public_id in attachment_refs:
            try:
                async with get_db_connection() as conn:
                    await conn.execute("BEGIN IMMEDIATE")
                    deleted = await delete_attachment_and_rewrite_message(
                        conn,
                        public_id=public_id,
                        user_id=current_user.id,
                        allow_admin=await current_user.is_admin,
                    )
                    await conn.commit()
                if deleted:
                    deleted_count += 1
                else:
                    failed_count += 1
            except Exception as exc:
                logger.error("Error deleting PDF attachment %s: %s", public_id, exc)
                failed_count += 1

        # Generated PDF exports unlinked below; their ledger rows are removed in
        # one batch afterwards. (attachment_refs are blob-store uploads, not
        # generated media -- freed by prune_unreferenced_blobs, never ledgered.)
        removed_pdf_paths = []
        for pdf_path in pdf_paths:
            path = Path(pdf_path)
            if path.suffix != ".pdf":
                failed_count += 1
                continue
            if not await aiofiles.os.path.exists(str(path)):
                failed_count += 1
                continue
            try:
                resolved_path = path.resolve()
                user_base_resolved = user_base_path.resolve()
            except OSError:
                failed_count += 1
                continue
            if not resolved_path.is_relative_to(user_base_resolved):
                failed_count += 1
                continue
            try:
                await aiofiles.os.remove(str(resolved_path))
                deleted_count += 1
                removed_pdf_paths.append(str(resolved_path))
            except Exception as exc:
                logger.error("Error deleting PDF %s: %s", path, exc)
                failed_count += 1

        if removed_pdf_paths:
            async with get_db_connection() as conn:
                await delete_generated_file_rows(conn, removed_pdf_paths)
                await conn.commit()

        if attachment_refs:
            await prune_unreferenced_blobs()

        return {"message": f"Successfully deleted: {deleted_count}, Failed: {failed_count}"}
    except Exception as exc:
        logger.error("Error in bulk PDF deletion: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/delete-mp3s")
async def delete_mp3s(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
        mp3_paths = body.get("mp3_paths", [])
        if not mp3_paths:
            raise HTTPException(status_code=400, detail="No MP3 paths provided")

        user_base_path = Path(get_user_directory(current_user.username))
        deleted_count = 0
        failed_count = 0

        # Generated MP3 exports unlinked below; their ledger rows are removed in
        # one batch afterwards.
        removed_mp3_paths = []
        for mp3_path in mp3_paths:
            path = Path(mp3_path)
            if path.suffix != ".mp3":
                failed_count += 1
                continue
            if not await aiofiles.os.path.exists(str(path)):
                failed_count += 1
                continue
            try:
                resolved_path = path.resolve()
                user_base_resolved = user_base_path.resolve()
            except OSError:
                failed_count += 1
                continue
            if not resolved_path.is_relative_to(user_base_resolved):
                failed_count += 1
                continue
            try:
                await aiofiles.os.remove(str(resolved_path))
                deleted_count += 1
                removed_mp3_paths.append(str(resolved_path))
            except Exception as exc:
                logger.error("Error deleting MP3 %s: %s", path, exc)
                failed_count += 1

        if removed_mp3_paths:
            async with get_db_connection() as conn:
                await delete_generated_file_rows(conn, removed_mp3_paths)
                await conn.commit()

        return {"message": f"Successfully deleted: {deleted_count}, Failed: {failed_count}"}
    except Exception as exc:
        logger.error("Error in bulk MP3 deletion: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
