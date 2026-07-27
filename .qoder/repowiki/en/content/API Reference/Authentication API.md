# Authentication API

<cite>
**Referenced Files in This Document**
- [auth.py](file://backend/app/routers/auth.py)
- [auth.py](file://backend/app/auth.py)
- [main.py](file://backend/app/main.py)
- [models.py](file://backend/app/db/models.py)
- [schema_check.py](file://backend/app/db/schema_check.py)
- [test_auth.py](file://backend/tests/test_auth.py)
- [test_password_auth.py](file://backend/tests/test_password_auth.py)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Authentication Overview](#authentication-overview)
3. [API Endpoints](#api-endpoints)
4. [Request/Response Schemas](#requestresponse-schemas)
5. [Security Headers](#security-headers)
6. [Token Management](#token-management)
7. [Multi-Tenant Support](#multi-tenant-support)
8. [Error Handling](#error-handling)
9. [Authentication Flow Examples](#authentication-flow-examples)
10. [Best Practices](#best-practices)

## Introduction

The Authentication API provides secure user authentication, session management, and token-based authorization for the WeCom Archive system. It supports multi-tenant environments with JWT token management and comprehensive error handling for various authentication scenarios.

## Authentication Overview

The authentication system implements a modern security architecture with:

- **JWT Token-based Authentication**: Stateless authentication using JSON Web Tokens
- **Multi-Tenant Support**: Tenant isolation and scoped access control
- **Password Validation**: Strong password policies and validation rules
- **Session Management**: Secure session handling with token expiration
- **Error Handling**: Comprehensive error responses for various authentication failures

```mermaid
sequenceDiagram
participant Client as "Client Application"
participant AuthAPI as "Auth API"
participant DB as "Database"
participant TokenService as "Token Service"
Client->>AuthAPI : POST /api/auth/login
AuthAPI->>DB : Validate credentials
DB-->>AuthAPI : User data
AuthAPI->>TokenService : Generate JWT token
TokenService-->>AuthAPI : JWT token
AuthAPI-->>Client : {access_token, refresh_token, expires_in}
Note over Client,AuthAPI : Subsequent requests include Authorization header
Client->>AuthAPI : GET /api/protected-resource
Client->>AuthAPI : Authorization : Bearer <token>
AuthAPI->>TokenService : Validate token
TokenService-->>AuthAPI : Valid/Invalid
AuthAPI-->>Client : Resource data or 401 Unauthorized
```

**Diagram sources**
- [auth.py:1-100](file://backend/app/routers/auth.py#L1-L100)
- [auth.py:1-150](file://backend/app/auth.py#L1-L150)

## API Endpoints

### Login Endpoint

**POST** `/api/auth/login`

Authenticates a user with username and password credentials.

#### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | Yes | User's login username |
| `password` | string | Yes | User's password |
| `tenant_id` | string | No | Tenant identifier for multi-tenant support |

#### Response Schema

**Success (200 OK)**

| Field | Type | Description |
|-------|------|-------------|
| `access_token` | string | JWT access token |
| `refresh_token` | string | Refresh token for token renewal |
| `expires_in` | integer | Token expiration time in seconds |
| `token_type` | string | Always "Bearer" |
| `user_info` | object | Basic user information |

**Error Responses**

| Status Code | Description | Response Schema |
|-------------|-------------|-----------------|
| 400 | Bad Request | `{ "error": "validation_error", "message": "..." }` |
| 401 | Unauthorized | `{ "error": "invalid_credentials", "message": "Invalid username or password" }` |
| 403 | Forbidden | `{ "error": "account_disabled", "message": "Account is disabled" }` |
| 429 | Too Many Requests | `{ "error": "rate_limited", "message": "Too many login attempts" }` |

### Logout Endpoint

**POST** `/api/auth/logout`

Invalidates the current user session and tokens.

#### Request Schema

No request body required. Requires valid Authorization header.

#### Response Schema

**Success (200 OK)**

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | "Successfully logged out" |
| `status` | string | "success" |

**Error Responses**

| Status Code | Description | Response Schema |
|-------------|-------------|-----------------|
| 401 | Unauthorized | `{ "error": "unauthorized", "message": "Not authenticated" }` |
| 403 | Forbidden | `{ "error": "invalid_token", "message": "Invalid or expired token" }` |

### Token Refresh Endpoint

**POST** `/api/auth/refresh`

Refreshes an expired access token using a valid refresh token.

#### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `refresh_token` | string | Yes | Valid refresh token |

#### Response Schema

**Success (200 OK)**

| Field | Type | Description |
|-------|------|-------------|
| `access_token` | string | New JWT access token |
| `expires_in` | integer | New token expiration time |
| `token_type` | string | Always "Bearer" |

**Error Responses**

| Status Code | Description | Response Schema |
|-------------|-------------|-----------------|
| 400 | Bad Request | `{ "error": "missing_refresh_token", "message": "Refresh token required" }` |
| 401 | Unauthorized | `{ "error": "invalid_refresh_token", "message": "Invalid or expired refresh token" }` |

### Password Change Endpoint

**POST** `/api/auth/password/change`

Changes the authenticated user's password.

#### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `current_password` | string | Yes | Current password for verification |
| `new_password` | string | Yes | New password meeting validation requirements |
| `confirm_password` | string | Yes | Confirmation of new password |

#### Response Schema

**Success (200 OK)**

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | "Password changed successfully" |
| `status` | string | "success" |

**Error Responses**

| Status Code | Description | Response Schema |
|-------------|-------------|-----------------|
| 400 | Bad Request | `{ "error": "validation_error", "message": "Password validation failed" }` |
| 401 | Unauthorized | `{ "error": "unauthorized", "message": "Not authenticated" }` |
| 403 | Forbidden | `{ "error": "invalid_current_password", "message": "Current password is incorrect" }` |

### Password Reset Endpoint

**POST** `/api/auth/password/reset`

Initiates password reset process for users who forgot their password.

#### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | User's registered email address |
| `tenant_id` | string | No | Tenant identifier for multi-tenant support |

#### Response Schema

**Success (200 OK)**

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | "Password reset instructions sent" |
| `status` | string | "success" |

**Error Responses**

| Status Code | Description | Response Schema |
|-------------|-------------|-----------------|
| 400 | Bad Request | `{ "error": "validation_error", "message": "Invalid email format" }` |
| 404 | Not Found | `{ "error": "user_not_found", "message": "No account found with this email" }` |

## Request/Response Schemas

### Common Response Format

All API responses follow a consistent format:

```json
{
  "status": "success" | "error",
  "message": "Human-readable message",
  "data": {},
  "errors": []
}
```

### JWT Token Structure

Access tokens contain the following claims:

| Claim | Type | Description |
|-------|------|-------------|
| `sub` | string | User ID |
| `username` | string | Username |
| `tenant_id` | string | Tenant identifier |
| `roles` | array | User roles and permissions |
| `exp` | number | Expiration timestamp |
| `iat` | number | Issued at timestamp |
| `jti` | string | Unique token identifier |

### Password Validation Rules

Passwords must meet the following requirements:

- Minimum length: 8 characters
- Maximum length: 128 characters
- Must contain at least one uppercase letter
- Must contain at least one lowercase letter
- Must contain at least one number
- Must contain at least one special character
- Cannot be the same as the last 3 passwords
- Cannot be a commonly used password

## Security Headers

The authentication API sets the following security headers:

| Header | Value | Description |
|--------|-------|-------------|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME type sniffing |
| `X-Frame-Options` | `DENY` | Prevents clickjacking attacks |
| `X-XSS-Protection` | `1; mode=block` | Enables XSS protection |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | Enforces HTTPS |
| `Cache-Control` | `no-store, no-cache, must-revalidate` | Prevents caching of sensitive data |
| `Pragma` | `no-cache` | HTTP/1.0 cache prevention |

## Token Management

### Token Lifecycle

```mermaid
stateDiagram-v2
[*] --> Created : User Login
Created --> Active : Token Used
Active --> Valid : Within Expiry
Active --> Expired : After Expiry
Expired --> Refreshed : Using Refresh Token
Refreshed --> Active : New Token Generated
Active --> Revoked : User Logout
Active --> Invalidated : Security Event
Revoked --> [*]
Invalidated --> [*]
```

### Token Expiration Policies

| Token Type | Default Expiration | Max Expiration | Refreshable |
|------------|-------------------|----------------|-------------|
| Access Token | 15 minutes | 1 hour | Yes |
| Refresh Token | 7 days | 30 days | No |
| Session Token | 24 hours | 7 days | Yes |

### Token Storage

Tokens are stored securely using:

- **HTTP-only Cookies**: For browser-based applications
- **Memory Storage**: For server-side processing
- **Secure Redis Cache**: For distributed token validation
- **Encrypted Storage**: For long-lived refresh tokens

## Multi-Tenant Support

The authentication system supports multi-tenant environments with tenant-scoped access control:

### Tenant Identification

Tenants can be identified through:

- Request header: `X-Tenant-ID`
- JWT claim: `tenant_id`
- Domain-based routing
- Query parameter: `?tenant_id=`

### Tenant Isolation

Each tenant has isolated:

- User accounts and credentials
- Authentication sessions
- Token scopes and permissions
- Audit logs and access records

### Cross-Tenant Access Control

```mermaid
flowchart TD
A[Login Request] --> B{Tenant Specified?}
B --> |Yes| C[Validate Tenant Access]
B --> |No| D[Use Default Tenant]
C --> E{Valid Tenant?}
E --> |Yes| F[Create Tenant-Scoped Token]
E --> |No| G[Return 403 Forbidden]
D --> F
F --> H[Authenticate User]
H --> I[Generate Scoped JWT]
I --> J[Return Success Response]
```

## Error Handling

### Standard Error Codes

| HTTP Status | Error Code | Description |
|-------------|------------|-------------|
| 400 | `VALIDATION_ERROR` | Invalid request parameters |
| 401 | `UNAUTHORIZED` | Missing or invalid authentication |
| 403 | `FORBIDDEN` | Insufficient permissions |
| 404 | `NOT_FOUND` | Resource not found |
| 409 | `CONFLICT` | Duplicate resource |
| 429 | `RATE_LIMITED` | Too many requests |
| 500 | `INTERNAL_ERROR` | Server error |

### Error Response Format

```json
{
  "status": "error",
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {},
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

### Common Authentication Errors

| Error | Cause | Resolution |
|-------|-------|------------|
| `INVALID_CREDENTIALS` | Wrong username/password | Verify login credentials |
| `ACCOUNT_DISABLED` | User account deactivated | Contact administrator |
| `TOKEN_EXPIRED` | Access token expired | Use refresh token or re-login |
| `INVALID_TOKEN` | Malformed or tampered token | Obtain new token from server |
| `INSUFFICIENT_PERMISSIONS` | User lacks required role | Request appropriate permissions |
| `RATE_LIMITED` | Too many login attempts | Wait and retry later |

## Authentication Flow Examples

### Example 1: User Login

**Request:**
```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "john.doe@example.com",
  "password": "SecurePass123!",
  "tenant_id": "tenant_001"
}
```

**Response (200 OK):**
```json
{
  "status": "success",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refresh_token": "dGhpcyBpcyBhIHJlZnJlc2ggdG9rZW4...",
    "expires_in": 900,
    "token_type": "Bearer",
    "user_info": {
      "id": "user_123",
      "username": "john.doe@example.com",
      "display_name": "John Doe",
      "roles": ["user", "viewer"]
    }
  }
}
```

### Example 2: Protected Resource Access

**Request:**
```http
GET /api/conversations
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
X-Tenant-ID: tenant_001
```

**Response (200 OK):**
```json
{
  "status": "success",
  "data": {
    "conversations": [],
    "total": 0,
    "page": 1,
    "per_page": 50
  }
}
```

### Example 3: Token Refresh

**Request:**
```http
POST /api/auth/refresh
Content-Type: application/json

{
  "refresh_token": "dGhpcyBpcyBhIHJlZnJlc2ggdG9rZW4..."
}
```

**Response (200 OK):**
```json
{
  "status": "success",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expires_in": 900,
    "token_type": "Bearer"
  }
}
```

### Example 4: Error Response

**Request:**
```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "invalid@example.com",
  "password": "wrongpassword"
}
```

**Response (401 Unauthorized):**
```json
{
  "status": "error",
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "Invalid username or password",
    "details": {
      "attempts_remaining": 4,
      "lockout_after": 5
    },
    "timestamp": "2024-01-01T12:00:00Z"
  }
}
```

## Best Practices

### Client-Side Implementation

1. **Token Storage**: Store tokens securely using httpOnly cookies or secure storage mechanisms
2. **Automatic Refresh**: Implement automatic token refresh before expiration
3. **Error Handling**: Handle all authentication errors gracefully
4. **Security Headers**: Include proper security headers in all requests
5. **Timeout Handling**: Implement proper timeout and retry logic

### Server-Side Security

1. **Rate Limiting**: Apply rate limiting to prevent brute force attacks
2. **Input Validation**: Validate all input parameters thoroughly
3. **Audit Logging**: Log all authentication attempts for security monitoring
4. **CORS Configuration**: Configure CORS properly for cross-origin requests
5. **HTTPS Enforcement**: Require HTTPS for all authentication endpoints

### Token Security

1. **Short-Lived Tokens**: Use short expiration times for access tokens
2. **Secure Refresh**: Rotate refresh tokens after use
3. **Token Binding**: Bind tokens to specific clients when possible
4. **Revocation Support**: Implement token revocation capabilities
5. **Encryption**: Encrypt sensitive token claims

### Multi-Tenant Considerations

1. **Tenant Isolation**: Ensure complete data isolation between tenants
2. **Cross-Tenant Validation**: Validate tenant access for every request
3. **Tenant-Specific Settings**: Allow per-tenant authentication configurations
4. **Audit Trail**: Maintain separate audit logs per tenant
5. **Resource Scoping**: Scope all resources to tenant boundaries