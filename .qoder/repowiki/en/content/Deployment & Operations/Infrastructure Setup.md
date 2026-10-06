# Infrastructure Setup

<cite>
**Referenced Files in This Document**
- [README.md](file://README.md)
- [DEPLOYMENT.md](file://docs/DEPLOYMENT.md)
- [requirements.txt](file://backend/requirements.txt)
- [pyproject.toml](file://pyproject.toml)
- [wecom-archive-worker.service](file://deploy/systemd/wecom-archive-worker.service)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)
- [qiniu-ssl-renew@.service](file://deploy/systemd/qiniu-ssl-renew@.service)
- [install.sh](file://scripts/deploy_server.sh)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)
- [media_storage_ops.md](file://docs/ops/media_storage_ops.md)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [System Requirements](#system-requirements)
3. [Hardware Specifications](#hardware-specifications)
4. [Operating System Prerequisites](#operating-system-prerequisites)
5. [Installation Process](#installation-process)
6. [Systemd Service Configuration](#systemd-service-configuration)
7. [Network Configuration](#network-configuration)
8. [Database Setup](#database-setup)
9. [Storage Configuration](#storage-configuration)
10. [Environment Variables](#environment-variables)
11. [SSL Certificate Installation](#ssl-certificate-installation)
12. [Reverse Proxy Setup](#reverse-proxy-setup)
13. [Troubleshooting Guide](#troubleshooting-guide)
14. [Conclusion](#conclusion)

## Introduction

This document provides comprehensive infrastructure setup instructions for deploying the WeCom Archive system in production environments. The system is designed to archive and manage WeChat Work (WeCom) messages, conversations, and media files with support for both local and cloud storage backends.

The deployment includes multiple systemd services for different components: a main application server, background workers for message processing, media download services, and SSL certificate renewal automation. The system supports flexible storage configurations including local filesystem and Qiniu Cloud Storage.

## System Requirements

### Python Environment
- **Python Version**: 3.8 or higher
- **Package Manager**: pip or virtualenv
- **Required Dependencies**: See backend requirements.txt for complete dependency list

### Database Requirements
- **PostgreSQL**: Version 12 or higher recommended
- **Connection Pooling**: Connection pool configuration required
- **Backup Strategy**: Automated backup procedures recommended

### External Services
- **WeCom API Access**: Valid WeCom enterprise account with archive permissions
- **Qiniu Cloud Storage**: Optional, for cloud-based media storage
- **Email Service**: Optional, for notification delivery

**Section sources**
- [requirements.txt](file://backend/requirements.txt)
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)

## Hardware Specifications

### Minimum Requirements
- **CPU**: 2 cores minimum
- **RAM**: 4GB RAM minimum
- **Storage**: 50GB available disk space
- **Network**: Stable internet connection for WeCom API access

### Recommended Production Specifications
- **CPU**: 4+ cores for high-volume deployments
- **RAM**: 8GB+ RAM for optimal performance
- **Storage**: 200GB+ SSD storage with RAID configuration
- **Network**: High-bandwidth connection for media downloads

### Scaling Considerations
- Horizontal scaling supported through multiple worker instances
- Database should be deployed separately with proper connection pooling
- Storage backend should be configured for high availability

## Operating System Prerequisites

### Supported Operating Systems
- **Ubuntu/Debian**: 20.04 LTS or newer
- **CentOS/RHEL**: 8 or newer
- **Alpine Linux**: For containerized deployments

### Required System Packages
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv \
    postgresql-client nginx certbot

# CentOS/RHEL
sudo yum install -y python3 python3-pip python3-virtualenv \
    postgresql-client nginx certbot
```

### System Dependencies
- **libpq-dev**: PostgreSQL client development libraries
- **build-essential**: For compiling Python packages
- **git**: Version control access for deployment scripts

## Installation Process

### Step 1: Clone Repository and Install Dependencies
```bash
# Clone the repository
git clone https://github.com/your-org/wecom-archive.git
cd wecom-archive

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r backend/requirements.txt
```

### Step 2: Database Setup
```bash
# Initialize database schema
alembic upgrade head

# Bootstrap default tenant
python scripts/bootstrap_default_tenant.py
```

### Step 3: Configure Environment Variables
Create environment configuration file:
```bash
cp .env.example .env
# Edit .env with your configuration
```

### Step 4: Install Systemd Services
```bash
# Copy service files
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo cp deploy/systemd/*.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload
```

**Section sources**
- [install.sh](file://scripts/deploy_server.sh)
- [DEPLOYMENT.md](file://docs/DEPLOYMENT.md)

## Systemd Service Configuration

### Main Application Service
The primary web application service handles HTTP requests and API endpoints.

**Service File**: `wecom-archive-worker.service`

**Key Configuration Parameters**:
- **User/Group**: Dedicated service user for security isolation
- **WorkingDirectory**: Application installation path
- **ExecStart**: Python application entry point
- **EnvironmentFile**: Path to environment variables
- **RestartPolicy**: Automatic restart on failure

### Media Download Service
Handles background media downloading from WeCom servers.

**Service File**: `wecom-archive-media-download.service`

**Configuration Features**:
- **Timer-based Execution**: Scheduled media synchronization
- **Resource Limits**: CPU and memory constraints
- **Logging**: Structured logging output

### SSL Renewal Service
Automated SSL certificate management for Qiniu Cloud Storage integration.

**Service File**: `qiniu-ssl-renew@.service`

**Features**:
- **Template-based**: Supports multiple domain instances
- **Automatic Renewal**: Certbot integration
- **Webhook Notifications**: Status notifications

**Section sources**
- [wecom-archive-worker.service](file://deploy/systemd/wecom-archive-worker.service)
- [wecom-archive-media-download.service](file://deploy/systemd/wecom-archive-media-download.service)
- [qiniu-ssl-renew@.service](file://deploy/systemd/qiniu-ssl-renew@.service)

## Network Configuration

### Required Ports
| Port | Protocol | Service | Description |
|------|----------|---------|-------------|
| 80 | TCP | HTTP | Web traffic (redirects to HTTPS) |
| 443 | TCP | HTTPS | Secure web traffic |
| 8080 | TCP | Application | Internal application port |
| 5432 | TCP | PostgreSQL | Database connections |

### Firewall Rules
```bash
# Allow HTTP/HTTPS traffic
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Allow internal application port
sudo ufw allow 8080/tcp

# Allow database connections (internal only)
sudo ufw allow from 127.0.0.1 to any port 5432 proto tcp
```

### Reverse Proxy Configuration
Nginx configuration for handling SSL termination and request routing:

**Key Configuration Elements**:
- **SSL Termination**: HTTPS encryption
- **Request Routing**: API vs static content separation
- **Caching Headers**: Optimal browser caching
- **Security Headers**: XSS protection, HSTS

**Section sources**
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)

## Database Setup

### PostgreSQL Installation
```bash
# Install PostgreSQL
sudo apt-get install postgresql postgresql-contrib

# Create database user
sudo -u postgres createuser --interactive

# Create database
sudo -u postgres createdb wecom_archive

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE wecom_archive TO wecom_user;"
```

### Database Schema Migration
```bash
# Run Alembic migrations
alembic upgrade head

# Verify migration status
alembic current
```

### Database Configuration
Configure database connection parameters:
- **Host**: Database server hostname
- **Port**: PostgreSQL port (default 5432)
- **Database**: Database name
- **Username**: Database user
- **Password**: Database password
- **Pool Size**: Connection pool configuration

### Backup Strategy
Implement automated database backups:
```bash
# Daily backup script
pg_dump -U wecom_user -h localhost wecom_archive > backup_$(date +%Y%m%d).sql
```

**Section sources**
- [media_storage_ops.md](file://docs/ops/media_storage_ops.md)

## Storage Configuration

### Local Storage Setup
For local filesystem storage:

**Configuration Parameters**:
- **Base Directory**: Root directory for stored files
- **Subdirectory Structure**: Organized by tenant and date
- **Permissions**: Proper file ownership and permissions
- **Disk Space Monitoring**: Automated monitoring alerts

### Qiniu Cloud Storage Setup
For cloud-based storage with Qiniu Kodo:

**Required Credentials**:
- **Access Key**: Qiniu access key
- **Secret Key**: Qiniu secret key
- **Bucket Name**: Target bucket name
- **Domain**: Custom domain for CDN access

**Configuration Steps**:
1. Create Qiniu bucket with appropriate permissions
2. Configure custom domain and SSL certificates
3. Set up CDN acceleration if needed
4. Configure storage policies and lifecycle rules

### Storage Backend Selection
The system supports multiple storage backends:
- **Local Filesystem**: Direct file storage
- **Qiniu Cloud Storage**: Cloud-based object storage
- **Future Extensions**: S3-compatible storage support

**Section sources**
- [media_storage_ops.md](file://docs/ops/media_storage_ops.md)

## Environment Variables

### Core Configuration
| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@host/db` |
| `SECRET_KEY` | Application secret key | Random generated string |
| `APP_HOST` | Application bind address | `0.0.0.0` |
| `APP_PORT` | Application port | `8080` |

### WeCom Integration
| Variable | Description | Example |
|----------|-------------|---------|
| `WECOM_CORP_ID` | WeCom corporation ID | `ww1234567890abcdef` |
| `WECOM_SECRET` | WeCom application secret | `secret_key_here` |
| `WECOM_TOKEN` | WeCom verification token | `verification_token` |

### Storage Configuration
| Variable | Description | Example |
|----------|-------------|---------|
| `STORAGE_BACKEND` | Storage backend type | `local` or `qiniu` |
| `LOCAL_STORAGE_PATH` | Local storage directory | `/var/lib/wecom-archive/media` |
| `QINIU_ACCESS_KEY` | Qiniu access key | `access_key_here` |
| `QINIU_SECRET_KEY` | Qiniu secret key | `secret_key_here` |
| `QINIU_BUCKET` | Qiniu bucket name | `my-bucket` |
| `QINIU_DOMAIN` | Qiniu CDN domain | `cdn.example.com` |

### Security Settings
| Variable | Description | Example |
|----------|-------------|---------|
| `ALLOWED_HOSTS` | Comma-separated allowed hosts | `example.com,www.example.com` |
| `SESSION_COOKIE_SECURE` | Secure cookies | `True` |
| `CSRF_ENABLED` | CSRF protection | `True` |

## SSL Certificate Installation

### Certbot Installation
```bash
# Install Certbot
sudo apt-get install certbot python3-certbot-nginx

# Obtain SSL certificate
sudo certbot certonly --webroot -w /var/www/html -d example.com -d www.example.com
```

### Certificate Management
**Certificate Locations**:
- **Private Key**: `/etc/letsencrypt/live/example.com/privkey.pem`
- **Full Chain**: `/etc/letsencrypt/live/example.com/fullchain.pem`
- **Certificate**: `/etc/letsencrypt/live/example.com/cert.pem`

### Automatic Renewal
Configure automatic certificate renewal:
```bash
# Test renewal process
sudo certbot renew --dry-run

# Enable automatic renewal
sudo systemctl enable certbot.timer
```

### Nginx SSL Configuration
Configure Nginx for SSL termination:
- **SSL Certificate Paths**: Point to Let's Encrypt certificates
- **Protocol Versions**: TLS 1.2 and 1.3 only
- **Cipher Suites**: Modern cipher configuration
- **HSTS**: HTTP Strict Transport Security headers

**Section sources**
- [qiniu-ssl-renew@.service](file://deploy/systemd/qiniu-ssl-renew@.service)

## Reverse Proxy Setup

### Nginx Configuration
Complete Nginx reverse proxy configuration:

**Server Block Configuration**:
- **HTTP to HTTPS Redirect**: Automatic redirect
- **SSL Termination**: Certificate management
- **Proxy Headers**: Proper header forwarding
- **Static Content Caching**: Browser caching optimization

**Location Blocks**:
- **API Endpoints**: `/api/*` proxy to application
- **Static Assets**: `/static/*` direct serving
- **Media Files**: `/media/*` proxied or direct serving
- **Health Checks**: `/health` endpoint

### Security Headers
Configure essential security headers:
- **Content-Security-Policy**: Restrict resource loading
- **X-Frame-Options**: Clickjacking protection
- **X-Content-Type-Options**: MIME sniffing prevention
- **Referrer-Policy**: Referrer information control

### Performance Optimization
- **Gzip Compression**: Enable for text-based content
- **Browser Caching**: Appropriate cache headers
- **Connection Keep-Alive**: Persistent connections
- **Buffer Sizes**: Optimized buffer configuration

**Section sources**
- [ARCHITECTURE.md](file://docs/ARCHITECTURE.md)

## Troubleshooting Guide

### Common Issues and Solutions

#### Service Startup Failures
- **Check Logs**: Review systemd journal logs
- **Verify Permissions**: Ensure proper file permissions
- **Validate Configuration**: Check environment variables
- **Test Dependencies**: Verify database connectivity

#### Database Connection Issues
- **Connection String**: Validate DATABASE_URL format
- **Firewall Rules**: Ensure port accessibility
- **Authentication**: Verify database credentials
- **Connection Pool**: Check pool size limits

#### Storage Problems
- **Local Storage**: Verify directory permissions and disk space
- **Qiniu Access**: Check API credentials and bucket permissions
- **Network Connectivity**: Test external service access
- **File Upload**: Monitor upload progress and error logs

#### SSL Certificate Issues
- **Certificate Expiry**: Check certificate validity dates
- **Domain Verification**: Ensure domain ownership
- **DNS Resolution**: Verify DNS records
- **Firewall Rules**: Check port 80/443 accessibility

### Log Analysis
**Application Logs**:
- Location: `/var/log/wecom-archive/`
- Format: JSON structured logging
- Rotation: Automated log rotation

**Systemd Logs**:
- Command: `journalctl -u wecom-archive-worker`
- Real-time: `journalctl -f -u wecom-archive-worker`

### Health Check Endpoints
- **Health Status**: `/health` returns service status
- **Readiness**: `/ready` indicates service readiness
- **Metrics**: `/metrics` for Prometheus scraping

**Section sources**
- [media_storage_ops.md](file://docs/ops/media_storage_ops.md)

## Conclusion

This infrastructure setup guide provides comprehensive instructions for deploying the WeCom Archive system in production environments. The system is designed with scalability, security, and maintainability as core principles.

Key deployment considerations include:
- **Proper Resource Allocation**: Ensure adequate CPU, memory, and storage resources
- **Security Hardening**: Implement proper firewall rules, SSL certificates, and access controls
- **Monitoring and Logging**: Set up comprehensive monitoring and alerting
- **Backup and Recovery**: Implement automated backups and disaster recovery procedures
- **Scaling Strategy**: Plan for horizontal scaling based on usage patterns

The modular architecture with separate systemd services allows for independent scaling and maintenance of different components. The flexible storage backend support enables adaptation to various deployment scenarios and growth requirements.

Regular updates and maintenance procedures should be established to ensure system reliability and security throughout the deployment lifecycle.