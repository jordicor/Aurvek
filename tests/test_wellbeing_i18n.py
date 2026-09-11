from wellbeing_service import DEFAULT_CONFIG, _reminder_copy


def test_default_wellbeing_notice_exposes_localization_metadata():
    config = {
        "wellbeing_notice_text_soft": DEFAULT_CONFIG["wellbeing_notice_text_soft"][0]
    }

    assert _reminder_copy(config, "soft") == {
        "text": DEFAULT_CONFIG["wellbeing_notice_text_soft"][0],
        "text_key": "notice_soft",
        "text_source": "platform_default",
    }


def test_configured_wellbeing_notice_is_preserved_without_localization_key():
    configured = "Admin copy — preserve  spacing exactly."

    assert _reminder_copy({"wellbeing_notice_text_soft": configured}, "soft") == {
        "text": configured,
        "text_source": "admin_configured",
    }
