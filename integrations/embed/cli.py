"""Operator-only registry management: python -m integrations.embed.cli --help."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from .identity import get_embed_store
from .models import AppConfig


async def run(args):
    if args.command == "configure":
        from integrations.applications.configuration import ApplicationConfiguration, apply_configuration
        config = ApplicationConfiguration.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
        if args.check:
            print(json.dumps({"app_id": config.app.app_id, "valid": True, "applied": False}))
            return
    store = get_embed_store()
    await store.initialize()
    if args.command == "configure":
        secret = lambda: os.environ.get("AURVEK_CLIENT_SECRET") or getpass.getpass(
            "Backend client secret (32+ random ASCII characters): ")
        print(json.dumps(await apply_configuration(store, config, secret)))
    elif args.command == "show":
        from integrations.applications.configuration import read_configuration
        print(json.dumps(await read_configuration(store, args.app_id), indent=2))
    elif args.command == "register":
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.prompt_public_id:
            async with store.connection(readonly=True) as connection:
                async with connection.execute("SELECT id FROM PROMPTS WHERE public_id=?", (args.prompt_public_id,)) as cursor:
                    prompt = await cursor.fetchone()
                    if not prompt:
                        raise ValueError("Prompt public ID not found in this environment")
                    data["prompt_id"] = int(prompt[0])
        config = AppConfig.model_validate(data)
        # Avoid putting secrets into process arguments, shell history, or stdout.
        secret = getpass.getpass("Backend client secret (32+ random ASCII characters): ")
        await store.register_app(config, secret)
        print(json.dumps({"app_id": config.app_id, "enabled": config.enabled}))
    elif args.command == "grant":
        if args.grant_prompt:
            from marketplace.services.entitlements import grant_prompt_entitlement
            config = await store.get_app(args.app_id)
            async with store.connection() as connection:
                await grant_prompt_entitlement(connection, user_id=args.user_id,
                    prompt_id=config.prompt_id, source="admin_manual")
                await connection.commit()
        subject = await store.provision_membership(args.app_id, args.user_id)
        print(json.dumps({"app_id": args.app_id, "subject": subject}))
    elif args.command == "revoke":
        await store.revoke_app_membership(args.app_id, args.subject)
        print(json.dumps({"app_id": args.app_id, "subject": args.subject, "status": "revoked"}))
    elif args.command == "disable":
        await store.set_app_enabled(args.app_id, False)
        print(json.dumps({"app_id": args.app_id, "enabled": False}))


def main():
    parser = argparse.ArgumentParser(description="Manage Aurvek applications and legacy embeds.")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Apply app, assistants, memory, tools and policy from operator JSON")
    configure.add_argument("config")
    configure.add_argument("--check", action="store_true", help="Validate JSON without database access or changes")
    show = sub.add_parser("show", help="Show application configuration without credentials or user data")
    show.add_argument("app_id")
    register = sub.add_parser("register", help="Create/update app from JSON; updates revoke prior sessions")
    register.add_argument("config")
    register.add_argument("--prompt-public-id", help="Resolve and verify prompt public ID in this environment")
    grant = sub.add_parser("grant", help="Invite an existing native account by verified numeric ID")
    grant.add_argument("app_id")
    grant.add_argument("user_id", type=int)
    grant.add_argument("--grant-prompt", action="store_true", help="Also grant native prompt entitlement")
    revoke = sub.add_parser("revoke", help="Revoke product membership; native rights remain unchanged")
    revoke.add_argument("app_id")
    revoke.add_argument("subject")
    disable = sub.add_parser("disable", help="Disable app and revoke sessions while reserving its hosts")
    disable.add_argument("app_id")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
