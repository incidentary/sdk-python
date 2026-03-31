# @incidentary/sdk-python

Python SDK for Incidentary.

## What pre-arm does

The SDK runs local anomaly triggers and can move capture mode from `NORMAL` to `PRE_ARMED` before external alerts:

- `slow_success`: successes stay successful but become much slower.
- `in_flight_pileup`: in-flight work grows faster than completion.
- `retry_onset`: outbound retries begin climbing.
- existing `5xx` trigger remains active.

Pre-arm is silent:

- no local paging
- no local incident creation
- only richer local capture and metadata while risk is elevated

When no incident binds, pre-arm expires and returns to `NORMAL`.

## Quick Start

```python
import os
from incidentary import IncidentaryClient

client = IncidentaryClient(
    api_key=os.environ["INCIDENTARY_API_KEY"],
    service_name="my-service",
    api_url=os.environ.get("INCIDENTARY_API_URL", "https://api.incidentary.com"),
)
```

## Event Vocabulary Helpers (Queue + Job + Webhook)

```python
from incidentary.types import RecordEventOptions

client.record_queue_publish(RecordEventOptions(event_attrs={"topic": "payments.jobs"}))
client.record_queue_consume()
client.record_job_start(RecordEventOptions(parent_ce_id=parent_ce_id))
client.record_job_end()
client.record_webhook_in()
client.record_webhook_out(RecordEventOptions(status=202))
```

Generic emitter:

```python
client.record_event("job_start", RecordEventOptions(event_attrs={"worker": "invoice-sync"}))
```

## Middleware usage

```python
from incidentary import IncidentaryWSGIMiddleware

app = IncidentaryWSGIMiddleware(app, client)
```

## Outbound instrumentation (retry-aware)

```python
from incidentary.middleware import instrumented_urlopen

response = instrumented_urlopen(
    client,
    {"trace_id": trace_id, "ce_id": parent_ce_id},
    "https://billing.internal/charges/123/capture",
    method="POST",
    data=b"{}",
    retry_metadata={
        "retry_attempt": 2,
        "route_template": "/charges/:id/capture",
        "downstream_service": "billing",
    },
)
```

Retry identity quality priority:

1. explicit retry metadata
2. route template
3. logical downstream edge
4. normalized URL fallback
5. unknown

`get_prearm_debug_state()` exposes per-quality usage and normalized URL fallback rate.

## Enhanced detail capture

Python SDK keeps one CE envelope and attaches optional `detail` only in elevated modes:

- `NORMAL`: base CE only
- `PRE_ARMED` / `INCIDENT`: base CE + optional detail metadata

Current detail fields include route metadata, request/response byte estimates, selected headers, retry/downstream metadata, and timeout/cancel classification.
Payload snippets are disabled by default and can be enabled with redaction + truncation.

## Configuration defaults

### State machine

- `pre_arm_threshold_high=10.0`
- `pre_arm_threshold_low=2.0`
- `pre_arm_min_duration_ms=60000`
- `pre_arm_ttl_ms=300000`
- `pre_arm_cooldown_ms=30000`

### Slow-success

- `pre_arm_enable_slow_success=True`
- `pre_arm_slow_min_ms=250`
- `pre_arm_slow_multiplier=2.0`
- `pre_arm_slow_alpha=0.1`
- `pre_arm_slow_success_rate_high=0.20`
- `pre_arm_slow_success_rate_mild=0.10`
- `pre_arm_slow_min_samples=50`
- `pre_arm_slow_include_4xx_as_success_like=True`

### In-flight pileup

- `pre_arm_enable_inflight=True`
- `pre_arm_inflight_min_abs=32`
- `pre_arm_inflight_multiplier=2.0`
- `pre_arm_inflight_net_growth_min=16`
- `pre_arm_inflight_hold_secs=3`
- `pre_arm_inflight_mild_hold_secs=2`

### Retry onset

- `pre_arm_enable_retry=True`
- `pre_arm_retry_window_ms=5000`
- `pre_arm_retry_rate_high=0.10`
- `pre_arm_retry_rate_mild=0.05`
- `pre_arm_retry_min_total=20`
- `pre_arm_retry_table_size=4096`

### Detail path

- `pre_arm_detail_capture_enabled=True`
- `pre_arm_detail_capture_payload_enabled=False`
- `pre_arm_detail_max_payload_bytes=4096`

## Debug status

Use `client.get_prearm_debug_state()` to inspect:

- trigger counters (`prearm_trigger_*`)
- state counters (`prearm_enter_total`, `prearm_bind_total`, `prearm_expire_total`)
- in-flight / slow-success / retry gauges
- retry key quality distribution and fallback rate
- last trigger record and active/recent pre-arm windows

## Benchmark

Run synthetic benchmark suite:

```bash
PYTHONPATH=src python bench_prearm.py
```

## Flush behavior

- Upload is asynchronous and fail-open.
- Retry backoff on failure: `1s`, `4s`, `16s`.
- After final retry failure, batch is dropped with warning log output.
- `capture_mode` uploads as `SKELETON` in `NORMAL` and `FULL` in `PRE_ARMED` / `INCIDENT`.

## Troubleshooting

1. Import error (`No module named incidentary`): ensure `PYTHONPATH=src` for local package testing.
2. 401 from ingest: verify workspace API key and API URL.
3. No CEs visible: ensure middleware or outbound instrumentation is actually writing events.
4. Frequent retries: inspect network path/API availability.
5. SDK version rejection (426): upgrade to `incidentary==0.2.0` or newer.
