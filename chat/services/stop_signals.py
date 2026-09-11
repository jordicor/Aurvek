"""Native stop flags with opaque generation identity for deferred revocation."""
import secrets


class StopSignals(dict):
    """Preserve dict consumers; each native new-turn reset begins a new epoch."""
    def __init__(self):
        super().__init__()
        self._generations = {}

    def __setitem__(self, conversation_id, value):
        if value is False:
            self._generations[conversation_id] = secrets.token_hex(16)
        super().__setitem__(conversation_id, value)

    def __delitem__(self, conversation_id):
        super().__delitem__(conversation_id)
        self._generations.pop(conversation_id, None)

    def pop(self, conversation_id, *default):
        value = super().pop(conversation_id, *default)
        self._generations.pop(conversation_id, None)
        return value

    def clear(self):
        super().clear()
        self._generations.clear()

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def generation(self, conversation_id):
        return self._generations.get(conversation_id) if conversation_id in self else None

    def stop_generation(self, conversation_id, generation):
        """Compare and signal without an await that could select a later turn."""
        if not generation or self.generation(conversation_id) != generation:
            return False
        super().__setitem__(conversation_id, True)
        return True


stop_signals = StopSignals()
