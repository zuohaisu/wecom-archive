# Architecture

<cite>
**Referenced Files in This Document**
- [main.py](file://backend/app/main.py)
- [models.py](file://backend/app/db/models.py)
- [wecom_sdk.py](file://backend/app/sdk/wecom_sdk.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [qiniu_storage.py](file://backend/app/qiniu_storage.py)
- [thumbnail_pipeline.py](file://backend/app/thumbnail_pipeline.py)
- [auth.py](file://backend/app/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)
- [decrypt_worker.py](file://backend/app/services/decrypt_worker.py)
- [media_worker.py](file://backend/app/services/media_worker.py)
- [sync_worker.py](file://backend/app/services/sync_worker.py)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [DATA_MODEL.md](file://docs/DATA_MODEL.md)
- [alembic.ini](file://backend/alembic.ini)
</cite>

## Update Summary
**Changes Made**
- Added comprehensive worker service architecture section documenting DecryptWorker, MediaWorker, and SyncWorker services
- Updated microservices architecture pattern to include dedicated worker services for tenant-scoped operations
- Enhanced event-driven message processing with structured message processing pipeline
- Added new diagrams illustrating worker service coordination and tenant isolation patterns
- Updated dependency analysis to include worker service infrastructure

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Worker Service Architecture](#worker-service-architecture)
5. [Architecture Overview](#architecture-overview)
6. [Detailed Component Analysis](#detailed-component-analysis)
7. [Dependency Analysis](#dependency-analysis)
8. [Performance Considerations](#performance-considerations)
9. [Troubleshooting Guide](#troubleshooting-guide)
10. [Conclusion](#conclusion)

## Introduction

WeCom Archive 365 is a comprehensive enterprise messaging archive system designed to integrate with WeCom (Enterprise WeChat) APIs for message archiving, storage, and retrieval. The system implements a microservices architecture with event-driven message processing and multi-tenant isolation to support multiple organizations securely. It provides a complete solution for compliance, audit, and review of corporate communications through WeCom's enterprise messaging platform.

The system handles the entire lifecycle of WeCom messages from ingestion through processing, storage, thumbnail generation, and web-based retrieval, while maintaining strict tenant isolation and supporting multiple storage backends including local filesystem and Qiniu Cloud storage.

## Project Structure

The WeCom Archive 365 system follows a modular Python application structure with clear separation of concerns:

```mermaid
graph TB
subgraph "Backend Application"
main[main.py]
routers[routers/]
sdk[sdk/]
db[db/]
web[web/]
services[services/]
end
subgraph "Database Layer"
models[Models]
migrations[Alembic Migrations]
session[Session Management]
end
subgraph "Storage Layer"
media_storage[Media Storage]
qiniu[Qiniu Provider]
thumbnails[Thumbnail Pipeline]
end
subgraph "Worker Services"
decrypt_worker[DecryptWorker]
media_worker[MediaWorker]
sync_worker[SyncWorker]
end
subgraph "External Services"
wecom[WeCom API]
qiniu_cloud[Qiniu Cloud]
database[(PostgreSQL)]
end
main --> routers
main --> sdk
main --> db
main --> web
routers --> services
services --> decrypt_worker
services --> media_worker
services --> sync_worker
sdk --> wecom
services --> media_storage
media_storage --> qiniu
media_storage --> database
thumbnails --> media_storage
```

**Diagram sources**
- [main.py:1-50](file://backend/app/main.py#L1-L50)
- [models.py:1-100](file://backend/app/db/models.py#L1-L100)
- [decrypt_worker.py:1-100](file://backend/app/services/decrypt_worker.py#L1-L100)
- [media_worker.py:1-100](file://backend/app/services/media_worker.py#L1-L100)
- [sync_worker.py:1-100](file://backend/app/services/sync_worker.py#L1-L100)

**Section sources**
- [main.py:1-100](file://backend/app/main.py#L1-L100)
- [ARCHITECTURE.md:1-200](file://docs/ARCHITECTURE.md#L1-L200)

## Core Components

### Microservices Architecture Pattern

The system implements a microservices architecture with clear service boundaries:

- **API Gateway**: FastAPI-based HTTP server handling all external requests
- **Message Ingestion Service**: Processes incoming WeCom events and messages
- **Storage Abstraction Service**: Manages data persistence across multiple backends
- **Thumbnail Generation Service**: Asynchronous media processing pipeline
- **Web Application Service**: Serves the user interface and management console
- **Worker Services**: Dedicated background services for specialized tasks

### Event-Driven Message Processing

The system uses an event-driven architecture for handling WeCom messages:

```mermaid
sequenceDiagram
participant Client as "WeCom API"
participant Gateway as "API Gateway"
participant Processor as "Message Processor"
participant Queue as "Message Queue"
participant DecryptWorker as "DecryptWorker"
participant MediaWorker as "MediaWorker"
participant SyncWorker as "SyncWorker"
participant Storage as "Storage Backend"
Client->>Gateway : POST /api/wecom/events
Gateway->>Processor : Validate & Parse Event
Processor->>Queue : Enqueue Message
Queue-->>DecryptWorker : Dequeue Decryption Task
DecryptWorker->>Storage : Store Decrypted Message
Queue-->>MediaWorker : Dequeue Media Task
MediaWorker->>Storage : Download & Process Media
Queue-->>SyncWorker : Dequeue Sync Task
SyncWorker->>Storage : Sync Contact Data
Storage-->>Client : Acknowledge Processing
```

**Diagram sources**
- [wecom_events.py:1-150](file://backend/app/routers/wecom_events.py#L1-L150)
- [thumbnail_pipeline.py:1-200](file://backend/app/thumbnail_pipeline.py#L1-L200)
- [decrypt_worker.py:1-150](file://backend/app/services/decrypt_worker.py#L1-L150)
- [media_worker.py:1-150](file://backend/app/services/media_worker.py#L1-L150)
- [sync_worker.py:1-150](file://backend/app/services/sync_worker.py#L1-L150)

### Multi-Tenant Isolation

The system implements strict tenant isolation at multiple levels:

- **Database Level**: Tenant-scoped queries and data partitioning
- **Storage Level**: Tenant-specific media directories and access controls
- **API Level**: Tenant context validation and authorization
- **Cache Level**: Tenant-isolated caching strategies
- **Worker Level**: Tenant-scoped worker operations and resource isolation

**Section sources**
- [models.py:1-300](file://backend/app/db/models.py#L1-L300)
- [auth.py:1-200](file://backend/app/auth.py#L1-L200)

## Worker Service Architecture

The WeCom Archive 365 system implements a comprehensive worker service architecture that handles specialized background tasks with tenant-scoped operations and structured message processing.

### Worker Service Components

The worker service architecture consists of three primary worker types, each responsible for specific processing tasks:

```mermaid
classDiagram
class DecryptWorker {
+string tenant_id
+process_message(message) bool
+decrypt_content(content) string
+validate_tenant_access() bool
+handle_encrypted_data(data) dict
}
class MediaWorker {
+string tenant_id
+download_media(media_id) bytes
+generate_thumbnails(file_path) list
+upload_to_storage(file_path, key) string
+process_media_types(media_type) bool
}
class SyncWorker {
+string tenant_id
+sync_contacts() list
+update_employee_info(user_id) Employee
+sync_conversation_members(conversation_id) list
+maintain_sync_state() void
}
class WorkerCoordinator {
+initialize_workers() void
+route_task(task_type, payload) bool
+monitor_worker_health() bool
+scale_workers(worker_count) void
}
DecryptWorker --> WorkerCoordinator : "registered with"
MediaWorker --> WorkerCoordinator : "registered with"
SyncWorker --> WorkerCoordinator : "registered with"
```

**Diagram sources**
- [decrypt_worker.py:1-200](file://backend/app/services/decrypt_worker.py#L1-L200)
- [media_worker.py:1-200](file://backend/app/services/media_worker.py#L1-L200)
- [sync_worker.py:1-200](file://backend/app/services/sync_worker.py#L1-L200)

### Tenant-Scoped Operations

Each worker service implements tenant-scoped operations to ensure data isolation and security:

```mermaid
flowchart TD
Task["Incoming Worker Task"] --> ValidateTenant["Validate Tenant Context"]
ValidateTenant --> CheckAccess{"Tenant Has Access?"}
CheckAccess --> |No| Reject["Reject Task - Unauthorized"]
CheckAccess --> |Yes| RouteToWorker["Route to Appropriate Worker"]
RouteToWorker --> DecryptWorker{"Is Decryption Task?"}
RouteToWorker --> MediaWorker{"Is Media Processing Task?"}
RouteToWorker --> SyncWorker{"Is Sync Task?"}
DecryptWorker --> |Yes| DecryptProcess["Decrypt Content"]
MediaWorker --> |Yes| MediaProcess["Process Media Files"]
SyncWorker --> |Yes| SyncProcess["Synchronize Data"]
DecryptProcess --> StoreResult["Store Results"]
MediaProcess --> StoreResult
SyncProcess --> StoreResult
StoreResult --> Complete["Task Complete"]
Reject --> End([End])
Complete --> End
```

**Diagram sources**
- [decrypt_worker.py:1-150](file://backend/app/services/decrypt_worker.py#L1-L150)
- [media_worker.py:1-150](file://backend/app/services/media_worker.py#L1-L150)
- [sync_worker.py:1-150](file://backend/app/services/sync_worker.py#L1-L150)

### Structured Message Processing

The worker services implement structured message processing for handling different message types and content formats:

```mermaid
sequenceDiagram
participant Queue as "Message Queue"
participant Router as "Task Router"
participant DecryptWorker as "DecryptWorker"
participant MediaWorker as "MediaWorker"
participant SyncWorker as "SyncWorker"
participant Storage as "Storage Backend"
Queue->>Router : Enqueue Task
Router->>Router : Analyze Task Type
Router->>DecryptWorker : Decrypt Text Messages
Router->>MediaWorker : Process Media Files
Router->>SyncWorker : Sync Contact Data
DecryptWorker->>Storage : Store Decrypted Content
MediaWorker->>Storage : Upload Processed Media
SyncWorker->>Storage : Update Contact Records
DecryptWorker-->>Queue : Task Complete
MediaWorker-->>Queue : Task Complete
SyncWorker-->>Queue : Task Complete
```

**Diagram sources**
- [decrypt_worker.py:1-200](file://backend/app/services/decrypt_worker.py#L1-L200)
- [media_worker.py:1-200](file://backend/app/services/media_worker.py#L1-L200)
- [sync_worker.py:1-200](file://backend/app/services/sync_worker.py#L1-L200)

### Worker Coordination and Health Monitoring

The worker service architecture includes coordination mechanisms for managing worker lifecycle and health monitoring:

```mermaid
graph TB
subgraph "Worker Management"
coordinator[Worker Coordinator]
health_monitor[Health Monitor]
task_router[Task Router]
resource_manager[Resource Manager]
end
subgraph "Worker Instances"
decrypt_instances[DecryptWorker Instances]
media_instances[MediaWorker Instances]
sync_instances[SyncWorker Instances]
end
subgraph "Infrastructure"
message_queue[Message Queue]
storage_backend[Storage Backend]
database[(Database)]
end
coordinator --> health_monitor
coordinator --> task_router
coordinator --> resource_manager
health_monitor --> decrypt_instances
health_monitor --> media_instances
health_monitor --> sync_instances
task_router --> decrypt_instances
task_router --> media_instances
task_router --> sync_instances
decrypt_instances --> storage_backend
media_instances --> storage_backend
sync_instances --> storage_backend
decrypt_instances --> database
media_instances --> database
sync_instances --> database
```

**Diagram sources**
- [decrypt_worker.py:1-100](file://backend/app/services/decrypt_worker.py#L1-L100)
- [media_worker.py:1-100](file://backend/app/services/media_worker.py#L1-L100)
- [sync_worker.py:1-100](file://backend/app/services/sync_worker.py#L1-L100)

**Section sources**
- [decrypt_worker.py:1-300](file://backend/app/services/decrypt_worker.py#L1-L300)
- [media_worker.py:1-300](file://backend/app/services/media_worker.py#L1-L300)
- [sync_worker.py:1-300](file://backend/app/services/sync_worker.py#L1-L300)

## Architecture Overview

The WeCom Archive 365 system follows a layered architecture pattern with clear separation between presentation, business logic, and data access layers:

```mermaid
graph TB
subgraph "Presentation Layer"
web_app[Web Application]
api_gateway[API Gateway]
admin_console[Admin Console]
end
subgraph "Business Logic Layer"
auth_service[Authentication Service]
conversation_service[Conversation Service]
message_service[Message Service]
media_service[Media Service]
search_service[Search Service]
end
subgraph "Integration Layer"
wecom_sdk[WeCom SDK]
storage_abstraction[Storage Abstraction]
thumbnail_pipeline[Thumbnail Pipeline]
notification_service[Notification Service]
end
subgraph "Worker Services Layer"
decrypt_worker[DecryptWorker]
media_worker[MediaWorker]
sync_worker[SyncWorker]
worker_coordinator[Worker Coordinator]
end
subgraph "Data Layer"
postgresql[(PostgreSQL)]
local_storage[Local Filesystem]
qiniu_storage[Qiniu Cloud]
redis_cache[Redis Cache]
end
web_app --> api_gateway
api_gateway --> auth_service
api_gateway --> conversation_service
api_gateway --> message_service
api_gateway --> media_service
api_gateway --> search_service
conversation_service --> wecom_sdk
message_service --> storage_abstraction
media_service --> thumbnail_pipeline
media_service --> storage_abstraction
storage_abstraction --> postgresql
storage_abstraction --> local_storage
storage_abstraction --> qiniu_storage
thumbnail_pipeline --> local_storage
thumbnail_pipeline --> qiniu_storage
decrypt_worker --> storage_abstraction
media_worker --> storage_abstraction
sync_worker --> storage_abstraction
worker_coordinator --> decrypt_worker
worker_coordinator --> media_worker
worker_coordinator --> sync_worker
```

**Diagram sources**
- [main.py:1-100](file://backend/app/main.py#L1-L100)
- [media_storage.py:1-200](file://backend/app/media_storage.py#L1-L200)
- [decrypt_worker.py:1-100](file://backend/app/services/decrypt_worker.py#L1-L100)
- [media_worker.py:1-100](file://backend/app/services/media_worker.py#L1-L100)
- [sync_worker.py:1-100](file://backend/app/services/sync_worker.py#L1-L100)

## Detailed Component Analysis

### WeCom SDK Integration Layer

The WeCom SDK integration layer provides a clean abstraction over WeCom's REST APIs:

```mermaid
classDiagram
class WeComSDK {
+string corp_id
+string corp_secret
+string agent_id
+get_chat_data(chat_id, cursor, limit) dict
+get_media(media_id, save_path) bool
+verify_signature(token, signature, timestamp, nonce) bool
-build_request(method, url, params) Request
-handle_response(response) Response
}
class MessageParser {
+parse_message(raw_data) Message
+extract_metadata(message) Metadata
+classify_content(message) ContentType
}
class ContactSync {
+sync_contacts() list
+update_display_names() void
+get_employee_info(user_id) Employee
}
WeComSDK --> MessageParser : "uses"
WeComSDK --> ContactSync : "manages"
```

**Diagram sources**
- [wecom_sdk.py:1-200](file://backend/app/sdk/wecom_sdk.py#L1-L200)

### Message Ingestion Pipeline

The message ingestion pipeline handles the complete flow of WeCom messages:

```mermaid
flowchart TD
Start([Incoming Message]) --> Validate["Validate Signature"]
Validate --> Valid{"Valid?"}
Valid --> |No| Reject["Reject Request"]
Valid --> |Yes| Parse["Parse Message"]
Parse --> Classify["Classify Content Type"]
Classify --> Process["Process Based on Type"]
Process --> DownloadMedia{"Has Media?"}
DownloadMedia --> |Yes| QueueMedia["Queue Media Task"]
DownloadMedia --> |No| StoreDB["Store to Database"]
QueueMedia --> DecryptWorker["DecryptWorker"]
DecryptWorker --> GenerateThumb["Generate Thumbnail"]
GenerateThumb --> MediaWorker["MediaWorker"]
MediaWorker --> StoreMedia["Store Media"]
StoreMedia --> StoreDB
StoreDB --> Complete([Complete])
Reject --> End([End])
Complete --> End
```

**Diagram sources**
- [wecom_events.py:1-200](file://backend/app/routers/wecom_events.py#L1-L200)
- [decrypt_worker.py:1-150](file://backend/app/services/decrypt_worker.py#L1-L150)
- [media_worker.py:1-150](file://backend/app/services/media_worker.py#L1-L150)

### Storage Abstraction Layer

The storage abstraction layer supports multiple backends with unified interface:

```mermaid
classDiagram
class StorageBackend {
<<interface>>
+upload(file_path, key) string
+download(key, dest_path) bool
+delete(key) bool
+exists(key) bool
+get_url(key, expires) string
}
class LocalStorage {
+base_path string
+upload(file_path, key) string
+download(key, dest_path) bool
+delete(key) bool
+get_url(key, expires) string
}
class QiniuStorage {
+bucket string
+domain string
+credentials Credentials
+upload(file_path, key) string
+download(key, dest_path) bool
+delete(key) bool
+get_url(key, expires) string
}
class StorageFactory {
+create_backend(backend_type, config) StorageBackend
+get_backend(tenant_id) StorageBackend
}
StorageBackend <|-- LocalStorage
StorageBackend <|-- QiniuStorage
StorageFactory --> StorageBackend : "creates"
```

**Diagram sources**
- [media_storage.py:1-300](file://backend/app/media_storage.py#L1-L300)
- [qiniu_storage.py:1-200](file://backend/app/qiniu_storage.py#L1-L200)

### Thumbnail Generation Pipeline

The thumbnail generation pipeline processes media files asynchronously:

```mermaid
sequenceDiagram
participant Queue as "Task Queue"
participant Worker as "Thumbnail Worker"
participant Processor as "Image Processor"
participant Storage as "Storage Backend"
Queue->>Worker : Process Image Task
Worker->>Storage : Download Original Image
Storage-->>Worker : Image Data
Worker->>Processor : Generate Thumbnails
Processor-->>Worker : Thumbnail Images
Worker->>Storage : Upload Thumbnails
Storage-->>Worker : Success
Worker-->>Queue : Task Complete
```

**Diagram sources**
- [thumbnail_pipeline.py:1-250](file://backend/app/thumbnail_pipeline.py#L1-L250)

### Web Application Serving

The web application provides both user-facing and administrative interfaces:

```mermaid
graph TB
subgraph "Frontend"
react_app[React Application]
static_assets[Static Assets]
i18n[Internationalization]
end
subgraph "Backend Routes"
auth_routes[Auth Routes]
conversation_routes[Conversation Routes]
search_routes[Search Routes]
admin_routes[Admin Routes]
media_routes[Media Routes]
end
subgraph "Services"
auth_service[Auth Service]
conversation_service[Conversation Service]
search_service[Search Service]
media_service[Media Service]
end
react_app --> auth_routes
react_app --> conversation_routes
react_app --> search_routes
react_app --> admin_routes
react_app --> media_routes
auth_routes --> auth_service
conversation_routes --> conversation_service
search_routes --> search_service
media_routes --> media_service
```

**Diagram sources**
- [conversations.py:1-200](file://backend/app/routers/conversations.py#L1-L200)

**Section sources**
- [wecom_sdk.py:1-300](file://backend/app/sdk/wecom_sdk.py#L1-L300)
- [media_storage.py:1-400](file://backend/app/media_storage.py#L1-L400)
- [thumbnail_pipeline.py:1-300](file://backend/app/thumbnail_pipeline.py#L1-L300)
- [decrypt_worker.py:1-300](file://backend/app/services/decrypt_worker.py#L1-L300)
- [media_worker.py:1-300](file://backend/app/services/media_worker.py#L1-L300)
- [sync_worker.py:1-300](file://backend/app/services/sync_worker.py#L1-L300)

## Dependency Analysis

The system has well-defined dependencies between components:

```mermaid
graph TB
subgraph "Core Dependencies"
fastapi[FastAPI Framework]
sqlalchemy[SQLAlchemy ORM]
pydantic[Pydantic Validation]
celery[Celery Tasks]
redis[Redis Client]
end
subgraph "Storage Dependencies"
boto3[Boto3 AWS SDK]
qiniu_client[Qiniu SDK]
pillow[Pillow Image Library]
ffmpeg[FFmpeg for Video]
end
subgraph "Security Dependencies"
jwt[JWT Authentication]
bcrypt[Bcrypt Password Hashing]
cryptography[Cryptography Library]
end
subgraph "Infrastructure Dependencies"
psycopg2[PostgreSQL Driver]
alembic[Alembic Migrations]
asyncio[Async I/O]
multiprocessing[Multiprocessing]
end
fastapi --> sqlalchemy
fastapi --> pydantic
sqlalchemy --> psycopg2
celery --> redis
media_storage --> qiniu_client
thumbnail_pipeline --> pillow
thumbnail_pipeline --> ffmpeg
auth --> jwt
auth --> bcrypt
decrypt_worker --> cryptography
media_worker --> pillow
sync_worker --> asyncio
```

**Diagram sources**
- [requirements.txt:1-100](file://backend/requirements.txt#L1-L100)
- [decrypt_worker.py:1-100](file://backend/app/services/decrypt_worker.py#L1-L100)
- [media_worker.py:1-100](file://backend/app/services/media_worker.py#L1-L100)
- [sync_worker.py:1-100](file://backend/app/services/sync_worker.py#L1-L100)

**Section sources**
- [requirements.txt:1-150](file://backend/requirements.txt#L1-L150)

## Performance Considerations

### Database Optimization
- Connection pooling with SQLAlchemy
- Query optimization with proper indexing
- Tenant-scoped queries for performance
- Caching strategies for frequently accessed data

### Storage Performance
- CDN integration for media delivery
- Async processing for heavy operations
- Chunked uploads for large files
- Compression for storage efficiency

### Worker Service Performance
- Horizontal scaling of worker instances
- Load balancing across worker pools
- Memory-efficient message processing
- Background task prioritization

### Scalability Patterns
- Horizontal scaling of stateless services
- Database read replicas for query distribution
- Message queue for load balancing
- Cache layer for reduced database load
- Worker auto-scaling based on queue depth

## Troubleshooting Guide

### Common Issues
- **WeCom API Rate Limiting**: Implement exponential backoff
- **Storage Backend Failures**: Fallback mechanisms and retry logic
- **Memory Issues**: Stream processing for large files
- **Database Connection Pool Exhaustion**: Proper connection management
- **Worker Service Crashes**: Health monitoring and automatic restarts
- **Tenant Isolation Violations**: Strict access control validation

### Worker Service Troubleshooting
- **Worker Health Checks**: Monitor worker status and performance metrics
- **Message Queue Backlog**: Track queue depth and processing rates
- **Tenant-Specific Errors**: Isolate and debug tenant-scoped issues
- **Resource Contention**: Monitor CPU, memory, and I/O usage per worker

### Monitoring and Logging
- Structured logging with correlation IDs
- Health check endpoints for service monitoring
- Metrics collection for performance tracking
- Alerting for critical failures
- Worker performance dashboards
- Queue depth monitoring

**Section sources**
- [auth.py:1-200](file://backend/app/auth.py#L1-L200)
- [media_storage.py:1-400](file://backend/app/media_storage.py#L1-L400)
- [decrypt_worker.py:1-200](file://backend/app/services/decrypt_worker.py#L1-L200)
- [media_worker.py:1-200](file://backend/app/services/media_worker.py#L1-L200)
- [sync_worker.py:1-200](file://backend/app/services/sync_worker.py#L1-L200)

## Conclusion

The WeCom Archive 365 system provides a robust, scalable, and secure solution for enterprise message archiving. Its microservices architecture, event-driven design, and multi-tenant isolation make it suitable for large-scale deployments. The flexible storage abstraction allows organizations to choose their preferred storage backend while maintaining consistent APIs and behavior.

The addition of comprehensive worker services (DecryptWorker, MediaWorker, and SyncWorker) significantly enhances the system's capability to handle specialized background tasks with tenant-scoped operations and structured message processing. This worker service architecture ensures efficient processing of decryption, media handling, and synchronization tasks while maintaining strict tenant isolation and providing scalable background processing capabilities.

The system's comprehensive approach to security, performance, and scalability ensures reliable operation in enterprise environments while providing the necessary tools for compliance and audit requirements. The worker service architecture enables horizontal scaling and efficient resource utilization for high-volume message processing scenarios.