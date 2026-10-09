"""One model, voice, and tools-backend selection for phone, web, and devices."""

from dataclasses import dataclass
import json

from ai_runtime.reasoning import parse_reasoning_selection, resolve_and_validate
from llm_catalog import row_to_dict


NATIVE_KINDS = frozenset({"gemini_live", "openai_live"})
BLOCKED_MACHINES = frozenset({"GPTSub", "GranSabio", "O1"})
CONFIG_COLUMNS = (
    "realtime_llm_id", "realtime_reasoning_json", "realtime_voice",
    "realtime_backend_llm_id", "forced_llm_id", "forced_reasoning_json",
)


class RealtimeConfigurationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


async def load_prompt_configuration(connection, prompt_id):
    row = await (await connection.execute(
        f"SELECT {','.join(CONFIG_COLUMNS)} FROM PROMPTS WHERE id=?", (prompt_id,),
    )).fetchone()
    if row is None:
        raise RealtimeConfigurationError("realtime_prompt_unavailable", "The assistant is unavailable.")
    return dict(zip(CONFIG_COLUMNS, row))


async def load_model(connection, model_id, *, backend=False):
    row = await (await connection.execute("SELECT * FROM LLM WHERE id=?", (model_id,))).fetchone()
    model = row_to_dict(row) if row is not None else None
    code = "live_backend_unavailable" if backend else "live_model_unavailable"
    if model is None or not model["enabled"] or model["machine"] in BLOCKED_MACHINES:
        raise RealtimeConfigurationError(code, (
            "Select an enabled API model for tools in the assistant's Real-time settings."
            if backend else "Select an enabled model in the assistant's Real-time settings."
        ))
    runtime = model["capabilities"]["runtime"]
    if backend and (runtime["kind"] != "standard" or "web" not in runtime["channels"]):
        raise RealtimeConfigurationError(code, "The tools backend must be a standard text API model.")
    return model


def model_voice(kind, value):
    if kind == "gemini_live":
        from integrations.telephony.native_voices import normalize_gemini_voice
        return normalize_gemini_voice(value)
    if kind in {"openai_realtime", "openai_live"}:
        from integrations.telephony.openai_realtime import OPENAI_REALTIME_VOICES
        voice = str(value or "marin").strip().lower()
        if voice not in OPENAI_REALTIME_VOICES:
            raise ValueError("Select a supported OpenAI voice in Real-time settings.")
        return voice
    return None


@dataclass(frozen=True)
class RealtimeSelection:
    model: dict
    backend: dict | None
    reasoning: dict
    voice: str | None
    configuration: dict

    @property
    def kind(self):
        return self.model["capabilities"]["runtime"]["kind"]

    def matches(self, other):
        def identity(model):
            if model is None:
                return None
            capabilities = model["capabilities"]
            reasoning = {key: value for key, value in capabilities.get("reasoning", {}).items()
                         if key != "source"}
            return (tuple(model.get(key) for key in (
                "id", "machine", "model", "provider_key", "provider_model_id", "enabled",
                "input_token_cost", "output_token_cost", "max_output_tokens", "context_window_tokens",
            )), capabilities.get("runtime"), reasoning, capabilities.get("vision"))

        return (self.configuration == other.configuration and self.voice == other.voice
                and self.reasoning == other.reasoning and identity(self.model) == identity(other.model)
                and identity(self.backend) == identity(other.backend))


