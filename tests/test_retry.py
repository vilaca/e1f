"""Retry/backoff helper: honors Retry-After, falls back to exponential backoff."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from e1f.common.retry import _retry_after_seconds, call_with_retry


class FakeResponse:
    def __init__(self, status_code=429, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def http_error(status=429, headers=None):
    return requests.HTTPError(f"{status}", response=FakeResponse(status, headers))


def test_retry_after_seconds_parses_delay_seconds():
    assert _retry_after_seconds(FakeResponse(headers={'Retry-After': '17'})) == 17.0


def test_retry_after_seconds_parses_http_date():
    future = datetime.now(UTC) + timedelta(seconds=30)
    value = format_datetime(future, usegmt=True)
    assert 25 < _retry_after_seconds(FakeResponse(headers={'Retry-After': value})) <= 30


def test_retry_after_seconds_missing_or_invalid():
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds(FakeResponse()) is None
    assert _retry_after_seconds(FakeResponse(headers={'Retry-After': 'soon'})) is None


def test_honors_retry_after_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] == 1:
            raise http_error(429, {'Retry-After': '7'})
        return 'ok'

    assert call_with_retry('test', flaky) == 'ok'
    assert sleeps == [7.0]


def test_exponential_backoff_without_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)

    def always_429():
        raise http_error(500)

    with pytest.raises(requests.HTTPError):
        call_with_retry('test', always_429, retries=3, base_delay=2.0)
    assert sleeps == [2.0, 4.0, 8.0]


def test_backoff_capped_at_max_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)

    def always_429():
        raise http_error(429)

    with pytest.raises(requests.HTTPError):
        call_with_retry('test', always_429, retries=3, base_delay=100.0, max_delay=150.0)
    assert sleeps == [100.0, 150.0, 150.0]


def test_non_retryable_error_raises_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)

    def bad_request():
        raise http_error(400)

    with pytest.raises(requests.HTTPError):
        call_with_retry('test', bad_request)
    assert sleeps == []


def test_connection_error_is_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] == 1:
            raise requests.ConnectionError('boom')
        return 'ok'

    assert call_with_retry('test', flaky) == 'ok'
    assert sleeps == [2.0]


def test_is_retryable_extends_to_non_requests_errors(monkeypatch):
    monkeypatch.setattr('e1f.common.retry.time.sleep', lambda s: None)
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('429 Too Many Requests')
        return 'ok'

    def retryable(e: Exception) -> bool:
        return '429' in str(e)
    assert call_with_retry('test', flaky, is_retryable=retryable) == 'ok'


def test_is_retryable_does_not_catch_other_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr('e1f.common.retry.time.sleep', sleeps.append)

    def other_error():
        raise RuntimeError('some other error')

    with pytest.raises(RuntimeError):
        call_with_retry('test', other_error, is_retryable=lambda e: '429' in str(e))
    assert sleeps == []
