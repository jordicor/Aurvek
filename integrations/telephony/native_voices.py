"""Provider voices accepted by native telephone model settings."""

GEMINI_LIVE_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})


def normalize_gemini_voice(value):
    voice = str(value or "Kore").strip()
    if voice not in GEMINI_LIVE_VOICES:
        raise ValueError("Select a supported Gemini Live voice")
    return voice
