# Client-Side Architecture

<cite>
**Referenced Files in This Document**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [refresh.js](file://backend/app/web/static/console/refresh.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)
- [search.js](file://backend/app/web/static/search.js)
- [i18n.js](file://backend/app/assets/i18n.js)
- [display_names.py](file://backend/app/display_names.py)
- [i18n_assets.py](file://backend/app/i18n_assets.py)
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
This document explains the client-side architecture of the WeCom Archive application, focusing on JavaScript module organization, API client behavior, and communication patterns with the backend. It also covers CSS architecture, responsive design principles, styling conventions, display name resolution, internationalization support, and localization features. Practical examples are provided for extending the API client, implementing new UI components, and customizing appearance and behavior.

## Project Structure
The client-side code is organized as a set of small, focused JavaScript modules under the static console directory, paired with shared CSS files. The entry point initializes the application state, wires up UI modules, and starts background refresh tasks. A separate search page uses its own script to handle query interactions.

```mermaid
graph TB
Entry["console-entry.js"] --> State["console-state.js"]
Entry --> API["api-client.js"]
Entry --> ConvList["conversation-list.js"]
Entry --> Timeline["timeline.js"]
Entry --> MediaViewer["media-viewer.js"]
Entry --> MsgRenderers["message-renderers.js"]
Entry --> Refresh["refresh.js"]
SearchPage["search.js"] --> API
BaseCSS["base.css"] --> AllModules["All Modules"]
DiagCSS["diagnostics.css"] --> AllModules
I18N["i18n.js"] --> AllModules
DisplayNames["display_names.py"] --> AllModules
I18nAssets["i18n_assets.py"] --> AllModules
```

**Diagram sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [refresh.js](file://backend/app/web/static/console/refresh.js)
- [search.js](file://backend/app/web/static/search.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)
- [i18n.js](file://backend/app/assets/i18n.js)
- [display_names.py](file://backend/app/display_names.py)
- [i18n_assets.py](file://backend/app/i18n_assets.py)

**Section sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [search.js](file://backend/app/web/static/search.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)
- [i18n.js](file://backend/app/assets/i18n.js)

## Core Components
- Console entrypoint: Initializes global state, registers event handlers, and bootstraps UI modules.
- API client: Centralized HTTP client that encapsulates request building, error handling, retries, and response normalization.
- Application state: Holds current conversation context, message lists, filters, and UI flags.
- Conversation list: Renders and manages selection of conversations.
- Timeline: Displays messages chronologically with pagination and incremental updates.
- Message renderers: Converts raw message payloads into DOM fragments based on message type.
- Media viewer: Handles image/video playback and thumbnails.
- Refresh manager: Schedules periodic data refreshes and handles network errors gracefully.
- Search page: Provides query input, result rendering, and filtering.

Key responsibilities and interactions are illustrated below.

```mermaid
sequenceDiagram
participant User as "User"
participant Entry as "console-entry.js"
participant State as "console-state.js"
participant API as "api-client.js"
participant Backend as "Backend API"
participant Conv as "conversation-list.js"
participant TL as "timeline.js"
participant MR as "message-renderers.js"
User->>Entry : Open console page
Entry->>State : Initialize state
Entry->>Conv : Render conversation list
Conv->>API : Fetch conversations
API->>Backend : GET /conversations
Backend-->>API : JSON payload
API-->>Conv : Normalized data
Conv-->>TL : Selected conversation ID
TL->>API : Fetch timeline messages
API->>Backend : GET /messages?conversation_id=...
Backend-->>API : JSON payload
API-->>TL : Normalized data
TL->>MR : Render messages
MR-->>TL : DOM fragments
TL-->>User : Updated timeline
```

**Diagram sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)

**Section sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)

## Architecture Overview
The client follows a modular, single-page approach where each feature is encapsulated in its own module. The API client abstracts all network calls, providing consistent error handling and retry policies. UI modules consume normalized responses and update the DOM incrementally. Styling is centralized in CSS files with component-scoped classes and responsive utilities. Internationalization is supported via a dedicated i18n module and backend-provided assets.

```mermaid
graph TB
subgraph "Console Page"
CE["console-entry.js"]
CS["console-state.js"]
CL["conversation-list.js"]
TL["timeline.js"]
MV["media-viewer.js"]
MR["message-renderers.js"]
RF["refresh.js"]
end
subgraph "Search Page"
SJ["search.js"]
end
subgraph "Styling"
BCSS["base.css"]
DCSS["diagnostics.css"]
end
subgraph "I18n"
I18NJS["i18n.js"]
I18NASSETS["i18n_assets.py"]
end
CE --> CS
CE --> CL
CE --> TL
CE --> MV
CE --> MR
CE --> RF
SJ --> CE
CL --> I18NJS
TL --> I18NJS
MR --> I18NJS
CL --> BCSS
TL --> BCSS
MV --> DCSS
CE --> I18NASSETS
```

**Diagram sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [refresh.js](file://backend/app/web/static/console/refresh.js)
- [search.js](file://backend/app/web/static/search.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)
- [i18n.js](file://backend/app/assets/i18n.js)
- [i18n_assets.py](file://backend/app/i18n_assets.py)

## Detailed Component Analysis

### API Client Implementation
The API client centralizes HTTP operations, including:
- Request construction with headers and parameters
- Error detection and user-friendly messages
- Retry logic for transient failures
- Response normalization for consistent consumption by UI modules

Extending the API client:
- Add new endpoints by defining typed methods that wrap fetch or XMLHttpRequest
- Implement retry strategies per endpoint if needed (e.g., higher retry for search queries)
- Normalize responses to a common shape with status, data, and error fields

```mermaid
flowchart TD
Start(["Call API Method"]) --> BuildReq["Build Request<br/>Headers + Params"]
BuildReq --> SendReq["Send HTTP Request"]
SendReq --> RespOK{"Response OK?"}
RespOK --> |Yes| Normalize["Normalize Payload"]
RespOK --> |No| HandleErr["Handle Error<br/>Retry or Fail"]
Normalize --> ReturnData["Return Data to Caller"]
HandleErr --> ReturnError["Return Error to Caller"]
```

**Diagram sources**
- [api-client.js](file://backend/app/web/static/console/api-client.js)

**Section sources**
- [api-client.js](file://backend/app/web/static/console/api-client.js)

### Application State Management
Application state holds:
- Current conversation ID and metadata
- Message list segments and pagination tokens
- UI flags such as loading states and active filters

Best practices:
- Keep state minimal and immutable where possible
- Provide setters that trigger re-renders only for affected parts
- Persist critical state across sessions using storage APIs when appropriate

```mermaid
classDiagram
class AppState {
+string currentConversationId
+Array messages
+object pagination
+boolean isLoading
+object filters
+setConversation(id)
+appendMessages(items)
+updateFilters(filters)
+reset()
}
```

**Diagram sources**
- [console-state.js](file://backend/app/web/static/console/console-state.js)

**Section sources**
- [console-state.js](file://backend/app/web/static/console/console-state.js)

### Conversation List Module
Responsibilities:
- Fetch and render conversation entries
- Handle selection events and propagate to timeline
- Support search and filtering within the list

Integration points:
- Uses API client to retrieve conversation data
- Consumes i18n strings for labels and statuses
- Updates CSS classes for active states and responsive layouts

```mermaid
sequenceDiagram
participant CL as "conversation-list.js"
participant API as "api-client.js"
participant State as "console-state.js"
participant TL as "timeline.js"
CL->>API : getConversations(params)
API-->>CL : conversations[]
CL->>CL : Render list items
CL->>State : setCurrentConversation(id)
CL->>TL : notifySelection(id)
```

**Diagram sources**
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)

**Section sources**
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)

### Timeline Module
Responsibilities:
- Load messages for a selected conversation
- Paginate and append new messages
- Coordinate with message renderers to produce DOM fragments

Behavioral flow:
- On selection, clear previous content and start loading
- Fetch initial batch, then schedule incremental loads
- Handle empty states and error states with user feedback

```mermaid
flowchart TD
Sel["Selected Conversation"] --> Clear["Clear Previous Content"]
Clear --> LoadBatch["Load Initial Batch"]
LoadBatch --> HasMore{"Has More?"}
HasMore --> |Yes| Append["Append Messages"]
Append --> ScheduleNext["Schedule Next Batch"]
HasMore --> |No| Done["Done"]
ScheduleNext --> Done
```

**Diagram sources**
- [timeline.js](file://backend/app/web/static/console/timeline.js)

**Section sources**
- [timeline.js](file://backend/app/web/static/console/timeline.js)

### Message Renderers
Responsibilities:
- Detect message types and render corresponding DOM structures
- Inject localized text and timestamps
- Attach event listeners for media playback and actions

Extension pattern:
- Register new renderers for additional message types
- Ensure accessibility attributes and keyboard navigation
- Use CSS classes scoped to message type for styling

```mermaid
classDiagram
class MessageRenderer {
+render(message) DOMFragment
+registerHandler(type, handler)
+getLocalizedText(key) string
}
```

**Diagram sources**
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [i18n.js](file://backend/app/assets/i18n.js)

**Section sources**
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [i18n.js](file://backend/app/assets/i18n.js)

### Media Viewer
Responsibilities:
- Display images and videos with thumbnails
- Handle lazy loading and fallbacks
- Provide zoom and fullscreen controls

Responsive considerations:
- Adapt layout for mobile screens
- Optimize media sizes based on viewport
- Defer heavy operations until visible

```mermaid
sequenceDiagram
participant MV as "media-viewer.js"
participant API as "api-client.js"
participant DOM as "DOM"
MV->>API : getMediaUrl(mediaId)
API-->>MV : signed URL
MV->>DOM : Create img/video element
MV->>DOM : Attach events (zoom, fullscreen)
MV-->>User : Media displayed
```

**Diagram sources**
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)

**Section sources**
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)

### Refresh Manager
Responsibilities:
- Schedule periodic refreshes for conversations and timelines
- Backoff on errors and pause during inactivity
- Provide manual refresh triggers

```mermaid
flowchart TD
Start(["Start Refresh Cycle"]) --> CheckActive{"Is Page Active?"}
CheckActive --> |No| Pause["Pause Until Active"]
CheckActive --> |Yes| Fetch["Fetch Latest Data"]
Fetch --> Success{"Success?"}
Success --> |Yes| UpdateUI["Update UI Incrementally"]
Success --> |No| Backoff["Apply Backoff Strategy"]
UpdateUI --> Wait["Wait Interval"]
Backoff --> Wait
Wait --> Start
```

**Diagram sources**
- [refresh.js](file://backend/app/web/static/console/refresh.js)

**Section sources**
- [refresh.js](file://backend/app/web/static/console/refresh.js)

### Search Page
Responsibilities:
- Capture user queries and send them to the backend
- Render results with pagination and filters
- Integrate with i18n for labels and messages

```mermaid
sequenceDiagram
participant User as "User"
participant SJ as "search.js"
participant API as "api-client.js"
participant Backend as "Backend API"
User->>SJ : Enter query
SJ->>API : search(query, filters)
API->>Backend : GET /search?q=...
Backend-->>API : results[]
API-->>SJ : Normalized results
SJ->>SJ : Render results
SJ-->>User : Updated search view
```

**Diagram sources**
- [search.js](file://backend/app/web/static/search.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)

**Section sources**
- [search.js](file://backend/app/web/static/search.js)

## Dependency Analysis
The client modules have clear dependencies:
- console-entry.js orchestrates initialization and wiring
- api-client.js is consumed by all data-fetching modules
- console-state.js provides shared state to UI modules
- i18n.js supplies localized strings across modules
- CSS files provide shared styles and responsive utilities

```mermaid
graph LR
CE["console-entry.js"] --> CS["console-state.js"]
CE --> CL["conversation-list.js"]
CE --> TL["timeline.js"]
CE --> MV["media-viewer.js"]
CE --> MR["message-renderers.js"]
CE --> RF["refresh.js"]
CL --> API["api-client.js"]
TL --> API
MV --> API
MR --> I18N["i18n.js"]
CL --> I18N
TL --> I18N
CE --> BCSS["base.css"]
MV --> DCSS["diagnostics.css"]
```

**Diagram sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [console-state.js](file://backend/app/web/static/console/console-state.js)
- [conversation-list.js](file://backend/app/web/static/console/conversation-list.js)
- [timeline.js](file://backend/app/web/static/console/timeline.js)
- [media-viewer.js](file://backend/app/web/static/console/media-viewer.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)
- [refresh.js](file://backend/app/web/static/console/refresh.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [i18n.js](file://backend/app/assets/i18n.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)

**Section sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [i18n.js](file://backend/app/assets/i18n.js)
- [base.css](file://backend/app/web/static/base.css)
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)

## Performance Considerations
- Lazy load media and defer non-critical scripts to improve initial paint
- Use pagination and virtual scrolling for large message lists
- Cache frequently accessed data in memory and persist where appropriate
- Debounce user inputs (e.g., search) to reduce network requests
- Apply backoff and retry strategies for resilient network operations

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- Network errors: Inspect API client error handling and retry configuration
- Missing translations: Verify i18n keys and asset loading
- Rendering bugs: Check message renderer registration and CSS class usage
- Memory leaks: Ensure event listeners are removed when components unmount
- Slow performance: Profile DOM updates and optimize re-renders

**Section sources**
- [api-client.js](file://backend/app/web/static/console/api-client.js)
- [i18n.js](file://backend/app/assets/i18n.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)

## Conclusion
The client-side architecture is modular and maintainable, with clear separation of concerns between networking, state, UI modules, and styling. The API client standardizes communication with the backend, while i18n and CSS ensure localization and responsive design. Extensibility is straightforward through registered handlers and typed API methods.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Extending the API Client
- Define new endpoint methods with consistent signatures
- Implement retry policies tailored to endpoint characteristics
- Normalize responses to include status, data, and error fields

**Section sources**
- [api-client.js](file://backend/app/web/static/console/api-client.js)

### Implementing New UI Components
- Create a new module under the console directory
- Wire it up in the entrypoint and subscribe to state changes
- Use i18n for labels and follow CSS naming conventions

**Section sources**
- [console-entry.js](file://backend/app/web/static/console/console-entry.js)
- [i18n.js](file://backend/app/assets/i18n.js)
- [base.css](file://backend/app/web/static/base.css)

### Customizing Appearance and Behavior
- Override CSS variables and component classes in diagnostics.css
- Adjust refresh intervals and retry policies in refresh.js
- Extend message renderers for new message types

**Section sources**
- [diagnostics.css](file://backend/app/web/static/diagnostics.css)
- [refresh.js](file://backend/app/web/static/console/refresh.js)
- [message-renderers.js](file://backend/app/web/static/console/message-renderers.js)

### Display Name Resolution System
Display names are resolved by combining contact information from the backend with local overrides. The system ensures consistent labeling across conversations and messages.

```mermaid
flowchart TD
Input["Contact ID"] --> Lookup["Lookup Contact Info"]
Lookup --> HasName{"Has Display Name?"}
HasName --> |Yes| UseName["Use Provided Name"]
HasName --> |No| Fallback["Fallback to Default Format"]
UseName --> Output["Resolved Display Name"]
Fallback --> Output
```

**Diagram sources**
- [display_names.py](file://backend/app/display_names.py)

**Section sources**
- [display_names.py](file://backend/app/display_names.py)

### Internationalization Support and Localization Features
The i18n module provides key-based translation lookups, pluralization, and date formatting. Backend assets supply language packs and locale-specific resources.

```mermaid
sequenceDiagram
participant UI as "UI Module"
participant I18N as "i18n.js"
participant Assets as "i18n_assets.py"
UI->>I18N : t("key", params)
I18N->>Assets : Load locale bundle
Assets-->>I18N : Translations map
I18N-->>UI : Localized string
```

**Diagram sources**
- [i18n.js](file://backend/app/assets/i18n.js)
- [i18n_assets.py](file://backend/app/i18n_assets.py)

**Section sources**
- [i18n.js](file://backend/app/assets/i18n.js)
- [i18n_assets.py](file://backend/app/i18n_assets.py)