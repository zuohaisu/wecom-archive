# API Reference

<cite>
**Referenced Files in This Document**
- [main.py](file://backend/app/main.py)
- [auth.py](file://backend/app/auth.py)
- [auth.py](file://backend/app/routers/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [search.py](file://backend/app/routers/search.py)
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)
- [API.md](file://docs/API.md)
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
10. [Appendices](#appendices)

## Introduction
This document provides a comprehensive API reference for the WeCom Archive 365 backend. It covers authentication endpoints, conversation management APIs, search functionality, reachability audit endpoints, and WeCom webhook handlers. For each endpoint, it specifies HTTP methods, URL patterns, request/response schemas, authentication requirements, error codes, pagination, filtering, rate limiting, and versioning considerations. Practical examples are included to illustrate typical requests and responses.

## Project Structure
The REST API is implemented using FastAPI routers under the app/routers directory. The application entry point registers routers and middleware, while shared logic (authentication, tenant scoping, media handling) lives in the app package. Documentation artifacts and design notes are available in the docs directory.

```mermaid
graph TB
A["App Entry<br/>main.py"] --> B["Auth Router<br/>routers/auth.py"]
A --> C["Conversations Router<br/>routers/conversations.py"]
A --> D["Search Router<br/>routers/search.py"]
A --> E["Reachability Audit Router<br/>routers/reachability_audit.py"]
A --> F["WeCom Events Router<br/>routers/wecom_events.py"]
G["Shared Auth Logic<br/>app/auth.py"] --> B
H["Docs & Contracts<br/>docs/API.md"] -.-> A
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [auth.py](file://backend/app/auth.py)
- [auth.py](file://backend/app/routers/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [search.py](file://backend/app/routers/search.py)
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)
- [API.md](file://docs/API.md)

**Section sources**
- [main.py](file://backend/app/main.py)
- [API.md](file://docs/API.md)

## Core Components
- Authentication: Token-based access with tenant isolation and optional password flow.
- Conversations: CRUD and membership operations scoped by tenant.
- Search: Full-text and filtered queries over archived messages with pagination.
- Reachability Audit: Health and capability checks for WeCom integration.
- WeCom Webhooks: Event ingestion from WeCom with signature verification and idempotency.

Key implementation locations:
- App entry and router registration: [main.py](file://backend/app/main.py)
- Shared auth utilities: [auth.py](file://backend/app/auth.py)
- Auth endpoints: [auth.py](file://backend/app/routers/auth.py)
- Conversation endpoints: [conversations.py](file://backend/app/routers/conversations.py)
- Search endpoints: [search.py](file://backend/app/routers/search.py)
- Reachability endpoints: [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- WeCom webhooks: [wecom_events.py](file://backend/app/routers/wecom_events.py)

**Section sources**
- [auth.py](file://backend/app/auth.py)
- [auth.py](file://backend/app/routers/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [search.py](file://backend/app/routers/search.py)
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)

## Architecture Overview
The API follows a layered architecture:
- HTTP layer: FastAPI routers define endpoints and validation.
- Service layer: Business logic for conversations, search, audits, and event processing.
- Data layer: Database models and migrations manage persistence.
- External integrations: WeCom SDK and storage backends handle external data and media.

```mermaid
sequenceDiagram
participant Client as "Client"
participant API as "FastAPI Routers"
participant Auth as "Auth Middleware/Utils"
participant Svc as "Service Layer"
participant DB as "Database"
participant WC as "WeCom SDK"
Client->>API : "HTTP Request"
API->>Auth : "Validate token/tenant"
Auth-->>API : "Authenticated context"
API->>Svc : "Invoke business logic"
Svc->>DB : "Read/Write data"
DB-->>Svc : "Results"
Svc->>WC : "Optional WeCom calls"
WC-->>Svc : "External results"
Svc-->>API : "Response payload"
API-->>Client : "HTTP Response"
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [auth.py](file://backend/app/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [search.py](file://backend/app/routers/search.py)
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)

## Detailed Component Analysis

### Authentication Endpoints
- Purpose: Issue tokens, validate credentials, and enforce tenant isolation.
- Methods:
  - POST /api/v1/auth/login: Authenticate user or service account; returns access token and metadata.
  - POST /api/v1/auth/token/refresh: Refresh an expiring token using a refresh token.
  - GET /api/v1/auth/me: Retrieve current authenticated user context and tenant info.
- Authentication: None for login; bearer token required for other endpoints.
- Request/Response Schema:
  - Login: {username_or_email, password} -> {access_token, token_type, expires_in, tenant_id}
  - Refresh: {refresh_token} -> {access_token, token_type, expires_in}
  - Me: No body -> {user_id, tenant_id, roles, permissions}
- Error Codes:
  - 401 Unauthorized: Invalid credentials or expired token.
  - 403 Forbidden: Insufficient permissions.
  - 422 Unprocessable Entity: Validation errors.
- Rate Limiting: Enforced per IP/user; see Performance section.
- Pagination: N/A.
- Filtering: N/A.
- Versioning: All endpoints prefixed with /api/v1/.

Example call:
- Request: POST /api/v1/auth/login
- Body: {"username_or_email": "admin@example.com", "password": "secret"}
- Response: {"access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600, "tenant_id": "t_abc"}

**Section sources**
- [auth.py](file://backend/app/routers/auth.py)
- [auth.py](file://backend/app/auth.py)

### Conversation Management APIs
- Purpose: Manage archived conversations and memberships within a tenant.
- Methods:
  - GET /api/v1/conversations: List conversations with filters and pagination.
  - GET /api/v1/conversations/{conversation_id}: Retrieve conversation details.
  - PUT /api/v1/conversations/{conversation_id}/members: Update membership list.
  - DELETE /api/v1/conversations/{conversation_id}/members/{member_id}: Remove member.
  - GET /api/v1/conversations/{conversation_id}/messages: List messages in a conversation.
- Authentication: Bearer token required; tenant-scoped.
- Request/Response Schema:
  - List: Query params include page, page_size, filter_by_participant, filter_by_date_range -> {items: [Conversation], total, has_more}
  - Details: Path param conversation_id -> {id, title, participants, created_at, updated_at}
  - Membership update: Body {add: [user_ids], remove: [user_ids]} -> {updated_members: [User]}
  - Messages: Query params include page, page_size, sort_by, direction -> {items: [Message], total, has_more}
- Error Codes:
  - 404 Not Found: Conversation or member not found.
  - 403 Forbidden: Tenant mismatch or insufficient permissions.
  - 422 Unprocessable Entity: Validation errors.
- Rate Limiting: Enforced per tenant; see Performance section.
- Pagination: Cursor or offset-based via page/page_size; consistent across list endpoints.
- Filtering: Participant, date range, message type, keyword (for messages).

Example call:
- Request: GET /api/v1/conversations?filter_by_participant=user_123&page=1&page_size=50
- Response: {"items": [...], "total": 120, "has_more": true}

**Section sources**
- [conversations.py](file://backend/app/routers/conversations.py)

### Search Functionality
- Purpose: Search archived messages across tenants with advanced filters.
- Methods:
  - POST /api/v1/search/messages: Execute search queries with filters and pagination.
- Authentication: Bearer token required; tenant-scoped.
- Request/Response Schema:
  - Search: Body {query, filters: {date_range, types, participants, keywords}, page, page_size, sort_by, direction} -> {results: [Message], total, has_more}
- Error Codes:
  - 400 Bad Request: Invalid query or filters.
  - 403 Forbidden: Tenant mismatch.
  - 422 Unprocessable Entity: Validation errors.
- Rate Limiting: Enforced per tenant; see Performance section.
- Pagination: Offset-based via page/page_size; supports sorting.
- Filtering: Date range, message types, participants, keywords, conversation scope.

Example call:
- Request: POST /api/v1/search/messages
- Body: {"query": "meeting notes", "filters": {"date_range": {"start": "2024-01-01", "end": "2024-12-31"}, "types": ["text", "image"]}, "page": 1, "page_size": 20}
- Response: {"results": [...], "total": 45, "has_more": false}

**Section sources**
- [search.py](file://backend/app/routers/search.py)

### Reachability Audit Endpoints
- Purpose: Provide health checks and WeCom integration status.
- Methods:
  - GET /api/v1/health: Basic health check.
  - GET /api/v1/reachability/wecom: Check WeCom connectivity and configuration.
- Authentication: Optional bearer token for sensitive checks; public for basic health.
- Request/Response Schema:
  - Health: No body -> {status: "ok", timestamp, version}
  - WeCom Reachability: No body -> {status: "connected|disconnected", last_sync_at, errors: [string]}
- Error Codes:
  - 500 Internal Server Error: Unexpected failures.
  - 503 Service Unavailable: WeCom service down.
- Rate Limiting: Minimal; designed for monitoring.
- Pagination: N/A.
- Filtering: N/A.

Example call:
- Request: GET /api/v1/reachability/wecom
- Response: {"status": "connected", "last_sync_at": "2024-10-01T12:00:00Z", "errors": []}

**Section sources**
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)

### WeCom Webhook Handlers
- Purpose: Ingest events from WeCom, verify signatures, and process asynchronously.
- Methods:
  - POST /api/v1/webhooks/wecom/events: Receive event payloads from WeCom.
- Authentication: Signature verification via WeCom token and timestamp; no bearer token required.
- Request/Response Schema:
  - Event: Body {msgtype, content, timestamp, sign, ...} -> {acknowledged: boolean, event_id: string}
- Error Codes:
  - 400 Bad Request: Invalid signature or malformed payload.
  - 422 Unprocessable Entity: Unsupported msgtype or missing fields.
  - 500 Internal Server Error: Processing failure.
- Rate Limiting: Enforced per source IP; see Performance section.
- Pagination: N/A.
- Filtering: N/A.
- Real-time Updates: Webhooks trigger async processing; clients poll or use SSE/WebSocket if enabled.

Example call:
- Request: POST /api/v1/webhooks/wecom/events
- Body: {"msgtype": "text", "content": "Hello", "timestamp": 1696156800, "sign": "abc123"}
- Response: {"acknowledged": true, "event_id": "evt_123"}

**Section sources**
- [wecom_events.py](file://backend/app/routers/wecom_events.py)

## Dependency Analysis
The API components depend on shared services and external systems:
- Routers depend on auth utilities for security.
- Services interact with database models and WeCom SDK.
- Media handling uses storage backends (local or Qiniu).
- Migrations manage schema evolution.

```mermaid
graph TB
subgraph "Routers"
AR["Auth Router"]
CR["Conversations Router"]
SR["Search Router"]
RR["Reachability Router"]
WR["WeCom Events Router"]
end
subgraph "Services"
AS["Auth Service"]
CS["Conversation Service"]
SS["Search Service"]
RS["Reachability Service"]
WS["WeCom Service"]
end
subgraph "Data"
DB["Database"]
Models["Models"]
end
subgraph "External"
WC["WeCom SDK"]
Storage["Storage Backend"]
end
AR --> AS
CR --> CS
SR --> SS
RR --> RS
WR --> WS
AS --> DB
CS --> DB
SS --> DB
RS --> DB
WS --> WC
CS --> Storage
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [auth.py](file://backend/app/auth.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [search.py](file://backend/app/routers/search.py)
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)

**Section sources**
- [main.py](file://backend/app/main.py)

## Performance Considerations
- Rate Limiting: Implemented per client/IP and tenant; configurable limits for auth, search, and webhooks.
- Pagination: Consistent offset-based pagination with page/page_size parameters; avoid large page sizes.
- Caching: Read-heavy endpoints may benefit from caching strategies (e.g., Redis for frequent queries).
- Async Processing: Webhook events processed asynchronously to prevent blocking.
- Database Optimization: Indexes on frequently queried fields (e.g., tenant_id, conversation_id, timestamps).
- Connection Pooling: Ensure efficient DB and external API connections.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- Authentication Failures: Verify token validity, tenant alignment, and permissions.
- Search Errors: Check query syntax, filter constraints, and tenant scope.
- WeCom Webhook Rejections: Validate signature, timestamp, and payload structure.
- Reachability Issues: Inspect WeCom configuration and network connectivity.
- Performance Degradation: Monitor rate limits, database queries, and external API latency.

Debugging tips:
- Use health and reachability endpoints to diagnose system status.
- Enable detailed logging for failed requests.
- Validate payloads with schema validators before sending.

**Section sources**
- [reachability_audit.py](file://backend/app/routers/reachability_audit.py)
- [wecom_events.py](file://backend/app/routers/wecom_events.py)

## Conclusion
The WeCom Archive 365 backend provides a robust API for managing archived conversations, searching messages, auditing reachability, and ingesting WeCom events. With clear authentication, tenant isolation, and scalable design, it supports enterprise-grade use cases. Adhering to the documented schemas, pagination, and rate limiting ensures reliable integration.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices
- API Versioning: All endpoints use /api/v1/ prefix; future versions will increment the version segment.
- Backward Compatibility: Deprecation notices provided for breaking changes; maintain clients should migrate gradually.
- WebSocket/SSE: If enabled, real-time updates can be consumed via separate channels; refer to deployment docs.
- Examples: See practical examples in each endpoint section for quick start.

[No sources needed since this section provides general guidance]