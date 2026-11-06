from unittest.mock import Mock, patch

import pytest
import requests

from baomiguan.client import ApiError, BaomiClient


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = {} if data is None else data
    return result


def test_known_read_retries_with_timeout_and_no_redirects():
    session = Mock()
    session.request.side_effect = [requests.Timeout("token=private"), response({"success": True})]
    client = BaomiClient("secret-token", session)
    with patch("baomiguan.client.time.sleep"):
        assert client.get("/portal/main-api/checkToken.do") == {"success": True}
    assert session.request.call_count == 2
    options = session.request.call_args.kwargs
    assert options["timeout"] == (5, 20)
    assert options["allow_redirects"] is False
    assert options["headers"]["token"] == "secret-token"


@pytest.mark.parametrize("path", [
    "/portal/main-api/v2/studyTime/saveCoursePackage.do",
    "/portal/main-api/v2/studyTime/updateCoursePackageExamInfo.do",
    "/portal/main-api/v2/activity/exam/getExamContentData.do",
    "/unknown-endpoint",
])
def test_get_write_and_unknown_paths_are_never_retried(path):
    session = Mock()
    session.request.side_effect = requests.Timeout("token=do-not-echo")
    with pytest.raises(ApiError) as error:
        BaomiClient(session=session).get(path)
    assert session.request.call_count == 1
    assert "do-not-echo" not in str(error.value)


def test_post_is_never_retried():
    session = Mock()
    session.request.side_effect = requests.ConnectionError()
    with pytest.raises(ApiError):
        BaomiClient(session=session).post("/portal/main-api/loginInNew.do", json={})
    assert session.request.call_count == 1


@pytest.mark.parametrize("url", [
    "https://evil.example/portal/main-api/checkToken.do",
    "http://www.baomi.org.cn/portal/main-api/checkToken.do",
    "https://www.baomi.org.cn@evil.example/",
    "https://user:pass@www.baomi.org.cn/",
    "file:///tmp/example",
])
def test_only_official_https_host_is_accepted(url):
    session = Mock()
    with pytest.raises(ApiError):
        BaomiClient(session=session).get(url)
    session.request.assert_not_called()


def test_redirect_cannot_leak_authentication_to_another_host():
    session = Mock()
    session.request.return_value = response(status=302)
    with pytest.raises(ApiError, match="HTTP 302"):
        BaomiClient("private", session).get("/portal/main-api/checkToken.do")
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_bad_json_and_non_object_are_explicit_errors():
    session = Mock()
    session.request.return_value = response([])
    with pytest.raises(ApiError, match="结构变化"):
        BaomiClient(session=session).get("/example")
    session.request.return_value.json.side_effect = ValueError("bad json")
    with pytest.raises(ApiError, match="JSON"):
        BaomiClient(session=session).get("/example")


def test_business_errors_are_returned_for_service_to_interpret():
    session = Mock()
    session.request.return_value = response({"status": 4, "message": "activity ended"})
    assert BaomiClient(session=session).get("/example")["status"] == 4


@pytest.mark.parametrize("value", [0, "0", False, None])
def test_invalid_login_result_is_not_truthy(value):
    session = Mock()
    session.request.return_value = response({"result": value, "data": {"nickName": "name"}})
    assert BaomiClient("private", session).check_login()["loggedIn"] is False


def test_missing_token_does_not_call_server():
    session = Mock()
    assert BaomiClient(session=session).check_login() == {"loggedIn": False, "nickname": ""}
    session.request.assert_not_called()


def test_password_is_rsa_encrypted_and_unvalidated_token_is_rejected():
    from Crypto.PublicKey import RSA

    key = RSA.generate(1024).publickey().export_key().decode("ascii")
    session = Mock()
    session.request.side_effect = [
        response({"data": key}),
        response({"token": "new-token", "result": 1}),
        response({"result": "0", "data": {}}),
    ]
    client = BaomiClient("old-token", session)
    with pytest.raises(ApiError, match="未通过验证"):
        client.login_password("a-user", "a-password")
    login_payload = session.request.call_args_list[1].kwargs["json"]
    assert login_payload["loginName"] != "a-user"
    assert login_payload["passWord"] != "a-password"
    assert client.token == "old-token"


def test_create_qr_returns_original_payload_without_image_dependencies():
    session = Mock()
    payload = '{"params":{"qrToken":"test-qr"}}'
    session.request.return_value = response({"data": {"data": payload}})
    result = BaomiClient(session=session).create_qr()
    assert result == {"qrToken": "test-qr", "content": payload}


@pytest.mark.parametrize(("status", "state"), [(0, "waiting"), (-1, "expired"), (2, "waiting")])
def test_qr_nonconfirmed_states_preserve_existing_login(status, state):
    session = Mock()
    session.request.return_value = response({"data": {"data": status}})
    client = BaomiClient("existing", session)
    assert client.poll_qr("qr-token") == {"state": state}
    assert client.token == "existing"
    assert session.request.call_args.kwargs["params"] == {"qrToken": "qr-token"}


def test_confirmed_qr_is_checked_before_accepting_token():
    session = Mock()
    session.request.side_effect = [
        response({"data": {"data": 1}}),
        response({"result": 1, "data": {"nickName": "用户"}}),
    ]
    client = BaomiClient(session=session)
    assert client.poll_qr("qr-token") == {"state": "confirmed", "nickname": "用户"}
    assert client.token == "qr-token"
