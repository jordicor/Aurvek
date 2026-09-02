from types import SimpleNamespace

import pytest

from chat.services import page_context


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("42", 42),
        ("0007", 7),
        ("0", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("1.0", None),
        ("abc", None),
        (None, None),
    ],
)
def test_requested_conversation_id_accepts_only_positive_ascii_integers(
    raw_value,
    expected,
):
    query_params = {} if raw_value is None else {"conversation_id": raw_value}
    request = SimpleNamespace(query_params=query_params)

    assert page_context._requested_conversation_id(request) == expected


@pytest.mark.asyncio
async def test_requested_conversation_lookup_is_owned_visible_and_full_card_shape():
    class RecordingCursor:
        def __init__(self):
            self.query = ""
            self.params = None

        async def execute(self, query, params):
            self.query = query
            self.params = params

        async def fetchone(self):
            return ("requested-row",)

    cursor = RecordingCursor()

    row = await page_context._load_requested_conversation_row(cursor, 7, 42)

    assert row == ("requested-row",)
    assert cursor.params == (42, 7)
    normalized_query = " ".join(cursor.query.split()).lower()
    assert "c.id = ? and c.user_id = ?" in normalized_query
    assert "coalesce(c.hidden_from_history, 0) = 0" in normalized_query
    assert "c.folder_id" in normalized_query
    assert "coalesce(c.is_incognito, 0) as is_incognito" in normalized_query
    assert "c.locked" in normalized_query
    assert "l.model as llm_model" in normalized_query
