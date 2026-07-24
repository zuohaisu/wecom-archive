---
kind: external_dependency
name: Qiniu Kodo Object Storage Provider
slug: qiniu-kodo
category: external_dependency
category_hints:
    - vendor_identity
    - sdk_real_api
    - client_constraint
scope:
    - '**'
---

### Qiniu Kodo
- Role: Pluggable cloud object-storage backend for archived media (images, voice, video, files), selected via `MEDIA_STORAGE_PROVIDER=qiniu_kodo` or per-row `storage_backend=qiniu_kodo`.
- Integration point: `backend/app/qiniu_storage.py` implements the `MediaStorageProvider` contract; `backend/app/media_storage.py` lazily constructs it from `QINIU_ACCESS_KEY`, `QINIU_SECRET_KEY`, `QINIU_BUCKET`, `QINIU_DOMAIN`, optional `QINIU_REGION` and `QINIU_TIMEOUT_SECONDS`.
- Usage model: The SDK is imported only when qiniu_kodo is selected, so local-mode startup never requires the `qiniu` package. All Qiniu SDK calls are confined to this module — callers see only the provider interface. Signed URLs are produced via the official `Auth.private_download_url` (or a deadline-pinned variant) and returned through the `/media/access` descriptor route; internal reads use a separate 60s signed URL fetched server-side.
- Constraint: `QINIU_DOMAIN` must be a full `https://` base URL (http/ bare host rejected at construction time); bucket is assumed private — no public object URLs are issued.
- Verify exact API/params against the official Qiniu Python SDK docs.