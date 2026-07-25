# Media Download Pipeline

<cite>
**Referenced Files in This Document**
- [media_download.py](file://backend/app/media_download.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [API.md](file://docs/API.md)
- [test_media_download.py](file://backend/tests/test_media_download.py)
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
</cite>

## Update Summary
**Changes Made**
- Updated architecture overview to reflect the separation of media download pipeline from conversations router to dedicated media access service
- Added new section documenting the media access service architecture
- Updated component relationships to show the new service-based structure
- Enhanced dependency analysis to reflect the decoupled architecture between conversation handling and media operations

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Media Access Service Architecture](#media-access-service-architecture)
6. [Detailed Component Analysis](#detailed-component-analysis)
7. [Dependency Analysis](#dependency-analysis)
8. [Performance Considerations](#performance-considerations)
9. [Troubleshooting Guide](#troubleshooting-guide)
10. [Conclusion](#conclusion)

## Introduction
This document explains the media download pipeline for WeCom (Enterprise WeChat) content, focusing on how media is retrieved from WeCom, queued and processed concurrently, validated for integrity, stored across backends, and monitored with progress tracking and retry/backoff strategies. The pipeline has been restructured into a dedicated media access service that operates independently from conversation handling, providing better separation of concerns and improved scalability. It covers authentication to WeCom, rate limiting, error recovery, storage integration, and end-to-end data flow from initial download to final placement.

## Project Structure
The media download pipeline spans application modules, services, scripts, and deployment units:
- Application modules implement the download orchestration, storage abstraction, and thumbnail generation.
- A dedicated media access service provides the core functionality for media operations.
- A dedicated script drives one-shot downloads for historical or manual runs.
- Systemd units schedule and manage long-running workers.
- Documentation provides architectural context and API contracts.

```mermaid
graph TB
subgraph "App Modules"
MD["media_download.py"]
MS["media_storage.py"]
QS["qiniu_storage.py"]
TP["thumbnail_pipeline.py"]
SDK["sdk/wecom_sdk.py"]
end
subgraph "Services"
MAS["services/media_access.py"]
end
subgraph "Routers"
CR["routers/conversations.py"]
end
subgraph "Scripts"
DWO["scripts/download_wecom_media_once.py"]
end
subgraph "Deployment"
Svc["systemd service<br/>wecom-archive-media-download.service"]
end
subgraph "Docs"
Arch["docs/ARCHITECTURE.md"]
Api["docs/API.md"]
end
DWO --> MD
MD --> SDK
MD --> MS
MS --> QS
MD --> TP
MAS --> MD
CR -.-> MAS
Svc --> DWO
Arch -.-> MD
Api -.-> MD
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [API.md](file://docs/API.md)

**Section sources**
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [API.md](file://docs/API.md)

## Core Components
- WeCom SDK client: encapsulates authentication, token management, and API calls to retrieve media metadata and streams.
- Media download orchestrator: manages queueing, concurrency, retries, backoff, progress tracking, and validation.
- Media access service: provides a dedicated interface for media operations, decoupling from conversation handling.
- Storage abstraction: defines a common interface for storing media bytes and metadata; concrete implementations include local filesystem and Qiniu Kodo.
- Thumbnail pipeline: generates thumbnails post-download and integrates with storage backends.
- One-shot downloader script: triggers batch or targeted downloads via the orchestrator.
- Systemd unit: ensures the worker process runs reliably and restarts on failure.

Key responsibilities:
- Authentication and token refresh for WeCom APIs.
- Rate limiting and throttling to respect provider limits.
- Retry with exponential backoff and jitter for transient failures.
- Integrity checks (size/hash/content-type) and corruption handling.
- Progress reporting and observability hooks.
- Pluggable storage backends with consistent semantics.
- Service-oriented architecture for media operations.

**Section sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)

## Architecture Overview
The pipeline follows a producer-consumer model with service-oriented architecture:
- Producers enqueue media tasks derived from WeCom conversations/messages.
- Consumers pull tasks, authenticate with WeCom, download media, validate, store, and generate thumbnails.
- The media access service provides a clean interface for media operations, separate from conversation routing.
- Observability tracks per-task progress and aggregate metrics.
- Failure paths trigger retries with backoff and notifications upon exhaustion.

```mermaid
sequenceDiagram
participant CLI as "One-shot Script"
participant Orchestrator as "Media Download Orchestrator"
participant Queue as "Download Queue"
participant Worker as "Worker Process"
participant SDK as "WeCom SDK Client"
participant Store as "Storage Backend"
participant Thumb as "Thumbnail Pipeline"
participant MAS as "Media Access Service"
participant CR as "Conversations Router"
CR->>MAS : "Request media operation"
MAS->>Orchestrator : "Delegate to orchestrator"
Orchestrator->>Queue : "Enqueue media tasks"
Worker->>Queue : "Dequeue task"
Worker->>SDK : "Authenticate and fetch media"
SDK-->>Worker : "Stream or bytes"
Worker->>Worker : "Validate size/type/hash"
alt Valid
Worker->>Store : "Persist media"
Store-->>Worker : "Success"
Worker->>Thumb : "Generate thumbnail"
Thumb-->>Worker : "Done"
Worker-->>Orchestrator : "Progress update"
else Invalid or Error
Worker->>Worker : "Retry with backoff"
alt Max retries exceeded
Worker->>Orchestrator : "Failure notification"
end
end
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)

## Media Access Service Architecture
The media access service provides a dedicated interface for all media-related operations, separating these concerns from conversation handling:

### Service Responsibilities
- **Media Retrieval**: Coordinates the download process from WeCom to storage backends
- **Task Management**: Handles queuing, scheduling, and concurrent processing of media tasks
- **Error Handling**: Implements retry logic, backoff strategies, and failure notifications
- **Progress Tracking**: Monitors download progress and provides status updates
- **Validation**: Ensures media integrity through size, hash, and content-type verification

### Integration Points
- **Conversations Router**: Delegates media operations to the media access service
- **Storage Backends**: Abstracts different storage implementations (local, Qiniu)
- **WeCom SDK**: Provides authentication and API access for media retrieval
- **Thumbnail Pipeline**: Integrates thumbnail generation after successful downloads

```mermaid
classDiagram
class MediaAccessService {
+download_media(media_id, tenant_id)
+get_media_status(task_id)
+cancel_download(task_id)
+list_pending_tasks()
+retry_failed_tasks()
}
class ConversationsRouter {
+handle_conversation_request()
+delegate_to_media_service()
}
class MediaDownloadOrchestrator {
+enqueue_task(task)
+process_queue()
+track_progress()
+handle_errors()
}
MediaAccessService --> MediaDownloadOrchestrator
ConversationsRouter --> MediaAccessService
```

**Diagram sources**
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [media_download.py](file://backend/app/media_download.py)

**Section sources**
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)

## Detailed Component Analysis

### WeCom SDK Client
Responsibilities:
- Obtain and cache access tokens using tenant credentials.
- Call WeCom APIs to list messages, resolve media IDs, and stream media content.
- Enforce rate limits and handle HTTP errors with appropriate exceptions.

```mermaid
classDiagram
class WeComClient {
+authenticate()
+get_message_list(params)
+resolve_media_id(media_id)
+stream_media(media_id, dest)
+handle_rate_limit(response)
}
```

**Diagram sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)

**Section sources**
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)

### Media Download Orchestrator
Responsibilities:
- Build and manage a concurrent download queue.
- Configure retry policies, exponential backoff, and jitter.
- Track per-task progress and aggregate status.
- Validate downloaded content before persisting.
- Integrate with storage backends and thumbnail pipeline.

```mermaid
flowchart TD
Start(["Task Enqueued"]) --> Auth["Authenticate to WeCom"]
Auth --> Fetch["Fetch Media Stream"]
Fetch --> Validate{"Validation Pass?"}
Validate --> |No| Backoff["Exponential Backoff + Jitter"]
Backoff --> RetryCheck{"Max Retries?"}
RetryCheck --> |Yes| Notify["Failure Notification"]
RetryCheck --> |No| Fetch
Validate --> |Yes| Store["Persist to Storage"]
Store --> Thumb["Generate Thumbnails"]
Thumb --> Done(["Complete"])
Notify --> End(["Exit"])
Done --> End
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)

**Section sources**
- [media_download.py](file://backend/app/media_download.py)

### Storage Abstraction and Qiniu Backend
Responsibilities:
- Define a unified interface for storing media bytes and associated metadata.
- Implement concrete backends: local filesystem and Qiniu Kodo.
- Provide idempotent put operations and verification helpers.

```mermaid
classDiagram
class StorageBackend {
<<interface>>
+put(data, metadata)
+get(key)
+exists(key) bool
+delete(key)
}
class LocalStorage {
+put(data, metadata)
+get(key)
+exists(key) bool
+delete(key)
}
class QiniuStorage {
+put(data, metadata)
+get(key)
+exists(key) bool
+delete(key)
}
StorageBackend <|-- LocalStorage
StorageBackend <|-- QiniuStorage
```

**Diagram sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

**Section sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

### Thumbnail Pipeline
Responsibilities:
- Trigger thumbnail generation after successful media storage.
- Handle different media types and formats.
- Persist thumbnails alongside original media metadata.

```mermaid
sequenceDiagram
participant Orchestrator as "Orchestrator"
participant Thumb as "Thumbnail Pipeline"
participant Store as "Storage Backend"
Orchestrator->>Thumb : "Request thumbnail for media_key"
Thumb->>Store : "Read source media"
Store-->>Thumb : "Bytes"
Thumb->>Thumb : "Decode and resize"
Thumb->>Store : "Write thumbnail"
Store-->>Thumb : "OK"
Thumb-->>Orchestrator : "Thumbnail ready"
```

**Diagram sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_storage.py](file://backend/app/media_storage.py)

**Section sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)

### One-shot Downloader Script
Responsibilities:
- Parse arguments for target tenants, conversations, or message ranges.
- Initialize the orchestrator and run a bounded number of concurrent workers.
- Report progress and exit codes for automation and monitoring.

```mermaid
sequenceDiagram
participant User as "Operator"
participant Script as "download_wecom_media_once.py"
participant Orchestrator as "Media Download Orchestrator"
participant Queue as "Download Queue"
User->>Script : "Run with filters"
Script->>Orchestrator : "Initialize and configure"
Orchestrator->>Queue : "Enqueue filtered tasks"
Orchestrator-->>Script : "Progress updates"
Script-->>User : "Summary and exit code"
```

**Diagram sources**
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)
- [media_download.py](file://backend/app/media_download.py)

**Section sources**
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)

### Systemd Service Integration
Responsibilities:
- Manage lifecycle of the media download worker process.
- Ensure automatic restart on crashes and controlled shutdown.
- Provide environment configuration for backend credentials and concurrency.

```mermaid
flowchart TD
Boot["System Boot"] --> Unit["Load systemd unit"]
Unit --> Exec["Start worker process"]
Exec --> Run["Run download loop"]
Run --> Health{"Healthy?"}
Health --> |Yes| Run
Health --> |No| Restart["Restart per policy"]
Restart --> Run
```

**Diagram sources**
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)

**Section sources**
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)

## Dependency Analysis
The pipeline exhibits clear separation of concerns with service-oriented architecture:
- The orchestrator depends on the WeCom SDK for retrieval and on the storage abstraction for persistence.
- The media access service provides a clean interface between conversation routing and media operations.
- The storage abstraction decouples implementation details from the orchestrator.
- The thumbnail pipeline depends on storage and image processing capabilities.
- The one-shot script depends on the orchestrator for execution.

```mermaid
graph LR
SDK["WeCom SDK"] --> Orchestrator["Media Download Orchestrator"]
Orchestrator --> Storage["Storage Abstraction"]
Storage --> Local["Local Storage"]
Storage --> Qiniu["Qiniu Storage"]
Orchestrator --> Thumb["Thumbnail Pipeline"]
Script["One-shot Script"] --> Orchestrator
MAS["Media Access Service"] --> Orchestrator
CR["Conversations Router"] --> MAS
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)

**Section sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_access.py](file://backend/app/services/media_access.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [download_wecom_media_once.py](file://backend/scripts/download_wecom_media_once.py)

## Performance Considerations
- Concurrency tuning: adjust worker count based on CPU, I/O, and WeCom rate limits.
- Streaming downloads: prefer streaming over buffering to reduce memory usage.
- Batched operations: group small writes where supported by storage backends.
- Caching: cache WeCom tokens and frequently accessed metadata to reduce latency.
- Backoff strategy: use exponential backoff with jitter to avoid thundering herds.
- Validation early: perform quick checks (content-type, size bounds) before full processing.
- Service isolation: dedicate resources to media operations for better performance isolation.

## Troubleshooting Guide
Common issues and resolutions:
- Authentication failures: verify tenant credentials and token expiration handling.
- Rate limit errors: increase backoff intervals and reduce concurrency.
- Network timeouts: enable retries with jitter and check upstream connectivity.
- Integrity mismatches: re-download and compare hashes; mark corrupted entries for review.
- Storage write failures: inspect backend permissions, quotas, and network reachability.
- Thumbnail generation errors: validate input formats and resource availability.
- Service communication issues: check inter-service communication and health endpoints.

Operational tips:
- Use the one-shot script with dry-run flags to validate configurations.
- Monitor system logs for worker health and retry patterns.
- Inspect storage backend dashboards for upload success rates.
- Check media access service health and status endpoints.
- Monitor conversation router to media service communication.

**Section sources**
- [test_media_download.py](file://backend/tests/test_media_download.py)
- [test_qiniu_worker_integration.py](file://backend/tests/test_qiniu_worker_integration.py)

## Conclusion
The media download pipeline provides a robust, extensible framework for retrieving WeCom media, enforcing reliability through retries and backoff, ensuring integrity via validation, and supporting multiple storage backends. The migration to a dedicated media access service improves separation of concerns, enhances scalability, and provides better operational visibility. Its modular design enables easy extension and maintenance while offering strong operational control and service-oriented architecture benefits.