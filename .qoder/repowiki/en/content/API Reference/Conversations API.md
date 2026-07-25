# Conversations API

<cite>
**Referenced Files in This Document**
- [main.py](file://backend/app/main.py)
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [timeline.py](file://backend/app/schemas/timeline.py)
- [models.py](file://backend/app/db/models.py)
- [schema_check.py](file://backend/app/db/schema_check.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [message_type_registry.py](file://backend/app/message_type_registry.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)
- [auth.py](file://backend/app/auth.py)
- [API.md](file://docs/API.md)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [DATA_MODEL.md](file://docs/DATA_MODEL.md)
</cite>

## Update Summary
**Changes Made**
- Updated architecture overview to reflect the extraction of media functionality into dedicated media service
- Simplified conversations router from 1166 lines to 40 lines by removing media-related endpoints
- Added comprehensive documentation for new /media router with dedicated media endpoints
- Updated component analysis to show cleaner separation between conversation and media concerns
- Revised dependency analysis to reflect the new media service layer separation
- Updated performance considerations to include benefits of modularized media handling

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
This document provides comprehensive API documentation for conversation management endpoints, including:
- Conversation retrieval and metadata operations
- Message listing with pagination, filtering by date range and message types, and sorting
- Participant management within conversations
- Timeline resolution and projection services for efficient message timeline operations
- Real-time updates via WebSocket connections for live message streaming and event notifications
- Practical examples of common workflows and error handling patterns

The backend is a FastAPI application that exposes REST endpoints and supports real-time communication through WebSockets. Data models are defined using SQLAlchemy, and the system integrates with WeCom (WeChat Work) to archive and manage conversations and messages.

**Updated** The architecture has been significantly simplified by extracting media functionality into a dedicated media service, reducing the conversations router from 1166 lines to just 40 lines while maintaining full API compatibility and improving code organization.

## Project Structure
The project follows a modular architecture with clear separation of concerns:
- **Routers**: HTTP endpoint definitions and request/response handling
- **Services**: Business logic for conversation management, participant operations, timeline resolution, and shared listing functionality
- **Database Models**: SQLAlchemy ORM models for conversations, messages, and participants
- **Utilities**: Helper functions for message parsing, display names, and contact synchronization

```mermaid
graph TB
subgraph "API Layer"
ConversationsRouter[Conversations Router - 40 lines]
MediaRouter[Media Router - Dedicated Service]
Auth[Authentication Middleware]
end
subgraph "Service Layer"
ListingService[Listing Service]
TimelineService[Timeline Service]
Membership[Conversation Membership Service]
Parser[Structured Message Parser]
MediaService[Dedicated Media Service]
end
subgraph "Data Layer"
Models[SQLAlchemy Models]
DB[(Database)]
end
subgraph "External Integrations"
WeCom[WeCom SDK]
Storage[Media Storage Backend]
end
ConversationsRouter --> ListingService
ConversationsRouter --> TimelineService
ConversationsRouter --> Membership
ConversationsRouter --> Parser
MediaRouter --> MediaService
MediaService --> Storage
ListingService --> Models
TimelineService --> Models
Membership --> Models
Parser --> Models
Models --> DB
ConversationsRouter --> WeCom
MediaRouter --> WeCom
```

**Diagram sources**
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [models.py](file://backend/app/db/models.py)

**Section sources**
- [main.py](file://backend/app/main.py)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)

## Core Components
The conversation management system consists of several key components with improved modularity:

### Conversation Router (Simplified)
Handles only core conversation operations, now reduced to 40 lines after extracting media functionality. Focuses exclusively on conversation CRUD operations, message listing, and participant management without media handling complexity.

### Media Router (New)
**New** A dedicated router specifically for media-related operations, providing clean separation of concerns and improved maintainability for media handling functionality.

### Listing Service
A dedicated service layer that handles common listing operations, pagination, filtering, and sorting across different entity types. This extraction improves code reusability and reduces duplication between conversation and message listing endpoints.

### Timeline Service
A specialized service layer responsible for timeline resolution and projection operations. This service handles complex timeline queries, message ordering, and temporal data projections for efficient conversation timeline rendering.

### Conversation Membership Service
Manages participant relationships within conversations, including adding/removing members and managing permissions.

### Message Type Registry
Defines supported message types and their corresponding handlers for different content formats.

### Structured Message Parser
Parses complex message structures from WeCom into standardized formats for consistent processing.

**Updated** The refactoring introduces a much cleaner separation between HTTP routing concerns and business logic, with the conversations router now focused solely on conversation operations while media handling is delegated to a dedicated service layer.

**Section sources**
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [message_type_registry.py](file://backend/app/message_type_registry.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)

## Architecture Overview
The Conversations API follows a layered architecture pattern with clear separation between presentation, business logic, and data access layers. The recent refactoring significantly enhances this separation by extracting media functionality into a dedicated service layer.

```mermaid
sequenceDiagram
participant Client as "API Client"
participant ConversationsRouter as "Conversations Router (40 lines)"
participant MediaRouter as "Media Router"
participant ListingService as "Listing Service"
participant TimelineService as "Timeline Service"
participant MediaService as "Media Service"
participant DB as "Database"
Client->>ConversationsRouter : GET /api/conversations/{id}/messages
ConversationsRouter->>ListingService : list_with_filters()
ListingService->>DB : query_messages_with_pagination()
DB-->>ListingService : paginated_results
ListingService-->>ConversationsRouter : formatted_response
Note over Client,DB : Media operations now handled separately<br/>through /media router
Client->>MediaRouter : POST /api/media/upload
MediaRouter->>MediaService : handle_media_upload()
MediaService->>DB : store_media_metadata()
MediaService-->>MediaRouter : upload_response
```

**Updated** The new architecture separates conversation and media concerns into dedicated routers, significantly reducing complexity in the conversations router while maintaining full API functionality through the new media service layer.

**Diagram sources**
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [models.py](file://backend/app/db/models.py)

## Detailed Component Analysis

### Simplified Conversation Endpoints

#### List Conversations
- **HTTP Method**: GET
- **URL Pattern**: `/api/conversations`
- **Query Parameters**:
  - `page`: Page number (default: 1)
  - `per_page`: Items per page (default: 50, max: 100)
  - `created_after`: Filter by creation date (ISO 8601 format)
  - `created_before`: Filter by creation date (ISO 8601 format)
  - `updated_after`: Filter by last update date (ISO 8601 format)
  - `updated_before`: Filter by last update date (ISO 8601 format)
  - `type`: Filter by conversation type (group/private)
  - `sort_by`: Sort field (created_at, updated_at, name)
  - `sort_order`: Sort direction (asc, desc)

#### Get Conversation Details
- **HTTP Method**: GET
- **URL Pattern**: `/api/conversations/{conversation_id}`
- **Path Parameters**:
  - `conversation_id`: UUID of the conversation

#### List Messages in Conversation
- **HTTP Method**: GET
- **URL Pattern**: `/api/conversations/{conversation_id}/messages`
- **Query Parameters**:
  - `page`: Page number (default: 1)
  - `per_page`: Items per page (default: 50, max: 100)
  - `message_types`: Comma-separated list of message types to filter
  - `created_after`: Filter by message creation date
  - `created_before`: Filter by message creation date
  - `sender_id`: Filter by sender user ID
  - `has_media`: Boolean flag for messages containing media
  - `sort_by`: Sort field (created_at, updated_at)
  - `sort_order`: Sort direction (asc, desc)

#### Update Conversation Metadata
- **HTTP Method**: PUT
- **URL Pattern**: `/api/conversations/{conversation_id}`
- **Request Body**:
  ```json
  {
    "name": "Updated conversation name",
    "description": "Updated description",
    "metadata": {
      "custom_field": "value"
    }
  }
  ```

### New Media Endpoints

#### Upload Media
- **HTTP Method**: POST
- **URL Pattern**: `/api/media/upload`
- **Request Body**: Multipart form data with file and metadata
- **Response**: Media access descriptor with download URL

#### Download Media
- **HTTP Method**: GET
- **URL Pattern**: `/api/media/{media_id}`
- **Path Parameters**:
  - `media_id`: UUID of the media file
- **Response**: Binary media content or thumbnail

#### Delete Media
- **HTTP Method**: DELETE
- **URL Pattern**: `/api/media/{media_id}`
- **Path Parameters**:
  - `media_id`: UUID of the media file

### Participant Management

#### Add Participant
- **HTTP Method**: POST
- **URL Pattern**: `/api/conversations/{conversation_id}/participants`
- **Request Body**:
  ```json
  {
    "user_id": "uuid-of-user",
    "role": "member|admin|viewer",
    "permissions": ["read", "write", "manage"]
  }
  ```

#### Remove Participant
- **HTTP Method**: DELETE
- **URL Pattern**: `/api/conversations/{conversation_id}/participants/{user_id}`

#### Update Participant Role
- **HTTP Method**: PUT
- **URL Pattern**: `/api/conversations/{conversation_id}/participants/{user_id}`
- **Request Body**:
  ```json
  {
    "role": "member|admin|viewer",
    "permissions": ["read", "write", "manage"]
  }
  ```

### Real-time Updates

#### WebSocket Connection
- **Connection URL**: `/ws/conversations/{conversation_id}`
- **Authentication**: Required via query parameter or header
- **Supported Events**:
  - `message_new`: New message received
  - `message_updated`: Message content updated
  - `participant_joined`: New participant added
  - `participant_left`: Participant removed
  - `conversation_updated`: Conversation metadata changed

#### WebSocket Message Format
```json
{
  "event": "message_new",
  "data": {
    "conversation_id": "uuid",
    "message_id": "uuid",
    "timestamp": "2024-01-01T00:00:00Z",
    "content": {...}
  },
  "metadata": {
    "sender_id": "uuid",
    "message_type": "text|image|video|document"
  }
}
```

**Section sources**
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [models.py](file://backend/app/db/models.py)

## Dependency Analysis
The conversation management system now has a much cleaner dependency structure with dedicated separation between conversation and media concerns.

```mermaid
classDiagram
class ConversationRouter {
+list_conversations()
+get_conversation()
+list_messages()
+update_metadata()
+add_participant()
+remove_participant()
}
class MediaRouter {
+upload_media()
+download_media()
+delete_media()
+get_media_info()
}
class ListingService {
+list_with_filters()
+apply_pagination()
+apply_sorting()
+build_query_filters()
}
class TimelineService {
+resolve_timeline()
+project_messages()
+handle_temporal_queries()
+optimize_timeline_rendering()
}
class MediaService {
+handle_upload()
+process_media()
+generate_thumbnails()
+manage_storage()
}
class MembershipService {
+add_member()
+remove_member()
+update_permissions()
+get_participants()
}
class MessageParser {
+parse_content()
+extract_media()
+validate_format()
}
class DatabaseModels {
+Conversation
+Message
+Participant
+MessageContent
+Media
}
ConversationRouter --> ListingService : "uses for listing"
ConversationRouter --> TimelineService : "uses for timeline"
ConversationRouter --> MembershipService : "uses for membership"
ConversationRouter --> MessageParser : "uses for parsing"
MediaRouter --> MediaService : "uses for media handling"
MediaService --> DatabaseModels : "manages media data"
ListingService --> DatabaseModels : "manages queries"
TimelineService --> DatabaseModels : "manages timeline queries"
MembershipService --> DatabaseModels : "manages"
MessageParser --> DatabaseModels : "reads"
```

**Updated** The new architecture significantly reduces coupling between the conversations router and media handling, with the conversations router now focusing solely on conversation operations while media functionality is encapsulated in a dedicated service layer.

**Diagram sources**
- [conversations.py](file://backend/app/routers/conversations.py)
- [media.py](file://backend/app/routers/media.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [models.py](file://backend/app/db/models.py)

**Section sources**
- [models.py](file://backend/app/db/models.py)
- [conversation_membership.py](file://backend/app/conversation_membership.py)
- [listing_service.py](file://backend/app/services/listing_service.py)
- [timeline_service.py](file://backend/app/services/timeline_service.py)

## Performance Considerations
- **Pagination**: All list endpoints support pagination to prevent large responses
- **Indexing**: Database indexes on frequently queried fields (conversation_id, created_at, sender_id)
- **Caching**: Redis caching for frequently accessed conversation metadata
- **Streaming**: Large message lists use server-sent events for efficient delivery
- **Connection Pooling**: Database connection pooling for concurrent requests
- **Rate Limiting**: API rate limiting to prevent abuse
- **Service Layer Optimization**: Dedicated listing and timeline services optimize common query patterns and reduce redundant database calls
- **Timeline Resolution**: Optimized timeline projection algorithms for efficient message ordering and temporal queries
- **Modular Media Handling**: Separated media operations improve performance through specialized optimization and resource management

**Updated** The extraction of media functionality into a dedicated service layer significantly improves performance through better resource isolation, specialized media handling optimizations, and reduced complexity in the main conversation router.

## Troubleshooting Guide

### Common Error Responses

#### 404 Not Found
- **Cause**: Invalid conversation ID or message ID
- **Solution**: Verify the resource exists and you have permission to access it

#### 403 Forbidden
- **Cause**: Insufficient permissions for the requested operation
- **Solution**: Check user roles and conversation membership status

#### 429 Too Many Requests
- **Cause**: Rate limit exceeded
- **Solution**: Implement exponential backoff and retry logic

#### 500 Internal Server Error
- **Cause**: Database connection issues or unexpected exceptions
- **Solution**: Check application logs and database connectivity

### Debugging Tips
- Enable detailed logging for API requests
- Use the health check endpoint to verify service status
- Monitor WebSocket connection stability
- Check database query performance with slow query logs
- Monitor listing and timeline service performance metrics for query optimization opportunities
- Profile timeline resolution operations for temporal query bottlenecks
- Monitor media service performance for upload/download bottlenecks
- Check media storage backend connectivity and performance

**Section sources**
- [auth.py](file://backend/app/auth.py)
- [schema_check.py](file://backend/app/db/schema_check.py)

## Conclusion
The Conversations API provides a comprehensive set of endpoints for managing conversations, messages, and participants in a secure and scalable manner. The recent refactoring significantly enhances the system's maintainability and performance by extracting media functionality into a dedicated service layer, reducing the conversations router from 1166 lines to just 40 lines.

Key features include:
- RESTful API design with comprehensive documentation
- Real-time messaging through WebSocket connections
- Flexible filtering and sorting options
- Secure participant management with role-based access control
- Efficient media handling through dedicated media service
- Optimized listing operations through dedicated service layer
- Specialized timeline resolution and projection services for efficient temporal queries
- Modular architecture with clear separation of concerns

**Updated** The architectural improvements provide significantly better code organization, improved testability, enhanced performance, specialized media handling, and cleaner separation of concerns while maintaining full backward compatibility with existing API consumers.

## Appendices

### API Documentation Reference
For complete API specifications, refer to the main API documentation file.

**Section sources**
- [API.md](file://docs/API.md)
- [DATA_MODEL.md](file://docs/DATA_MODEL.md)

### Data Model Reference
The data model documentation provides detailed information about database schemas and relationships.

**Section sources**
- [DATA_MODEL.md](file://docs/DATA_MODEL.md)
- [models.py](file://backend/app/db/models.py)