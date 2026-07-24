# External Service Mocking

<cite>
**Referenced Files in This Document**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [test_wecom_sdk_media.py](file://backend/tests/test_wecom_sdk_media.py)
- [test_qiniu_storage.py](file://backend/tests/test_qiniu_storage.py)
- [test_qiniu_provider_factory.py](file://backend/tests/test_qiniu_provider_factory.py)
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)
- [test_qiniu_https_domain.py](file://backend/tests/test_qiniu_https_domain.py)
- [test_media_download.py](file://backend/tests/test_media_download.py)
- [test_thumbnail_pipeline.py](file://backend/tests/test_thumbnail_pipeline.py)
- [mock_ingest.py](file://backend/scripts/mock_ingest.py)
- [smoke_wecom_get_chat_data.py](file://backend/scripts/smoke_wecom_get_chat_data.py)
- [smoke_wecom_sdk_init.py](file://backend/scripts/smoke_wecom_sdk_init.py)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)

## Introduction
This document explains how to mock external services for WeCom SDK and Qiniu storage integration testing within the project. It covers HTTP request mocking, simulating API responses, handling authentication flows, and testing error scenarios such as rate limiting, timeouts, and network failures. It also provides guidance for mocking media download operations from WeCom, file upload/download with Qiniu, worker processes, and provider factories used by the application.

## Project Structure
The relevant code for external service interactions and their tests is primarily located under backend/app and backend/tests:
- WeCom SDK integration: backend/app/sdk/wecom_sdk.py
- Qiniu storage integration: backend/app/qiniu_storage.py
- Media download orchestration: backend/app/media_download.py
- Thumbnail pipeline: backend/app/thumbnail_pipeline.py
- Tests for WeCom SDK media: backend/tests/test_wecom_sdk_media.py
- Tests for Qiniu storage and providers: backend/tests/test_qiniu_storage.py, test_qiniu_provider_factory.py, test_qiniu_https_domain.py
- Worker integration tests: backend/tests/test_qiniu_worker_integration.py
- Media download tests: backend/tests/test_media_download.py
- Thumbnail pipeline tests: backend/tests/test_thumbnail_pipeline.py
- Utility scripts for smoke tests and mocks: backend/scripts/mock_ingest.py, smoke_wecom_get_chat_data.py, smoke_wecom_sdk_init.py

```mermaid
graph TB
subgraph "Application"
WSDK["WeCom SDK<br/>sdk/wecom_sdk.py"]
QN["Qiniu Storage<br/>qiniu_storage.py"]
MD["Media Download<br/>media_download.py"]
TP["Thumbnail Pipeline<br/>thumbnail_pipeline.py"]
end
subgraph "Tests"
TW["test_wecom_sdk_media.py"]
TQ["test_qiniu_storage.py"]
TPF["test_qiniu_provider_factory.py"]
TQW["test_qiniu_worker_integration.py"]
TMD["test_media_download.py"]
TTP["test_thumbnail_pipeline.py"]
end
subgraph "Scripts"
MI["mock_ingest.py"]
SWCD["smoke_wecom_get_chat_data.py"]
SWSI["smoke_wecom_sdk_init.py"]
end
TW --> WSDK
TQ --> QN
TPF --> QN
TQW --> QN
TMD --> MD
TTP --> TP
MI --> WSDK
SWCD --> WSDK
SWSI --> WSDK
```

**Diagram sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [test_wecom_sdk_media.py](file://backend/tests/test_wecom_sdk_media.py)
- [test_qiniu_storage.py](file://backend/tests/test_qiniu_storage.py)
- [test_qiniu_provider_factory.py](file://backend/tests/test_qiniu_provider_factory.py)
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)
- [test_media_download.py](file://backend/tests/test_media_download.py)
- [test_thumbnail_pipeline.py](file://backend/tests/test_thumbnail_pipeline.py)
- [mock_ingest.py](file://backend/scripts/mock_ingest.py)
- [smoke_wecom_get_chat_data.py](file://backend/scripts/smoke_wecom_get_chat_data.py)
- [smoke_wecom_sdk_init.py](file://backend/scripts/smoke_wecom_sdk_init.py)

**Section sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [test_wecom_sdk_media.py](file://backend/tests/test_wecom_sdk_media.py)
- [test_qiniu_storage.py](file://backend/tests/test_qiniu_storage.py)
- [test_qiniu_provider_factory.py](file://backend/tests/test_qiniu_provider_factory.py)
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)
- [test_media_download.py](file://backend/tests/test_media_download.py)
- [test_thumbnail_pipeline.py](file://backend/tests/test_thumbnail_pipeline.py)
- [mock_ingest.py](file://backend/scripts/mock_ingest.py)
- [smoke_wecom_get_chat_data.py](file://backend/scripts/smoke_wecom_get_chat_data.py)
- [smoke_wecom_sdk_init.py](file://backend/scripts/smoke_wecom_sdk_init.py)

## Core Components
- WeCom SDK: Provides methods to interact with WeCom APIs (e.g., token acquisition, media download). For testing, you can replace or wrap its HTTP calls to simulate responses and errors.
- Qiniu Storage: Implements upload/download operations and signed URL generation. Tests should mock HTTP requests to Qiniu endpoints and simulate success/failure paths.
- Media Download: Orchestrates fetching media from WeCom and storing it via storage backends. Testing focuses on mocking WeCom media endpoints and storage operations.
- Thumbnail Pipeline: Processes thumbnails after media download. Tests should mock image processing steps and storage writes.

Key patterns for mocking:
- Replace HTTP clients with test doubles that return predefined responses or raise exceptions.
- Use environment variables or configuration flags to switch between real and mocked endpoints.
- Inject mock providers into factory functions to control behavior deterministically.

**Section sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

## Architecture Overview
The system integrates WeCom SDK for message and media retrieval and Qiniu storage for persistent media management. Tests isolate these integrations by mocking HTTP calls and simulating various response scenarios.

```mermaid
sequenceDiagram
participant Test as "Test Case"
participant WSDK as "WeCom SDK"
participant QN as "Qiniu Storage"
participant MD as "Media Download"
participant TP as "Thumbnail Pipeline"
Test->>WSDK : "Mock token/media endpoints"
WSDK-->>Test : "Simulated access token"
Test->>MD : "Trigger media download flow"
MD->>WSDK : "Request media content"
WSDK-->>MD : "Mocked media bytes"
MD->>QN : "Upload media"
QN-->>MD : "Mocked upload success"
MD->>TP : "Generate thumbnail"
TP-->>MD : "Mocked thumbnail result"
MD-->>Test : "Download complete"
```

**Diagram sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

## Detailed Component Analysis

### WeCom SDK Mocking
- Authentication Flow: Mock token acquisition by intercepting HTTP requests to WeCom auth endpoints. Return valid tokens or simulate failures (e.g., invalid credentials).
- Media Download: Intercept media download requests and return predefined binary data or simulate network errors.
- Rate Limiting: Simulate 429 responses and verify retry/backoff logic.
- Timeouts: Force socket timeouts to validate error handling and logging.

```mermaid
flowchart TD
Start(["Start WeCom Auth"]) --> GetToken["Call WeCom Token Endpoint"]
GetToken --> TokenOK{"Token Success?"}
TokenOK --> |Yes| StoreToken["Store Access Token"]
TokenOK --> |No| HandleError["Handle Auth Error"]
StoreToken --> End(["Auth Complete"])
HandleError --> End
```

**Diagram sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)

**Section sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [test_wecom_sdk_media.py](file://backend/tests/test_wecom_sdk_media.py)
- [smoke_wecom_get_chat_data.py](file://backend/scripts/smoke_wecom_get_chat_data.py)
- [smoke_wecom_sdk_init.py](file://backend/scripts/smoke_wecom_sdk_init.py)

### Qiniu Storage Mocking
- Upload/Download: Mock HTTP calls to Qiniu upload and download endpoints. Return success responses or simulate failures (e.g., 500 errors, network unreachable).
- Signed URLs: Mock signed URL generation and expiration behavior.
- Provider Factory: Inject mock providers to control upload/download outcomes deterministically.
- HTTPS Domain: Validate domain configuration and certificate handling by mocking TLS handshake.

```mermaid
classDiagram
class QiniuStorage {
+upload(file_path, key) bool
+download(key, dest_path) bool
+generate_signed_url(key, expires) string
}
class QiniuProviderFactory {
+create_provider() QiniuStorage
}
class MockQiniuStorage {
+upload(file_path, key) bool
+download(key, dest_path) bool
+generate_signed_url(key, expires) string
}
QiniuStorage <|-- MockQiniuStorage : "extends"
QiniuProviderFactory --> QiniuStorage : "creates"
```

**Diagram sources**
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [test_qiniu_provider_factory.py](file://backend/tests/test_qiniu_provider_factory.py)

**Section sources**
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [test_qiniu_storage.py](file://backend/tests/test_qiniu_storage.py)
- [test_qiniu_provider_factory.py](file://backend/tests/test_qiniu_provider_factory.py)
- [test_qiniu_https_domain.py](file://backend/tests/test_qiniu_https_domain.py)

### Media Download Orchestration
- Workflow: Fetch media from WeCom, store via Qiniu, generate thumbnails.
- Mocking Strategy: Mock WeCom media endpoints and Qiniu storage operations. Simulate partial downloads and retries.
- Error Handling: Test scenarios where WeCom returns errors or Qiniu upload fails.

```mermaid
sequenceDiagram
participant Test as "Test Case"
participant MD as "Media Download"
participant WSDK as "WeCom SDK"
participant QN as "Qiniu Storage"
participant TP as "Thumbnail Pipeline"
Test->>MD : "Initiate download"
MD->>WSDK : "Get media"
WSDK-->>MD : "Mocked media bytes"
MD->>QN : "Upload to Qiniu"
QN-->>MD : "Success/Failure"
alt Success
MD->>TP : "Generate thumbnail"
TP-->>MD : "Thumbnail ready"
MD-->>Test : "Complete"
else Failure
MD-->>Test : "Error logged"
end
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

**Section sources**
- [media_download.py](file://backend/app/media_download.py)
- [test_media_download.py](file://backend/tests/test_media_download.py)

### Thumbnail Pipeline
- Processing: Accepts downloaded media, generates thumbnails, stores results.
- Mocking: Mock image processing libraries and storage writes. Simulate large images and failure cases.

```mermaid
flowchart TD
Start(["Receive Media"]) --> Resize["Resize Image"]
Resize --> Format["Convert Format"]
Format --> Store["Store Thumbnail"]
Store --> End(["Done"])
```

**Diagram sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

**Section sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [test_thumbnail_pipeline.py](file://backend/tests/test_thumbnail_pipeline.py)

### Worker Processes
- Purpose: Background tasks for syncing, downloading, and processing media.
- Mocking: Simulate task queues and external service calls. Test idempotency and retry logic.

```mermaid
sequenceDiagram
participant Test as "Test Case"
participant Worker as "Worker Process"
participant Queue as "Task Queue"
participant WSDK as "WeCom SDK"
participant QN as "Qiniu Storage"
Test->>Queue : "Enqueue sync task"
Worker->>Queue : "Poll tasks"
Worker->>WSDK : "Fetch messages/media"
WSDK-->>Worker : "Mocked data"
Worker->>QN : "Upload processed files"
QN-->>Worker : "Success"
Worker-->>Test : "Task completed"
```

**Diagram sources**
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)

**Section sources**
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)

## Dependency Analysis
External dependencies include WeCom SDK and Qiniu storage. Tests isolate these by mocking HTTP clients and injecting test doubles.

```mermaid
graph TB
WSDK["WeCom SDK"] --> HTTP["HTTP Client"]
QN["Qiniu Storage"] --> HTTP
MD["Media Download"] --> WSDK
MD --> QN
TP["Thumbnail Pipeline"] --> QN
```

**Diagram sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

**Section sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

## Performance Considerations
- Avoid unnecessary I/O in tests by using fast mocks.
- Simulate high-latency scenarios to validate timeout handling.
- Test concurrency limits and retry mechanisms under load.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- Authentication Failures: Verify mocked token responses match expected formats.
- Network Errors: Ensure mocks simulate realistic error codes and messages.
- Rate Limiting: Confirm retry logic respects backoff strategies.
- Timeout Handling: Validate that timeouts are caught and logged appropriately.

**Section sources**
- [test_wecom_sdk_media.py](file://backend/tests/test_wecom_sdk_media.py)
- [test_qiniu_storage.py](file://backend/tests/test_qiniu_storage.py)
- [test_media_download.py](file://backend/tests/test_media_download.py)

## Conclusion
Effective mocking of WeCom SDK and Qiniu storage enables robust integration testing. By simulating API responses, handling errors, and validating workflows, tests ensure reliability and resilience in production environments.

[No sources needed since this section summarizes without analyzing specific files]