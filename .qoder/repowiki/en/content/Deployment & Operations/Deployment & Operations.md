# Deployment & Operations

<cite>
**Referenced Files in This Document**
- [wecom-archive-worker.service](file://deploy/systemd/wecom-archive-worker.service)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)
- [qiniu-ssl-renew@.service](file://deploy/systemd/qiniu-ssl-renew@.service)
- [DEPLOYMENT.md](file://docs/DEPLOYMENT.md)
- [Dockerfile](file://ssl-renew/Dockerfile)
- [main.py](file://backend/app/main.py)
- [deploy_server.sh](file://scripts/deploy_server.sh)
- [deploy.yml](file://github/workflows/deploy.yml)
- [requirements.txt](file://backend/requirements.txt)
- [alembic.ini](file://backend/alembic.ini)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [System Architecture Overview](#system-architecture-overview)
3. [Production Deployment Strategies](#production-deployment-strategies)
4. [Configuration Management](#configuration-management)
5. [Monitoring and Logging](#monitoring-and-logging)
6. [Health Checks and Alerting](#health-checks-and-alerting)
7. [Backup and Disaster Recovery](#backup-and-disaster-recovery)
8. [Database Maintenance](#database-maintenance)
9. [Performance Tuning](#performance-tuning)
10. [Scaling and High Availability](#scaling-and-high-availability)
11. [SSL Certificate Management](#ssl-certificate-management)
12. [Network Security](#network-security)
13. [Troubleshooting Guide](#troubleshooting-guide)
14. [Conclusion](#conclusion)

## Introduction

This document provides comprehensive deployment and operations guidance for the WeCom Archive system. The application is designed to archive and manage WeChat Work (WeCom) conversations, messages, and media files with support for multiple storage backends including local filesystem and Qiniu Cloud Storage.

The system consists of several key components:
- **Web Application**: FastAPI-based backend serving REST APIs and web interface
- **Worker Service**: Background job processing for message synchronization and media downloads
- **Media Download Service**: Dedicated service for handling large media file downloads
- **SSL Renewal Service**: Automated SSL certificate management for Qiniu integration
- **Database Layer**: PostgreSQL with Alembic migrations for schema management

## System Architecture Overview

```mermaid
graph TB
subgraph "Frontend"
UI[Web Interface]
API[REST API]
end
subgraph "Backend Services"
Worker[Worker Service]
MediaDL[Media Download Service]
SSLRenew[SSL Renewal Service]
end
subgraph "Infrastructure"
DB[(PostgreSQL)]
Cache[(Redis - Optional)]
Storage[Storage Backend<br/>Local/Qiniu]
end
subgraph "External Services"
WeCom[WeCom API]
Qiniu[Qiniu Cloud]
end
UI --> API
API --> Worker
API --> MediaDL
Worker --> DB
MediaDL --> Storage
SSLRenew --> Qiniu
Worker --> WeCom
MediaDL --> WeCom
```

**Diagram sources**
- [main.py:1-100](file://backend/app/main.py#L1-L100)
- [wecom-archive-worker.service:1-50](file://deploy/systemd/wecom-archive-worker.service#L1-L50)

## Production Deployment Strategies

### Systemd Service Deployment

The system supports production deployment using systemd services for process management, automatic restarts, and logging integration.

#### Core Services

1. **Main Web Application Service**
   - Manages the FastAPI application server
   - Handles HTTP requests and WebSocket connections
   - Integrates with authentication and authorization systems

2. **Worker Service**
   - Processes background jobs for message synchronization
   - Handles WeCom API polling and data ingestion
   - Manages retry logic and error handling

3. **Media Download Service**
   - Dedicated service for downloading large media files
   - Implements rate limiting and bandwidth management
   - Supports resume functionality for interrupted downloads

4. **SSL Renewal Service**
   - Automated certificate renewal for Qiniu integration
   - Monitors certificate expiration dates
   - Triggers revalidation and update processes

#### Service Configuration

Each service follows systemd best practices:
- Proper dependency ordering with `After=` directives
- Resource limits via `LimitNOFILE` and memory constraints
- Logging integration with journald
- Automatic restart policies with exponential backoff

**Section sources**
- [wecom-archive-worker.service:1-50](file://deploy/systemd/wecom-archive-worker.service#L1-L50)
- [wecom-archive-media-download.service:1-50](file://deploy/systemd/wecom-archive-media-download.service#L1-L50)
- [qiniu-ssl-renew@.service:1-50](file://deploy/systemd/qiniu-ssl-renew@.service#L1-L50)

### Docker Container Deployment

The system includes Docker support for containerized deployments with standardized environments.

#### Container Architecture

```mermaid
sequenceDiagram
participant Host as "Host System"
participant Docker as "Docker Engine"
participant App as "App Container"
participant DB as "DB Container"
participant Storage as "Volume Mount"
Host->>Docker : docker-compose up
Docker->>DB : Start database container
Docker->>Storage : Mount persistent volumes
Docker->>App : Start application container
App->>DB : Connect to database
App->>Storage : Access mounted volumes
Note over App,Storage : Application running with persistent storage
```

**Diagram sources**
- [Dockerfile:1-100](file://ssl-renew/Dockerfile#L1-L100)

#### Container Orchestration

For production environments, consider using:
- **Docker Compose**: For single-node deployments
- **Kubernetes**: For multi-node orchestration and scaling
- **Cloud-native platforms**: AWS ECS, Google Cloud Run, Azure Container Instances

### Cloud Platform Deployment

#### AWS Deployment Options

1. **EC2 Instance Deployment**
   - Traditional VM-based deployment
   - Full control over environment configuration
   - Suitable for legacy infrastructure requirements

2. **Elastic Beanstalk**
   - PaaS solution with automated scaling
   - Built-in health monitoring and load balancing
   - Simplified deployment pipeline

3. **Fargate/ECS**
   - Serverless container orchestration
   - Pay-per-use pricing model
   - Automatic scaling and high availability

#### Other Cloud Providers

- **Google Cloud Platform**: Cloud Run, GKE, Compute Engine
- **Microsoft Azure**: Azure Container Instances, AKS, Virtual Machines
- **DigitalOcean**: Droplets, Managed Kubernetes, App Platform

**Section sources**
- [deploy.yml:1-100](file://.github/workflows/deploy.yml#L1-L100)

## Configuration Management

### Environment Variables

The application uses environment variables for configuration management, supporting different deployment environments through `.env` files and system-level configuration.

#### Core Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@localhost/db` | Yes |
| `SECRET_KEY` | Application secret key for encryption | Random generated | Yes |
| `WECOM_CORP_ID` | WeCom corporation ID | None | Yes |
| `WECOM_SECRET` | WeCom application secret | None | Yes |
| `STORAGE_BACKEND` | Storage backend type (`local`, `qiniu`) | `local` | No |
| `QINIU_ACCESS_KEY` | Qiniu access key | None | Conditional |
| `QINIU_SECRET_KEY` | Qiniu secret key | None | Conditional |
| `LOG_LEVEL` | Application log level | `INFO` | No |

#### Database Configuration

```mermaid
flowchart TD
Start([Application Start]) --> CheckEnv["Check DATABASE_URL"]
CheckEnv --> Valid{"Valid Connection String?"}
Valid --> |No| Error["Connection Error"]
Valid --> |Yes| Connect["Connect to Database"]
Connect --> Migrate["Run Alembic Migrations"]
Migrate --> Success["Application Ready"]
Error --> End([Exit])
Success --> End
```

**Diagram sources**
- [alembic.ini:1-50](file://backend/alembic.ini#L1-L50)

#### Secrets Management

For production deployments, use dedicated secrets management solutions:
- **HashiCorp Vault**: Enterprise-grade secrets management
- **AWS Secrets Manager**: Native AWS integration
- **Azure Key Vault**: Microsoft's cloud secrets service
- **Environment-specific .env files**: For development and staging

**Section sources**
- [requirements.txt:1-50](file://backend/requirements.txt#L1-L50)

## Monitoring and Logging

### Application Logging

The system implements structured logging with multiple output formats and log levels.

#### Log Configuration

- **Log Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL
- **Output Formats**: JSON for machine parsing, text for human readability
- **Log Rotation**: Automatic rotation based on size and time
- **Centralized Logging**: Integration with ELK stack or similar solutions

#### Structured Log Format

```json
{
  "timestamp": "2024-01-01T12:00:00Z",
  "level": "INFO",
  "service": "wecom-archive",
  "message": "Database connection established",
  "request_id": "abc123",
  "user_id": "user456"
}
```

### Metrics Collection

Implement application metrics using Prometheus-compatible endpoints:
- **Request latency percentiles**
- **Error rates by endpoint**
- **Database connection pool statistics**
- **Memory and CPU usage**
- **Queue depth for background jobs**

### Health Check Endpoints

The application exposes health check endpoints for monitoring and load balancer integration:

- `/health`: Basic health status
- `/ready`: Readiness probe for container orchestration
- `/metrics`: Prometheus metrics endpoint
- `/debug/pprof`: Python profiling endpoint (development only)

**Section sources**
- [main.py:1-200](file://backend/app/main.py#L1-L200)

## Health Checks and Alerting

### Health Check Implementation

```mermaid
stateDiagram-v2
[*] --> Starting
Starting --> Healthy : "All checks pass"
Starting --> Unhealthy : "Critical check fails"
Healthy --> Degraded : "Non-critical check fails"
Degraded --> Healthy : "Check recovers"
Degraded --> Unhealthy : "Multiple failures"
Unhealthy --> Healthy : "Service recovery"
```

**Diagram sources**
- [main.py:150-250](file://backend/app/main.py#L150-L250)

### Alerting Configuration

Configure alerts based on health check responses and application metrics:

#### Critical Alerts
- Service down (health check failure)
- Database connection failures
- Authentication service unavailability
- Storage backend errors

#### Warning Alerts
- High error rates (>1% over 5 minutes)
- Slow response times (>2s p95)
- Low disk space (<10% remaining)
- Memory usage >80%

#### Notification Channels
- Email notifications for critical alerts
- Slack/Teams integration for team notifications
- PagerDuty/OpsGenie for on-call escalation
- Webhook integration for custom alerting systems

## Backup and Disaster Recovery

### Database Backups

Implement automated database backup strategies:

#### Backup Types
- **Full backups**: Complete database snapshots (daily)
- **Incremental backups**: Transaction log backups (hourly)
- **Point-in-time recovery**: Enable WAL archiving for PostgreSQL

#### Backup Schedule

```mermaid
flowchart LR
Daily["Daily Full Backup"] --> Weekly["Weekly Retention"]
Hourly["Hourly Incremental"] --> Daily
Hourly --> Weekly
Weekly --> Monthly["Monthly Archive"]
Monthly --> Yearly["Yearly Archive"]
```

#### Backup Verification
- Automated backup integrity checks
- Restore testing in isolated environments
- Backup retention policy enforcement

### File Storage Backups

For media files and user uploads:
- **Versioned backups**: Maintain multiple versions of important files
- **Cross-region replication**: Store backups in different geographic regions
- **Encryption at rest**: Ensure backup data security

### Disaster Recovery Procedures

#### Recovery Time Objectives (RTO)
- **Critical systems**: < 1 hour
- **Important services**: < 4 hours  
- **Non-critical data**: < 24 hours

#### Recovery Point Objectives (RPO)
- **Transaction data**: < 5 minutes
- **User content**: < 1 hour
- **Archived data**: < 24 hours

**Section sources**
- [wecom_archive_worker_runbook.md:1-100](file://docs/wecom_archive_worker_runbook.md#L1-L100)

## Database Maintenance

### Migration Management

The system uses Alembic for database schema migrations:

#### Migration Workflow
1. **Development**: Create migration scripts locally
2. **Testing**: Apply migrations in test environment
3. **Staging**: Validate migrations in staging
4. **Production**: Apply migrations with rollback procedures

#### Migration Best Practices
- Always include rollback scripts
- Test migrations against production-like data
- Use zero-downtime migration techniques
- Monitor migration execution time

### Performance Optimization

#### Query Optimization
- Regular analysis of slow queries using `EXPLAIN ANALYZE`
- Index optimization based on query patterns
- Connection pooling configuration
- Query result caching where appropriate

#### Database Maintenance Tasks
- Regular VACUUM and ANALYZE operations
- Index rebuild schedules
- Statistics updates for query optimizer
- Deadlock monitoring and resolution

### Schema Evolution

```mermaid
sequenceDiagram
participant Dev as "Developer"
participant Git as "Git Repository"
participant CI as "CI/CD Pipeline"
participant Staging as "Staging DB"
participant Prod as "Production DB"
Dev->>Git : Create migration script
Git->>CI : Trigger CI pipeline
CI->>Staging : Apply migration
CI->>Staging : Run tests
Staging-->>CI : Test results
CI->>Prod : Apply migration (manual approval)
Prod-->>Dev : Migration status
```

**Diagram sources**
- [alembic.ini:1-100](file://backend/alembic.ini#L1-L100)

## Performance Tuning

### Application Performance

#### Web Server Configuration
- **Gunicorn workers**: Configure based on CPU cores and memory
- **Worker timeout**: Adjust based on request processing time
- **Keep-alive settings**: Optimize for expected traffic patterns
- **Buffer sizes**: Tune for typical payload sizes

#### Database Performance
- **Connection pool sizing**: Based on concurrent request volume
- **Query optimization**: Identify and optimize slow queries
- **Index strategy**: Add indexes for frequently queried columns
- **Read replicas**: Implement for read-heavy workloads

#### Caching Strategy
- **Application-level caching**: Redis/Memcached for frequently accessed data
- **Database query caching**: Result caching for expensive queries
- **CDN integration**: Static asset caching for improved delivery
- **Browser caching**: Appropriate cache headers for client-side caching

### Resource Allocation

#### Memory Management
- **Python memory limits**: Configure based on application requirements
- **Garbage collection tuning**: Adjust GC parameters for workload patterns
- **Memory leak detection**: Regular profiling and monitoring
- **Resource cleanup**: Ensure proper resource disposal

#### CPU Optimization
- **Process parallelization**: Leverage multiple CPU cores
- **I/O bound operations**: Asynchronous processing for network calls
- **CPU-intensive tasks**: Offload to separate worker processes
- **Profiling**: Regular performance analysis and optimization

## Scaling and High Availability

### Horizontal Scaling

```mermaid
graph TB
subgraph "Load Balancer"
LB[Nginx/HAProxy]
end
subgraph "Application Tier"
App1[App Instance 1]
App2[App Instance 2]
App3[App Instance 3]
end
subgraph "Data Tier"
DB[(Primary DB)]
DBReplica[(DB Replica)]
Cache[(Redis Cluster)]
end
LB --> App1
LB --> App2
LB --> App3
App1 --> DB
App2 --> DB
App3 --> DB
DB -.-> DBReplica
App1 --> Cache
App2 --> Cache
App3 --> Cache
```

**Diagram sources**
- [main.py:1-100](file://backend/app/main.py#L1-L100)

### Auto-scaling Configuration

#### Kubernetes Autoscaling
- **Horizontal Pod Autoscaler**: Scale based on CPU/memory usage
- **Custom metrics**: Scale based on queue depth or request rate
- **Cluster autoscaler**: Add/remove nodes based on demand
- **Pod disruption budgets**: Maintain availability during scaling

#### Cloud Provider Autoscaling
- **AWS Auto Scaling Groups**: Scale EC2 instances based on metrics
- **Google Cloud Autoscaler**: Scale managed instance groups
- **Azure Auto Scale**: Scale virtual machine scale sets

### High Availability Setup

#### Multi-AZ Deployment
- Deploy across multiple availability zones
- Implement cross-zone load balancing
- Configure failover mechanisms
- Test disaster recovery procedures regularly

#### Database High Availability
- **Primary-replica setup**: Read replicas for scaling
- **Automatic failover**: Database proxy with failover capability
- **Data consistency**: Synchronous replication for critical data
- **Backup strategy**: Cross-region backups for disaster recovery

## SSL Certificate Management

### Automated Certificate Renewal

The system includes automated SSL certificate management for Qiniu integration:

#### Certificate Lifecycle
1. **Certificate Request**: Generate CSR and submit to CA
2. **Validation**: Domain validation through DNS or HTTP challenges
3. **Issuance**: Receive signed certificates from CA
4. **Installation**: Deploy certificates to target systems
5. **Monitoring**: Track expiration dates and trigger renewals
6. **Rotation**: Seamless certificate rotation without downtime

#### Qiniu SSL Integration

```mermaid
sequenceDiagram
participant Timer as "Systemd Timer"
participant Renew as "SSL Renew Script"
participant Qiniu as "Qiniu API"
participant Nginx as "Nginx Config"
participant Certbot as "Certbot"
Timer->>Renew : Execute renewal check
Renew->>Certbot : Check certificate expiry
Certbot-->>Renew : Certificate status
Renew->>Qiniu : Upload new certificate
Qiniu-->>Renew : Upload confirmation
Renew->>Nginx : Reload configuration
Nginx-->>Renew : Reload success
Renew-->>Timer : Renewal complete
```

**Diagram sources**
- [qiniu-ssl-renew@.service:1-50](file://deploy/systemd/qiniu-ssl-renew@.service#L1-L50)

### Certificate Best Practices
- **Automated renewal**: Set up automated certificate renewal
- **Multi-domain support**: Handle wildcard and SAN certificates
- **Backup certificates**: Maintain secure backups of certificates
- **Access control**: Restrict certificate file permissions
- **Monitoring**: Alert on certificate expiration

## Network Security

### Firewall Configuration

#### Inbound Rules
- **HTTP/HTTPS**: Allow traffic on ports 80 and 443
- **SSH**: Restrict SSH access to specific IP ranges
- **Database**: Block direct database access from external networks
- **Internal services**: Allow inter-service communication

#### Outbound Rules
- **API endpoints**: Allow outbound connections to WeCom API
- **Storage services**: Permit access to cloud storage endpoints
- **Package repositories**: Allow package download URLs
- **DNS resolution**: Enable DNS queries

### Network Segmentation

```mermaid
graph TB
subgraph "Public Zone"
LB[Load Balancer]
WAF[Web Application Firewall]
end
subgraph "Application Zone"
App1[App Server 1]
App2[App Server 2]
App3[App Server 3]
end
subgraph "Data Zone"
DB[(Database)]
Cache[(Cache)]
Storage[(Storage)]
end
subgraph "Management Zone"
Admin[Admin Console]
Monitoring[Monitoring]
end
LB --> WAF
WAF --> App1
WAF --> App2
WAF --> App3
App1 --> DB
App2 --> DB
App3 --> DB
App1 --> Cache
App2 --> Cache
App3 --> Cache
Admin --> App1
Monitoring --> App1
```

### Security Hardening

#### Application Security
- **Input validation**: Validate all user inputs
- **Authentication**: Implement strong authentication mechanisms
- **Authorization**: Role-based access control (RBAC)
- **Session management**: Secure session handling and timeouts
- **CORS configuration**: Restrict cross-origin requests

#### Infrastructure Security
- **Container security**: Scan images for vulnerabilities
- **Secret management**: Use dedicated secrets management solutions
- **Audit logging**: Enable comprehensive audit trails
- **Network encryption**: TLS for all communications
- **Regular security updates**: Keep all components updated

## Troubleshooting Guide

### Common Issues and Solutions

#### Service Startup Failures
- **Check service logs**: Review systemd journal logs
- **Verify dependencies**: Ensure all required services are running
- **Validate configuration**: Check environment variables and config files
- **Resource limits**: Verify system resources are sufficient

#### Database Connection Issues
- **Connection strings**: Verify database connection parameters
- **Network connectivity**: Check firewall rules and network ACLs
- **Authentication**: Confirm database user credentials
- **Connection pooling**: Monitor connection pool utilization

#### Performance Problems
- **Resource monitoring**: Check CPU, memory, and disk usage
- **Slow queries**: Analyze database query performance
- **Network latency**: Monitor network performance metrics
- **Application profiling**: Profile application code for bottlenecks

### Debugging Tools

#### Log Analysis
- **Structured logs**: Parse JSON logs for analysis
- **Log aggregation**: Centralize logs for correlation
- **Error tracking**: Implement error tracking and reporting
- **Performance logs**: Enable detailed performance logging

#### Monitoring Dashboards
- **Application metrics**: Real-time application performance
- **Infrastructure metrics**: System resource utilization
- **Business metrics**: User activity and system usage
- **Alert dashboards**: Current alert status and history

### Emergency Procedures

#### Service Recovery
1. **Identify the issue**: Check logs and metrics
2. **Assess impact**: Determine affected users and services
3. **Apply fix**: Implement temporary or permanent solution
4. **Verify recovery**: Confirm service restoration
5. **Document incident**: Record details for future reference

#### Data Recovery
1. **Stop affected services**: Prevent further data corruption
2. **Identify backup point**: Determine last known good state
3. **Restore from backup**: Apply backup to clean environment
4. **Validate data integrity**: Verify restored data accuracy
5. **Resume operations**: Gradually restore service availability

**Section sources**
- [wecom_archive_media_download_runbook.md:1-100](file://docs/wecom_archive_media_download_runbook.md#L1-L100)

## Conclusion

This deployment and operations guide provides comprehensive coverage for deploying and managing the WeCom Archive system in production environments. The system supports multiple deployment strategies including systemd services, Docker containers, and cloud platforms, with robust configuration management, monitoring, and operational procedures.

Key considerations for successful deployment include:
- **Proper environment configuration** with secure secrets management
- **Comprehensive monitoring and alerting** for proactive issue detection
- **Automated backup and disaster recovery** procedures
- **Performance tuning** based on actual workload characteristics
- **Security hardening** following industry best practices
- **Scalability planning** for growth and high availability requirements

Regular maintenance, monitoring, and adherence to operational procedures will ensure reliable operation of the WeCom Archive system in production environments.