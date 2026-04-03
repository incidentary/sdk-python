# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-01

### Added

- Exponential backoff retries for failed ingest uploads (1s, 4s, 16s).
- `sdk_telemetry` metadata in CE batch payloads.
- Explicit drop-after-retries warning behavior.
- Local pre-arm triggers: `slow_success`, `in_flight_pileup`, `retry_onset`.
- Downstream edge key resolution with tiered quality levels.
- Ring buffer with pre-alert capture ordering.
- Serverless support with flush-before-exit.
- Auto-instrument module for zero-config setup.

## [0.1.0] - 2026-02-01

### Added

- Initial release.
- `IncidentaryClient` with API key auth and configurable ingest URL.
- WSGI middleware for synchronous frameworks.
- ASGI middleware for async frameworks.
- Flask, Django, aiohttp, httpx integrations.
- Celery task and Kombu message instrumentation.
- psycopg2 and asyncpg DB query instrumentation.
- gRPC interceptor integration.
- Integration registry with auto-detection.
- Causal event types: HTTP_IN, HTTP_OUT, QUEUE_PUBLISH, QUEUE_CONSUME, INTERNAL.
- Event vocabulary helpers for queue, job, and webhook operations.
- Trace context propagation via `x-incidentary-trace-id` and `x-incidentary-parent-ce` headers.