async def resolve_selection(connection, *, prompt_id, conversation_llm_id, channel,
                            configuration=None):
    config = configuration if configuration is not None else await load_prompt_configuration(connection, prompt_id)
    # An empty setting explicitly inherits the conversation model, as before.
    # It never selects an alternative native provider automatically.
    model = await load_model(connection, config["realtime_llm_id"] or conversation_llm_id)
    runtime = model["capabilities"]["runtime"]
    if channel not in runtime["channels"] or (channel in {"live", "device"} and runtime["kind"] not in NATIVE_KINDS):
        raise RealtimeConfigurationError("live_model_incompatible", (
            "The configured model cannot run on this connection. Select Gemini Live for camera "
            "or GPT Live for audio in Real-time settings. OpenAI Realtime and standard models use phone calls only."
        ))
    raw_reasoning = config["realtime_reasoning_json"]
    if raw_reasoning is None and config["realtime_llm_id"] is None:
        raw_reasoning = config["forced_reasoning_json"]
    try:
        reasoning = resolve_and_validate(parse_reasoning_selection(
            json.loads(raw_reasoning) if raw_reasoning else None), model["capabilities"]).to_dict()
        voice = model_voice(runtime["kind"], config["realtime_voice"])
    except (ValueError, TypeError) as exc:
        raise RealtimeConfigurationError("live_voice_unavailable", str(exc)) from exc
    backend = None
    if runtime["kind"] in NATIVE_KINDS:
        backend = await load_model(connection, config["realtime_backend_llm_id"]
                                   or config["forced_llm_id"] or conversation_llm_id, backend=True)
    return RealtimeSelection(model, backend, reasoning, voice, config)


async def validate_settings(connection, *, llm_id=None, backend_llm_id=None,
                            reasoning_selection=None, realtime_voice=None, inherited_llm_id=None):
    model = await load_model(connection, llm_id) if llm_id is not None else None
    if model is not None and not {"phone", "live", "device"}.intersection(model["capabilities"]["runtime"]["channels"]):
        raise RealtimeConfigurationError("live_model_incompatible", "This model has no supported real-time transport.")
    kind = model["capabilities"]["runtime"]["kind"] if model else "standard"
    if backend_llm_id is not None:
        if kind not in NATIVE_KINDS:
            raise RealtimeConfigurationError("live_backend_unavailable", "A separate tools API model requires Gemini Live or GPT Live.")
        await load_model(connection, backend_llm_id, backend=True)
    reasoning = None
    if reasoning_selection is not None:
        target = model or (await load_model(connection, inherited_llm_id) if inherited_llm_id else None)
        if target is None:
            raise ValueError("Select a specific model before setting real-time thinking.")
        reasoning = resolve_and_validate(reasoning_selection, target["capabilities"]).to_dict()
    if realtime_voice is not None and kind not in NATIVE_KINDS | {"openai_realtime"}:
        raise ValueError("A model voice requires a native real-time model.")
    return {
        "realtime_llm_id": llm_id, "realtime_backend_llm_id": backend_llm_id,
        "realtime_reasoning_selection": reasoning, "realtime_voice": model_voice(kind, realtime_voice),
    }


async def settings_response(connection, prompt_id):
    config = await load_prompt_configuration(connection, prompt_id)
    return {
        "realtime_llm_id": config["realtime_llm_id"],
        "realtime_backend_llm_id": config["realtime_backend_llm_id"],
        "realtime_voice": config["realtime_voice"],
        "realtime_reasoning_selection": json.loads(config["realtime_reasoning_json"]) if config["realtime_reasoning_json"] else None,
        "inherited_llm_id": config["forced_llm_id"],
    }


async def save_settings(connection, prompt_id, **values):
    config = await load_prompt_configuration(connection, prompt_id)
    normalized = await validate_settings(connection, inherited_llm_id=config["forced_llm_id"], **values)
    reasoning = normalized["realtime_reasoning_selection"]
    await connection.execute(
        "UPDATE PROMPTS SET realtime_llm_id=?,realtime_backend_llm_id=?,"
        "realtime_reasoning_json=?,realtime_voice=? WHERE id=?",
        (normalized["realtime_llm_id"], normalized["realtime_backend_llm_id"],
         json.dumps(reasoning) if reasoning is not None else None, normalized["realtime_voice"], prompt_id),
    )
    await connection.commit()
    return await settings_response(connection, prompt_id)
