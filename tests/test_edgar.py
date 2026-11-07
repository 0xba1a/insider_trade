import requests

from insider_trade.edgar import _is_missing_object


def _resp(status: int, body: str) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    return r


def test_s3_access_denied_is_missing_object():
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>'
    assert _is_missing_object(_resp(403, body))


def test_throttle_page_is_not_missing_object():
    body = "<html><head><title>SEC.gov | Request Rate Threshold Exceeded</title></head></html>"
    assert not _is_missing_object(_resp(403, body))
