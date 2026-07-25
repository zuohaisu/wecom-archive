# Storage Abstraction Layer

<cite>
**Referenced Files in This Document**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_classification.py](file://backend/app/media_classification.py)
- [main.py](file://backend/app/main.py)
- [0005_media_storage_backend_reference.py](file://backend/alembic/versions/0005_media_storage_backend_reference.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0007_media_migration_metadata.py](file://backend/alembic/versions/0007_media_migration_metadata.py)
- [0012_media_thumbnails.py](file://backend/alembic/versions/0012_media_thumbnails.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)
- [backfill_thumbnails_once.py](file://backend/scripts/backfill_thumbnails_once.py)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [rnd_185_media_storage_abstraction.md](file://docs/research/rnd_185_media_storage_abstraction.md)
- [rnd_186_local_qiniu_migration.md](file://docs/research/rnd_186_local_qiniu_migration.md)
- [wecom_archive_media_download_runbook.md](file://docs/wecom_archive_media_download_runbook.md)
- [wecom_archive_worker_runbook.md](file://docs/wecom_archive_worker_runbook.md)
</cite>

## Update Summary
**Changes Made**
- Added dedicated MediaWorker service section for improved reliability and progress tracking
- Updated media download pipeline architecture to reflect the new worker-based approach
- Enhanced reliability and monitoring capabilities documentation
- Updated dependency analysis to include the new MediaWorker component

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
10. [Appendices](#appendices)

## Introduction
This document explains the storage abstraction layer that unifies media persistence across multiple backends, primarily local filesystem and Qiniu Cloud Object Storage (KODO). It covers the unified interface, backend factory pattern, enhanced media download pipeline with dedicated MediaWorker service for improved reliability and progress tracking, thumbnail generation workflow, classification system, access control, CDN integration, migration framework for moving media between backends, backup strategies, disaster recovery procedures, and performance optimizations such as caching, connection pooling, and concurrent operations.

## Project Structure
The storage abstraction is implemented under the backend application module with dedicated files for storage interfaces, backend implementations, worker services, pipelines, and scripts. Alembic migrations provide schema evolution for backend references, migration bookkeeping, metadata, and thumbnails. Operational runbooks and research documents describe design decisions and operational procedures.

```mermaid
graph TB
subgraph "App Module"
A["media_storage.py"]
B["qiniu_storage.py"]
C["media_download.py"]
D["services/media_worker.py"]
E["thumbnail_pipeline.py"]
F["media_thumbnails.py"]
G["media_classification.py"]
H["main.py"]
end
subgraph "Migrations"
M1["0005_media_storage_backend_reference.py"]
M2["0006_media_migration_bookkeeping.py"]
M3["0007_media_migration_metadata.py"]
M4["0012_media_thumbnails.py"]
end
subgraph "Scripts"
S1["migrate_local_media_to_qiniu.py"]
S2["backfill_thumbnails_once.py"]
end
A --> B
C --> A
D --> C
D --> A
E --> A
F --> A
G --> A
H --> A
S1 --> A
S1 --> B
S2 --> E
M1 --> A
M2 --> S1
M3 --> S1
M4 --> E
```

**Diagram sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_classification.py](file://backend/app/media_classification.py)
- [main.py](file://backend/app/main.py)
- [0005_media_storage_backend_reference.py](file://backend/alembic/versions/0005_media_storage_backend_reference.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0007_media_migration_metadata.py](file://backend/alembic/versions/0007_media_migration_metadata.py)
- [0012_media_thumbnails.py](file://backend/alembic/versions/0012_media_thumbnails.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)
- [backfill_thumbnails_once.py](file://backend/scripts/backfill_thumbnails_once.py)

**Section sources**
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [rnd_185_media_storage_abstraction.md](file://docs/research/rnd_185_media_storage_abstraction.md)

## Core Components
- Unified storage interface: Defines a consistent API for storing, retrieving, deleting, and generating signed URLs for media objects, abstracting backend specifics.
- Local filesystem backend: Implements the interface using the local disk, suitable for development or small-scale deployments.
- Qiniu Cloud backend: Implements the interface against Qiniu KODO, including signed URL generation and CDN domain support.
- Backend factory: Resolves the active storage backend based on configuration, enabling runtime selection without changing callers.
- **Enhanced Media Worker Service**: Dedicated service for reliable media processing with progress tracking, retry mechanisms, and error handling.
- Media download pipeline: Orchestrates fetching media from WeCom, classifying content, persisting via the storage interface, and triggering downstream processing through the MediaWorker.
- Thumbnail pipeline: Generates thumbnails for supported media types, persists them alongside originals, and updates metadata.
- Classification system: Determines media type and properties to guide storage paths, thumbnail generation, and access policies.
- Access control and CDN: Enforces tenant isolation and generates time-limited signed URLs; integrates with CDN domains for efficient delivery.

**Section sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_classification.py](file://backend/app/media_classification.py)
- [rnd_185_media_storage_abstraction.md](file://docs/research/rnd_185_media_storage_abstraction.md)

## Architecture Overview
The storage abstraction layer provides a single entry point for all media operations. Callers use the unified interface without knowing whether data resides locally or on Qiniu. The factory selects the backend at startup based on environment configuration. The enhanced architecture now includes a dedicated MediaWorker service that handles media processing tasks with improved reliability, progress tracking, and error recovery. Downstream components like download and thumbnail pipelines consume this interface uniformly while leveraging the worker service for robust processing.

```mermaid
classDiagram
class StorageInterface {
+store(media_id, bytes_or_stream, metadata)
+get(media_id) bytes_or_stream
+delete(media_id) bool
+signed_url(media_id, expires_seconds) string
+exists(media_id) bool
}
class LocalStorageBackend {
+store(media_id, bytes_or_stream, metadata)
+get(media_id) bytes_or_stream
+delete(media_id) bool
+signed_url(media_id, expires_seconds) string
+exists(media_id) bool
}
class QiniuStorageBackend {
+store(media_id, bytes_or_stream, metadata)
+get(media_id) bytes_or_stream
+delete(media_id) bool
+signed_url(media_id, expires_seconds) string
+exists(media_id) bool
}
class MediaWorker {
+process_media(media_id)
+track_progress(media_id)
+handle_errors(media_id, error)
+retry_failed_jobs()
}
class StorageFactory {
+resolve() StorageInterface
}
StorageInterface <|.. LocalStorageBackend
StorageInterface <|.. QiniuStorageBackend
StorageFactory --> StorageInterface : "returns"
MediaWorker --> StorageInterface : "uses"
```

**Diagram sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_worker.py](file://backend/app/services/media_worker.py)

**Section sources**
- [rnd_185_media_storage_abstraction.md](file://docs/research/rnd_185_media_storage_abstraction.md)

## Detailed Component Analysis

### Unified Storage Interface and Factory
- Interface contract: Methods for store/get/delete/signed_url/exists ensure consistent behavior across backends.
- Factory resolution: Reads configuration to instantiate either LocalStorageBackend or QiniuStorageBackend.
- Tenant scoping: Paths and keys incorporate tenant identifiers to isolate media per tenant.
- Error handling: Normalizes backend-specific errors into common exceptions for upstream handling.

```mermaid
sequenceDiagram
participant Caller as "Caller"
participant Factory as "StorageFactory"
participant Backend as "StorageInterface"
participant Disk as "Local FS"
participant Qiniu as "Qiniu KODO"
Caller->>Factory : resolve()
alt Local backend configured
Factory-->>Caller : LocalStorageBackend
Caller->>Disk : store/get/delete/signed_url
else Qiniu backend configured
Factory-->>Caller : QiniuStorageBackend
Caller->>Qiniu : store/get/delete/signed_url
end
```

**Diagram sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

**Section sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

### Enhanced Media Worker Service
**Updated** The media download pipeline now leverages a dedicated MediaWorker service that provides improved reliability, progress tracking, and error handling capabilities.

- **Reliable Processing**: Implements retry mechanisms with exponential backoff for failed operations
- **Progress Tracking**: Monitors and reports download/upload progress for long-running operations
- **Error Recovery**: Automatically recovers from transient failures and network interruptions
- **Resource Management**: Manages concurrent operations and resource allocation efficiently
- **Health Monitoring**: Provides status endpoints and metrics for operational visibility

```mermaid
flowchart TD
Start(["Media Task Received"]) --> Validate["Validate task parameters"]
Validate --> Process{"Process task"}
Process --> |Success| TrackProgress["Track progress"]
Process --> |Failure| HandleError["Handle error with retry logic"]
TrackProgress --> Complete{"Task complete?"}
Complete --> |No| Continue["Continue processing"]
Complete --> |Yes| Finalize["Finalize and cleanup"]
HandleError --> Retry{"Retry available?"}
Retry --> |Yes| Backoff["Apply exponential backoff"]
Backoff --> Process
Retry --> |No| Fail["Mark task as failed"]
Continue --> Process
Finalize --> End(["Task Complete"])
Fail --> End
```

**Diagram sources**
- [media_worker.py](file://backend/app/services/media_worker.py)
- [media_download.py](file://backend/app/media_download.py)

**Section sources**
- [media_worker.py](file://backend/app/services/media_worker.py)
- [media_download.py](file://backend/app/media_download.py)

### Media Download Pipeline
**Updated** The media download pipeline now integrates with the MediaWorker service for enhanced reliability and progress tracking.

- Ingestion: Receives media payloads from WeCom events or scheduled syncs.
- Classification: Detects media type and attributes to determine storage path and thumbnail needs.
- **Worker Integration**: Delegates processing to MediaWorker for reliable execution with progress tracking.
- Persistence: Uses the storage interface to write media with metadata.
- Post-processing: Triggers thumbnail generation when applicable through the worker service.

```mermaid
flowchart TD
Start(["Start"]) --> Receive["Receive media payload"]
Receive --> Classify["Classify media type"]
Classify --> Queue["Queue to MediaWorker"]
Queue --> WorkerProcess["MediaWorker processes task"]
WorkerProcess --> Persist{"Persist success?"}
Persist --> |No| WorkerRetry["Worker retry mechanism"]
WorkerRetry --> Persist
Persist --> |Yes| ThumbnailsNeeded{"Thumbnails needed?"}
ThumbnailsNeeded --> |No| Complete["Complete with progress update"]
ThumbnailsNeeded --> |Yes| QueueThumb["Queue thumbnail job via worker"]
QueueThumb --> Complete
```

**Diagram sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [media_classification.py](file://backend/app/media_classification.py)

**Section sources**
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [media_classification.py](file://backend/app/media_classification.py)

### Thumbnail Generation Workflow
- Trigger: After successful media persistence, thumbnails are generated for supported formats.
- Processing: Converts original media to standardized thumbnail sizes and formats.
- Storage: Persists thumbnails via the same storage interface, co-located with originals.
- Metadata update: Records thumbnail availability and dimensions for fast serving.

```mermaid
sequenceDiagram
participant DL as "Download Pipeline"
participant Worker as "MediaWorker"
participant Thumb as "Thumbnail Pipeline"
participant Store as "StorageInterface"
participant DB as "Metadata Store"
DL->>Store : store(original)
Store-->>DL : ok
DL->>Worker : enqueue_thumbnail(original_id)
Worker->>Thumb : process_thumbnail(original_id)
Thumb->>Store : get(original)
Thumb->>Thumb : generate(thumbnail variants)
Thumb->>Store : store(thumbnails)
Thumb->>DB : update(metadata with thumbnails)
Thumb-->>Worker : done
Worker-->>DL : completion notification
```

**Diagram sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_worker.py](file://backend/app/services/media_worker.py)

**Section sources**
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)

### Media Classification System
- Type detection: Analyzes headers, magic numbers, and file extensions to classify media.
- Attributes: Extracts size, MIME type, and capabilities (e.g., image/video/audio).
- Policy mapping: Maps classification results to storage paths and thumbnail rules.

```mermaid
flowchart TD
Input["Raw bytes/stream"] --> Detect["Detect MIME/type"]
Detect --> Validate{"Valid media?"}
Validate --> |No| Reject["Reject and log"]
Validate --> |Yes| Map["Map to storage path<br/>and thumbnail policy"]
Map --> Output["Classification result"]
```

**Diagram sources**
- [media_classification.py](file://backend/app/media_classification.py)

**Section sources**
- [media_classification.py](file://backend/app/media_classification.py)

### Access Control and CDN Integration
- Signed URLs: Time-limited, cryptographically signed links prevent unauthorized access.
- Tenant isolation: Keys include tenant context to enforce isolation.
- CDN domains: Configurable CDN endpoints serve signed URLs efficiently.
- Cache headers: Appropriate cache-control directives balance freshness and bandwidth.

```mermaid
sequenceDiagram
participant Client as "Client"
participant API as "API Server"
participant Store as "StorageInterface"
participant CDN as "CDN/Qiniu Domain"
Client->>API : GET /media/{id}
API->>Store : signed_url(id, expires)
Store-->>API : signed_url
API-->>Client : 302 Redirect to signed_url
Client->>CDN : Fetch media via signed_url
CDN-->>Client : Stream media
```

**Diagram sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

**Section sources**
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)

### Migration Framework for Moving Media Between Backends
- Schema support: Migrations add backend reference fields, migration bookkeeping, and metadata tracking.
- Script-driven migration: Batch process moves media from local to Qiniu while preserving metadata and consistency.
- Idempotency: Bookkeeping ensures safe retries and rollback points.
- Validation: Post-migration verification checks completeness and integrity.

```mermaid
flowchart TD
Start(["Start Migration"]) --> ReadPlan["Read migration plan"]
ReadPlan --> Iterate{"Next item exists?"}
Iterate --> |No| Verify["Run verification"]
Iterate --> |Yes| Copy["Copy to target backend"]
Copy --> UpdateMeta["Update metadata and bookkeeping"]
UpdateMeta --> Iterate
Verify --> Report["Generate report"]
Report --> End(["End"])
```

**Diagram sources**
- [0005_media_storage_backend_reference.py](file://backend/alembic/versions/0005_media_storage_backend_reference.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0007_media_migration_metadata.py](file://backend/alembic/versions/0007_media_migration_metadata.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)

**Section sources**
- [0005_media_storage_backend_reference.py](file://backend/alembic/versions/0005_media_storage_backend_reference.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0007_media_migration_metadata.py](file://backend/alembic/versions/0007_media_migration_metadata.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)
- [rnd_186_local_qiniu_migration.md](file://docs/research/rnd_186_local_qiniu_migration.md)

### Backup Strategies and Disaster Recovery
- Backups: Periodic snapshots of local storage and object listings for cloud backends; metadata backups to database.
- Restore: Rebuild indexes and verify checksums; re-download missing items if necessary.
- DR procedures: Failover between backends by switching configuration; validate signed URLs and CDN routing.
- Runbooks: Step-by-step guides for incident response and recovery.

**Section sources**
- [wecom_archive_media_download_runbook.md](file://docs/wecom_archive_media_download_runbook.md)
- [wecom_archive_worker_runbook.md](file://docs/wecom_archive_worker_runbook.md)

## Dependency Analysis
**Updated** The storage layer now includes the MediaWorker service as a central component for reliable media processing, integrating with configuration for backend selection, WeCom SDK for ingestion, database for metadata, and CDN for delivery. Migrations evolve schema to support backend references and migration bookkeeping.

```mermaid
graph TB
Main["main.py"] --> Storage["media_storage.py"]
Storage --> Local["Local FS"]
Storage --> Qiniu["qiniu_storage.py"]
Download["media_download.py"] --> Storage
Download --> Worker["services/media_worker.py"]
Worker --> Storage
Thumb["thumbnail_pipeline.py"] --> Storage
Meta["media_thumbnails.py"] --> Storage
Classify["media_classification.py"] --> Storage
MigScript["migrate_local_media_to_qiniu.py"] --> Storage
MigScript --> Qiniu
DB["Database"] --> MigBook["migration bookkeeping"]
DB --> ThumbMeta["thumbnail metadata"]
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_classification.py](file://backend/app/media_classification.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0012_media_thumbnails.py](file://backend/alembic/versions/0012_media_thumbnails.py)

**Section sources**
- [main.py](file://backend/app/main.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [media_download.py](file://backend/app/media_download.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [media_classification.py](file://backend/app/media_classification.py)
- [migrate_local_media_to_qiniu.py](file://backend/scripts/migrate_local_media_to_qiniu.py)
- [0006_media_migration_bookkeeping.py](file://backend/alembic/versions/0006_media_migration_bookkeeping.py)
- [0012_media_thumbnails.py](file://backend/alembic/versions/0012_media_thumbnails.py)

## Performance Considerations
- Caching: Use in-process caches for frequently accessed metadata and signed URL results; leverage CDN edge caching for media.
- Connection pooling: Configure HTTP clients and storage SDKs with pooled connections to reduce latency and resource usage.
- Concurrent operations: Parallelize uploads/downloads where safe; throttle concurrency to avoid overwhelming backends.
- Streaming: Stream large media to minimize memory footprint during upload/download and thumbnail generation.
- Chunked transfers: For large objects, implement chunked uploads and resumable downloads.
- Thumbnail optimization: Generate only necessary sizes and formats; cache thumbnails aggressively.
- **Worker Optimization**: Leverage MediaWorker's built-in concurrency controls and resource management for optimal throughput.
- **Progress Tracking**: Utilize worker progress tracking for better user experience and monitoring.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
- Signed URL failures: Validate expiration windows, signing keys, and CDN domain configuration.
- Backend connectivity: Check network reachability, credentials, and quotas for Qiniu; verify local filesystem permissions.
- Migration issues: Inspect bookkeeping tables for partial progress; rerun idempotent steps; verify checksums post-copy.
- Thumbnail generation: Confirm input format support and output constraints; review worker logs for conversion errors.
- Access control: Ensure tenant scoping in keys and correct ACL settings; test signed URL retrieval with different tenants.
- **Worker Issues**: Monitor MediaWorker health endpoints, check retry queues, and review error logs for processing failures.
- **Progress Tracking**: Investigate stuck tasks by examining worker progress logs and queue status.

**Section sources**
- [wecom_archive_media_download_runbook.md](file://docs/wecom_archive_media_download_runbook.md)
- [wecom_archive_worker_runbook.md](file://docs/wecom_archive_worker_runbook.md)

## Conclusion
The storage abstraction layer delivers a robust, extensible foundation for media management across local and cloud backends. By standardizing operations through a unified interface and factory pattern, it simplifies integration, enables seamless migration, and supports scalable delivery via CDN. The enhanced architecture with the dedicated MediaWorker service provides improved reliability, progress tracking, and error recovery capabilities. Combined with strong access control, comprehensive migration tooling, and operational runbooks, it provides a reliable platform for enterprise-grade media archival and retrieval.

## Appendices
- Configuration examples: Backend selection, CDN domains, and timeout/pooling parameters.
- Migration runbook: Step-by-step instructions for moving media between backends safely.
- Operational checklists: Pre/post migration validations, backup schedules, and DR drills.
- **Worker Configuration**: MediaWorker setup, scaling parameters, and monitoring configuration.

[No sources needed since this section provides general guidance]