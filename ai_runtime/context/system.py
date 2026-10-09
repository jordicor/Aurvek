from ai_runtime.dependencies import *
from system_prompt_defaults import (
    DEFAULT_SYSTEM_BLOCKS,
    SYSTEM_BLOCK_METADATA,
    MANDATORY_SYSTEM_KEYS,
    APPLICATION_HELP_POLICY,
)

_BLOCK_VAR_PATTERN = re.compile(r'\{(user_level|current_datetime_utc)\}')

def _resolve_system_block(sys_key: str, content: str, is_enabled: bool) -> dict | None:
    """Resolve a system block from DB row, applying runtime policy.
    Returns the resolved block dict, or None if it should be excluded."""
    if sys_key not in SYSTEM_BLOCK_METADATA:
        return None
    meta = SYSTEM_BLOCK_METADATA[sys_key]
    default = DEFAULT_SYSTEM_BLOCKS[sys_key]
    if sys_key in MANDATORY_SYSTEM_KEYS:
        effective_content = content.strip() if content and content.strip() else default["content"]
        return {
            "system_key": sys_key,
            "content": effective_content,
            "position": meta["position"],
            "condition": meta["condition"],
        }
    if not is_enabled:
        return None
    effective_content = content.strip() if content and content.strip() else default["content"]
    return {
        "system_key": sys_key,
        "content": effective_content,
        "position": meta["position"],
        "condition": meta["condition"],
    }


def _scope_help_policy(blocks: list[dict], embedded: bool) -> list[dict]:
    if not embedded:
        return blocks
    return [{**block, 'content': APPLICATION_HELP_POLICY}
            if block.get('system_key') == 'platform_help_policy' else block for block in blocks]


async def get_effective_blocks(*, embedded: bool | None = None) -> list[dict]:
    """Fetch blocks for runtime prompt assembly.
    Known system blocks are resolved via _resolve_system_block (normalized, policy-enforced).
    Custom blocks: enabled only, as-is from DB.
    Missing system blocks: filled from code defaults.
    All sorted by position then display_order."""
    if embedded is None:
        from ai_runtime.channel_turns import current_channel_turn
        turn = current_channel_turn()
        embedded = turn is not None and turn.context.application is not None
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """SELECT system_key, content, position, condition,
                          is_enabled, is_system, display_order
                   FROM SYSTEM_PROMPT_BLOCKS
                   WHERE is_system = 1 OR is_enabled = 1
                   ORDER BY CASE WHEN position = 'pre_prompt' THEN 0 ELSE 1 END,
                            display_order ASC, id ASC"""
            )
            rows = await cursor.fetchall()
    except Exception:
        logger.warning("Failed to read SYSTEM_PROMPT_BLOCKS, using code defaults")
        return _scope_help_policy(sorted(DEFAULT_SYSTEM_BLOCKS.values(),
                      key=lambda b: (0 if b["position"] == "pre_prompt" else 1, b["display_order"])), embedded)

    blocks = []
    seen_system_keys = set()

    for sys_key, content, position, condition, is_enabled, is_system, display_order in rows:
        if sys_key and sys_key in SYSTEM_BLOCK_METADATA:
            if sys_key in seen_system_keys:
                logger.warning("Duplicate system block '%s', skipping", sys_key)
                continue
            seen_system_keys.add(sys_key)
            resolved = _resolve_system_block(sys_key, content, is_enabled)
            if resolved is None:
                continue
            resolved["display_order"] = SYSTEM_BLOCK_METADATA[sys_key]["display_order"]
            blocks.append(resolved)
        elif not sys_key and not is_system:
            blocks.append({
                "system_key": None,
                "content": content,
                "position": position,
                "condition": condition,
                "display_order": display_order,
            })
        else:
            logger.warning("Dropping invalid block row: system_key=%s, is_system=%s", sys_key, is_system)

    for key, default in DEFAULT_SYSTEM_BLOCKS.items():
        if key not in seen_system_keys:
            logger.warning("System block '%s' missing from DB, using code default", key)
            blocks.append(default)

    blocks.sort(key=lambda b: (0 if b["position"] == "pre_prompt" else 1, b.get("display_order", 0)))
    return _scope_help_policy(blocks, embedded)


def _render_block(block: dict, variables: dict) -> str:
    """Render a block's content with variable substitution."""
    rendered = _BLOCK_VAR_PATTERN.sub(
        lambda m: variables.get(m.group(1), m.group(0)), block["content"]
    )
    return rendered.strip()


def assemble_system_prompt(blocks: list[dict], variables: dict, prompt_base: str,
                           watchdog_enabled: bool, watchdog_hint_block: str = "") -> str:
    """Assemble the full system prompt from blocks, prompt_base, and optional watchdog hint."""
    pre_parts = []
    post_parts = []
    hint_inserted = False

    for block in blocks:
        if block["condition"] == "watchdog_only" and not watchdog_enabled:
            continue
        rendered = _render_block(block, variables)
        if not rendered:
            continue
        if block["position"] == "pre_prompt":
            pre_parts.append(rendered)
        else:
            post_parts.append(rendered)
            if (block.get("system_key") == "watchdog_preamble"
                    and watchdog_hint_block and not hint_inserted):
                hint = watchdog_hint_block.strip()
                if hint:
                    post_parts.append(hint)
                    hint_inserted = True

    if watchdog_enabled and watchdog_hint_block and not hint_inserted:
        hint = watchdog_hint_block.strip()
        if hint:
            post_parts.append(hint)

    all_parts = pre_parts + [prompt_base.strip()] + post_parts
    return "\n\n".join(p for p in all_parts if p)
