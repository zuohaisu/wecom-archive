# Web Application Architecture

<cite>
**Referenced Files in This Document**
- [main.py](file://backend/app/main.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [auth.py](file://backend/app/auth.py)
- [web/__init__.py](file://backend/app/web/__init__.py)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)
- [static/console/conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [static/console/media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [static/console/message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [static/console/timeline.js](file://backend/app/web/static/console/timeline.js)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)
- [templates/messages.html](file://backend/app/web/templates/messages.html)
- [templates/message_detail.html](file://backend/app/web/templates/message_detail.html)
- [templates/review_console.html](file://backend/app/web/templates/review_console.html)
- [templates/search.html](file://backend/app/web/templates/search.html)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)
</cite>

## Update Summary
**Changes Made**
- Updated Project Structure section to reflect the new modular router architecture with dedicated message and web routers
- Enhanced FastAPI Server Setup section to document the reduced complexity in main.py through route extraction
- Added detailed analysis of the separation of concerns achieved through modular routing
- Updated all relevant diagrams to show the improved architectural boundaries between routers and services
- Strengthened the Service Layer Architecture section to highlight the business logic abstraction

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Service Layer Architecture](#service-layer-architecture)
7. [Dependency Analysis](#dependency-analysis)
8. [Performance Considerations](#performance-considerations)
9. [Troubleshooting Guide](#troubleshooting-guide)
10. [Conclusion](#conclusion)

## Introduction
This document describes the web application architecture for a WeCom archive system built with FastAPI. The application has undergone a major architectural refactoring that extracts routes into dedicated modules (messages.py, web.py), significantly reducing main.py complexity and improving separation of concerns. The modular router architecture follows Flask/FastAPI best practices with clear boundaries between authentication, conversation management, message operations, web page rendering, and search functionality. Business logic is abstracted into service layers for better maintainability and testability. It covers server setup, template rendering, static asset management, and the client-side JavaScript console used for conversation listing, message viewing, media browsing, and search. It also explains authentication middleware, session management, role-based access control, responsive design patterns, internationalization support, real-time update mechanisms, diagnostics and health endpoints, error handling strategies, and performance monitoring integration.

## Project Structure
The backend is organized under backend/app with clear separation between API routers, service layer, data models, storage logic, and web-facing assets (templates and static files). The recent architectural refactoring has moved route definitions from a monolithic main.py into dedicated router modules, creating a clean separation of concerns. Each router module handles specific functional areas: authentication, conversations, messages, web pages, and search. The frontend console is implemented as vanilla JavaScript modules loaded by templates. The architecture now follows Flask/FastAPI best practices with dedicated modules for different concerns and enhanced modularity.

```mermaid
graph TB
subgraph "FastAPI App - Modular Architecture"
Main["main.py<br/>App initialization & config"]
AuthRouter["routers/auth.py<br/>Authentication endpoints"]
ConvRouter["routers/conversations.py<br/>Conversation management"]
SearchRouter["routers/search.py<br/>Search functionality"]
MsgRouter["routers/messages.py<br/>Message operations"]
WebRouter["routers/web.py<br/>Web page rendering"]
AuthService["services/listing_service.py<br/>Business logic layer"]
AuthMiddleware["auth.py<br/>Session & role validation"]
end
subgraph "Web Assets"
Templates["templates/*.html<br/>Jinja2 templates"]
StaticJS["web/static/console/*.js<br/>Client-side modules"]
I18N["assets/i18n.js<br/>Internationalization"]
end
subgraph "Data & Storage Layer"
Models["db/models.py<br/>SQLAlchemy models"]
Session["db/session.py<br/>Database sessions"]
MediaStorage["media_storage.py<br/>Storage abstraction"]
Thumbnails["media_thumbnails.py<br/>Thumbnail generation"]
Parser["structured_message_parser.py<br/>Message parsing"]
end
Main --> AuthRouter
Main --> ConvRouter
Main --> SearchRouter
Main --> MsgRouter
Main --> WebRouter
Main --> AuthService
Main --> AuthMiddleware
Main --> Templates
Main --> StaticJS
Main --> I18N
AuthRouter --> Models
ConvRouter --> Models
SearchRouter --> Models
MsgRouter --> Models
WebRouter --> Models
AuthService --> Models
Models --> Session
MediaStorage --> Models
Thumbnails --> MediaStorage
Parser --> Models
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [auth.py](file://backend/app/auth.py)
- [web/__init__.py](file://backend/app/web/__init__.py)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)

**Section sources**
- [main.py](file://backend/app/main.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [web/__init__.py](file://backend/app/web/__init__.py)

## Core Components
- **FastAPI Server**: Centralized app initialization with modular router registration, middleware stack configuration, and static/template mounting. Significantly simplified through route extraction to dedicated modules.
- **Authentication Middleware**: Validates sessions, enforces roles, and protects routes with secure cookie handling.
- **Modular Routers**: REST endpoints organized by concern - authentication, conversations, messages, web pages, and search, each in dedicated modules following separation of concerns.
- **Service Layer**: Business logic extracted into dedicated services like the listing service for better maintainability and testability.
- **Template Engine**: Renders HTML pages with context data using Jinja2.
- **Static Asset Management**: Serves CSS/JS for the console and diagnostics with proper caching.
- **Client-Side Console**: Vanilla JS modules for UI interactions, API calls, state management, and real-time updates.
- **Data Layer**: SQLAlchemy models and session configuration with proper connection pooling.
- **Media Pipeline**: Storage abstraction, thumbnails generation, and structured message parsing.

**Section sources**
- [main.py](file://backend/app/main.py)
- [auth.py](file://backend/app/auth.py)
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [web/__init__.py](file://backend/app/web/__init__.py)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)

## Architecture Overview
The FastAPI application exposes HTTP endpoints through modular routers for authentication, conversation retrieval, message operations, and search functionality. Pages are rendered via Jinja2 templates that load static JavaScript modules to build the interactive console. Authentication middleware guards sensitive routes using session cookies and role checks. The service layer provides business logic abstraction, while media assets are served through a storage abstraction that supports local and cloud backends, with thumbnail generation on demand. The modular router architecture ensures clear separation between request handling, business logic, and data access.

```mermaid
sequenceDiagram
participant Browser as "Browser"
participant FastAPI as "FastAPI App"
participant Router as "Modular Router"
participant Service as "Service Layer"
participant DB as "Database"
participant Storage as "Media Storage"
Browser->>FastAPI : GET /login (template)
FastAPI-->>Browser : messages.html
Browser->>FastAPI : POST /auth/login
FastAPI->>Router : authenticate()
Router->>Service : business logic
Service->>DB : verify credentials
DB-->>Service : user record
Service-->>Router : processed result
Router-->>FastAPI : session cookie set
FastAPI-->>Browser : redirect to /console
Browser->>FastAPI : GET /api/messages
FastAPI->>Router : message operations
Router->>Service : listing service
Service->>DB : query messages
DB-->>Service : message list
Service-->>Router : enriched data
Router-->>Browser : JSON
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)

## Detailed Component Analysis

### FastAPI Server Setup and Modular Routing
- Initializes the FastAPI app with significantly reduced complexity due to route extraction to dedicated modules.
- Registers modular routers following separation of concerns principles.
- Mounts static files and configures template directories.
- Applies global middleware including authentication, CORS, and logging.
- Exposes diagnostic and health endpoints for operational visibility.
- **Updated**: Routes are now organized in dedicated modules (messages.py, web.py) following separation of concerns, making main.py much simpler and more maintainable.

```mermaid
flowchart TD
Start(["App Startup"]) --> MountStatic["Mount static directory"]
MountStatic --> MountTemplates["Configure template engine"]
MountTemplates --> RegisterRouters["Register modular routers<br/>(auth, conversations, messages,<br/>web, search)"]
RegisterRouters --> ApplyMiddleware["Apply auth and other middleware"]
ApplyMiddleware --> HealthEndpoints["Expose /health and /diagnostics"]
HealthEndpoints --> Ready(["Server Ready"])
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [web/__init__.py](file://backend/app/web/__init__.py)

**Section sources**
- [main.py](file://backend/app/main.py)
- [web/__init__.py](file://backend/app/web/__init__.py)

### Authentication Middleware and Session Management
- Middleware validates session cookies, extracts user identity and roles, and attaches them to request state.
- Protects routes by requiring specific roles; unauthorized requests receive appropriate responses.
- Login flow sets secure session cookies and redirects authenticated users to the console.
- Integrated with modular routers for consistent authorization across all endpoints.

```mermaid
classDiagram
class AuthMiddleware {
+process_request(request)
+validate_session(cookie)
+attach_user_context(request)
+enforce_roles(request, required_roles)
}
class AuthRouter {
+login(request, credentials)
+logout(request)
+me(request)
}
class SessionStore {
+create_session(user_id, roles)
+get_session(session_id)
+delete_session(session_id)
}
AuthMiddleware --> SessionStore : "reads/writes"
AuthRouter --> SessionStore : "manages lifecycle"
```

**Diagram sources**
- [auth.py](file://backend/app/auth.py)
- [routers/auth.py](file://backend/app/routers/auth.py)

**Section sources**
- [auth.py](file://backend/app/auth.py)
- [routers/auth.py](file://backend/app/routers/auth.py)

### Conversations and Messages
- Provides endpoints to list conversations and fetch message details through dedicated message router.
- Integrates with database models to retrieve tenant-scoped data.
- Supports pagination and filtering parameters.
- **Updated**: Message operations are now handled by the dedicated messages router module, providing clear separation from other concerns and improved maintainability.

```mermaid
sequenceDiagram
participant Browser as "Console JS"
participant MsgRouter as "Messages Router"
participant Service as "Listing Service"
participant DB as "Database"
participant Parser as "Structured Message Parser"
Browser->>MsgRouter : GET /api/messages?tenant_id=...
MsgRouter->>Service : process listing request
Service->>DB : query messages
DB-->>Service : message list
Service-->>MsgRouter : enriched data
MsgRouter-->>Browser : JSON
Browser->>MsgRouter : GET /api/messages/{id}/details
MsgRouter->>DB : query message details
DB-->>MsgRouter : message data
MsgRouter->>Parser : parse structured content
Parser-->>MsgRouter : enriched message
MsgRouter-->>Browser : JSON
```

**Diagram sources**
- [routers/messages.py](file://backend/app/routers/messages.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [db/models.py](file://backend/app/db/models.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)

**Section sources**
- [routers/messages.py](file://backend/app/routers/messages.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [db/models.py](file://backend/app/db/models.py)
- [structured_message_parser.py](file://backend/app/structured_message_parser.py)

### Search Functionality
- Exposes a search endpoint supporting queries and filters.
- Uses indexed fields for efficient lookups across messages and metadata.
- Returns paginated results suitable for client-side rendering.
- Integrated into the modular router architecture for consistent request handling.

```mermaid
flowchart TD
Query["Client sends search query"] --> Validate["Validate parameters"]
Validate --> BuildQuery["Build DB query"]
BuildQuery --> Execute["Execute search"]
Execute --> Results{"Results found?"}
Results --> |Yes| Enrich["Enrich with metadata"]
Results --> |No| Empty["Return empty set"]
Enrich --> Paginate["Apply pagination"]
Paginate --> Return["Return JSON"]
Empty --> Return
```

**Diagram sources**
- [routers/search.py](file://backend/app/routers/search.py)
- [db/models.py](file://backend/app/db/models.py)

**Section sources**
- [routers/search.py](file://backend/app/routers/search.py)
- [db/models.py](file://backend/app/db/models.py)

### Template Rendering and Static Assets
- Templates render HTML pages with embedded i18n scripts and module loaders.
- Static assets include CSS and JS modules for the console, diagnostics, and search UI.
- Templates pass context variables such as user roles, locale, and feature flags.
- Web router handles page-specific rendering logic in dedicated module.

```mermaid
graph LR
Templates["messages.html"] --> LoadJS["Load console-entry.js"]
Templates --> LoadCSS["Load base.css"]
Templates --> I18N["Load i18n.js"]
Review["review_console.html"] --> LoadJS
SearchPage["search.html"] --> LoadJS
DiagnosticsPage["diagnostics.html"] --> LoadDiagnosticsJS["Load diagnostics.js"]
```

**Diagram sources**
- [templates/messages.html](file://backend/app/web/templates/messages.html)
- [templates/review_console.html](file://backend/app/web/templates/review_console.html)
- [templates/search.html](file://backend/app/web/templates/search.html)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

**Section sources**
- [templates/messages.html](file://backend/app/web/templates/messages.html)
- [templates/review_console.html](file://backend/app/web/templates/review_console.html)
- [templates/search.html](file://backend/app/web/templates/search.html)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

### Client-Side JavaScript Framework (Vanilla Modules)
- Entry point initializes modules, handles routing, and manages global state.
- API client abstracts HTTP calls, error handling, and retries.
- Conversation list renders paginated lists with filters and refresh controls.
- Media viewer loads images/videos and handles fallbacks.
- Message renderers format different message types and structured content.
- Timeline component visualizes chronological events.

```mermaid
classDiagram
class ConsoleEntry {
+init()
+setupRoutes()
+loadModules()
}
class ApiClient {
+get(url, params)
+post(url, body)
+handleError(response)
}
class ConversationList {
+render(conversations)
+filter(query)
+refresh()
}
class MediaViewer {
+open(mediaId)
+showThumbnail(id)
+fallbackOnError()
}
class MessageRenderers {
+renderText(msg)
+renderImage(msg)
+renderStructured(msg)
}
class Timeline {
+appendEvent(event)
+clear()
}
ConsoleEntry --> ApiClient : "uses"
ConsoleEntry --> ConversationList : "creates"
ConsoleEntry --> MediaViewer : "creates"
ConsoleEntry --> MessageRenderers : "uses"
ConsoleEntry --> Timeline : "updates"
```

**Diagram sources**
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)
- [static/console/conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [static/console/media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [static/console/message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [static/console/timeline.js](file://backend/app/web/static/console/timeline.js)

**Section sources**
- [static/console/console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)
- [static/console/conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [static/console/media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [static/console/message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [static/console/timeline.js](file://backend/app/web/static/console/timeline.js)

### Internationalization Support
- i18n script provides translation lookup and dynamic locale switching.
- Templates inject locale-specific strings into the page context.
- Client modules consume i18n functions to render localized UI text.

```mermaid
flowchart TD
SetLocale["Set locale from header or user preference"] --> LoadTranslations["Load translations bundle"]
LoadTranslations --> TFunction["Expose t(key) function"]
TFunction --> RenderUI["Render UI with translated strings"]
```

**Diagram sources**
- [assets/i18n.js](file://backend/app/assets/i18n.js)

**Section sources**
- [assets/i18n.js](file://backend/app/assets/i18n.js)

### Real-Time Update Mechanisms
- Console uses periodic polling or event-driven updates to refresh conversation lists and timelines.
- Refresh utilities trigger re-fetches and incremental updates without full page reloads.

```mermaid
sequenceDiagram
participant Timer as "Refresh Timer"
participant Console as "Console JS"
participant API as "Backend API"
Timer->>Console : tick()
Console->>API : GET /api/conversations (incremental)
API-->>Console : diff/new items
Console->>Console : update timeline and list
```

**Diagram sources**
- [static/console/refresh.js](file://backend/app/web/static/console/refresh.js)
- [static/console/timeline.js](file://backend/app/web/static/console/timeline.js)

**Section sources**
- [static/console/refresh.js](file://backend/app/web/static/console/refresh.js)
- [static/console/timeline.js](file://backend/app/web/static/console/timeline.js)

### Responsive Design Patterns
- Base CSS defines responsive grids, typography scales, and mobile-first layouts.
- Console modules adapt layouts based on viewport size and device capabilities.

```mermaid
flowchart TD
DetectViewport["Detect viewport size"] --> ApplyStyles["Apply responsive CSS classes"]
ApplyStyles --> AdjustLayout["Adjust grid and navigation"]
AdjustLayout --> OptimizeAssets["Lazy-load heavy assets"]
```

**Diagram sources**
- [static/base.css](file://backend/app/web/static/base.css)

**Section sources**
- [static/base.css](file://backend/app/web/static/base.css)

### Diagnostics and Health Check Endpoints
- Health endpoint returns service status and dependencies readiness.
- Diagnostics page aggregates logs, metrics, and runtime information for troubleshooting.
- Integrated into the modular router architecture for consistent endpoint handling.

```mermaid
sequenceDiagram
participant Admin as "Admin Browser"
participant FastAPI as "FastAPI App"
participant Health as "Health Endpoint"
participant Diag as "Diagnostics Page"
Admin->>FastAPI : GET /health
FastAPI->>Health : check services
Health-->>FastAPI : status JSON
FastAPI-->>Admin : 200 OK
Admin->>FastAPI : GET /diagnostics
FastAPI->>Diag : render diagnostics.html
Diag-->>Admin : interactive dashboard
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

**Section sources**
- [main.py](file://backend/app/main.py)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

### Error Handling Strategies
- Global exception handlers return consistent error formats for API and HTML contexts.
- Client-side error handling displays user-friendly messages and retry options.
- Logging captures stack traces and contextual data for debugging.
- Modular router structure enables consistent error handling across all endpoints.

```mermaid
flowchart TD
Exception["Exception occurs"] --> Classify["Classify error type"]
Classify --> APIContext{"API response?"}
APIContext --> |Yes| JSONError["Return JSON error"]
APIContext --> |No| HTMLError["Render error template"]
JSONError --> Log["Log details"]
HTMLError --> Log
Log --> Notify["Notify monitoring if critical"]
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)

**Section sources**
- [main.py](file://backend/app/main.py)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)

### Performance Monitoring Integration
- Metrics collection points capture request latency, error rates, and resource usage.
- Middleware instruments endpoints and stores metrics for external collectors.
- Diagnostics page exposes key metrics for quick inspection.
- Modular architecture enables granular performance monitoring per router.

```mermaid
graph TB
Request["Incoming Request"] --> Instrument["Instrument middleware"]
Instrument --> CollectMetrics["Collect latency/errors"]
CollectMetrics --> Store["Store in metrics store"]
Store --> Expose["Expose via /diagnostics"]
```

**Diagram sources**
- [main.py](file://backend/app/main.py)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

**Section sources**
- [main.py](file://backend/app/main.py)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

## Service Layer Architecture
The application now includes a dedicated service layer that encapsulates business logic, providing better separation of concerns and testability. The listing service specifically handles complex data operations and business rules for conversation and message listings. The modular router architecture ensures that routers focus on request/response handling while delegating business logic to services.

```mermaid
classDiagram
class ListingService {
+list_conversations(tenant_id, filters)
+list_messages(tenant_id, conversation_id, params)
+get_message_details(message_id)
+apply_filters(data, filters)
+paginate_results(data, page, per_page)
}
class MessageRouter {
+get_messages(request)
+get_message_detail(request)
+validate_params(params)
}
class ConversationRouter {
+list_conversations(request)
+get_conversation_detail(request)
}
MessageRouter --> ListingService : "delegates business logic"
ConversationRouter --> ListingService : "delegates business logic"
ListingService --> Database : "data access"
```

**Diagram sources**
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)

**Section sources**
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)

## Dependency Analysis
The application exhibits clear layering with enhanced separation: routers depend on service layer which depends on models and storage abstractions; templates depend on static assets; client modules depend on the API client and i18n utilities. The service layer acts as an intermediary between routers and data access, improving maintainability and testability. The modular router architecture ensures that each router has well-defined dependencies and responsibilities.

```mermaid
graph TB
Routers["Routers<br/>(auth, conversations,<br/>messages, web, search)"] --> Services["Service Layer"]
Services --> Models["Models"]
Routers --> Storage["Media Storage"]
Templates["Templates"] --> Static["Static Assets"]
ConsoleJS["Console JS"] --> API["API Client"]
ConsoleJS --> I18N["i18n.js"]
Models --> Session["DB Session"]
Storage --> Thumbnails["Thumbnails"]
Services --> DataAccess["Data Access Layer"]
```

**Diagram sources**
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)

**Section sources**
- [routers/auth.py](file://backend/app/routers/auth.py)
- [routers/conversations.py](file://backend/app/routers/conversations.py)
- [routers/search.py](file://backend/app/routers/search.py)
- [routers/messages.py](file://backend/app/routers/messages.py)
- [routers/web.py](file://backend/app/routers/web.py)
- [services/listing_service.py](file://backend/app/services/listing_service.py)
- [db/models.py](file://backend/app/db/models.py)
- [db/session.py](file://backend/app/db/session.py)
- [media_storage.py](file://backend/app/media_storage.py)
- [media_thumbnails.py](file://backend/app/media_thumbnails.py)
- [assets/i18n.js](file://backend/app/assets/i18n.js)
- [static/console/api-client.js](file://backend/app/web/static/console/api-client.js)

## Performance Considerations
- Use pagination and filtering on all list endpoints to reduce payload sizes.
- Implement caching for frequently accessed resources like conversation lists and search results.
- Lazy-load heavy assets (images, videos) and use thumbnails where possible.
- Monitor and optimize database queries with proper indexing and joins.
- Leverage browser caching headers for static assets and media.
- **Updated**: Service layer enables better caching strategies and business logic optimization. Modular router architecture allows for targeted performance monitoring and optimization per functional area.

## Troubleshooting Guide
- Verify health endpoint responds with expected status codes and dependency checks.
- Inspect diagnostics page for runtime metrics and logs.
- Check browser console for JavaScript errors and network failures.
- Validate session cookies and role claims when encountering authorization issues.
- Review server logs for stack traces and error context.
- **Updated**: Check service layer logs for business logic errors and data processing issues. Modular router structure makes it easier to isolate and debug specific functional areas.

**Section sources**
- [main.py](file://backend/app/main.py)
- [templates/diagnostics.html](file://backend/app/web/templates/diagnostics.html)
- [static/diagnostics.js](file://backend/app/web/static/diagnostics.js)

## Conclusion
The web application combines a robust FastAPI backend with a modular vanilla JavaScript console to deliver a responsive, internationalized, and secure experience. The recent architectural restructuring with separated concerns, dedicated router modules (messages.py, web.py), and service layer extraction follows Flask/FastAPI best practices, significantly reducing main.py complexity and improving maintainability and scalability. Authentication middleware ensures role-based access, while media pipelines and structured message parsing enrich the user interface. Diagnostics and health endpoints provide operational visibility, and performance monitoring integrates seamlessly for proactive maintenance. The new service layer architecture enables better testing, caching, and business logic management. The modular router approach creates clear boundaries between request handling, business logic, and data access, making the codebase more maintainable and scalable.