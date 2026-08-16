"""AI support service layer (RND-356 / T2 and later T4/T5 additions).

Only this package (plus app/ai_kb) may talk to an LLM provider or the
kb_document_chunks index. Routers (T3) call into here; this package never
imports app.routers.* (see backend/tests/test_architecture_boundary.py).
"""
