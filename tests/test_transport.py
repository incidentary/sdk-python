import io
import json
import urllib.error

from incidentary.transport import Transport


def make_http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost/api/v1/ingest/batch",
        code=status,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def test_transport_pauses_after_free_ce_limit(monkeypatch, capsys):
    errors = []
    current_time_s = 1_710_244_800.0
    monkeypatch.setattr("incidentary.transport.time.time", lambda: current_time_s)

    def raising_urlopen(*args, **kwargs):
        raise make_http_error(
            429,
            {
                "error": "ce_limit_reached",
                "limit_type": "ce",
                "plan": "free",
                "limit": 200000,
            },
        )

    monkeypatch.setattr("incidentary.transport.urllib.request.urlopen", raising_urlopen)

    transport = Transport(
        base_url="http://localhost:18080",
        api_key="test",
        service_name="svc",
        on_error=errors.append,
    )

    transport._do_upload(b"{}", None)

    assert transport.is_healthy is False
    assert transport._can_attempt_request() is False
    assert errors
    assert "Pausing ingest until" in str(errors[0])
    assert "incidentary_ce_limit_reached" in capsys.readouterr().out
