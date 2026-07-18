from src.interfaces.api_keys import (
    ApiKeyAdminListResponse,
    InvalidKeyInfo,
    InvalidKeyReason,
    invalid_key_info,
)


def test_every_reason_has_a_message():
    for reason in InvalidKeyReason:
        info = invalid_key_info(reason)
        assert isinstance(info, InvalidKeyInfo)
        assert info.reason == reason
        assert info.message  # non-empty human string


def test_admin_list_response_defaults_to_empty_invalid_map():
    resp = ApiKeyAdminListResponse(keys=["a"])
    assert resp.invalid_keys == {}
