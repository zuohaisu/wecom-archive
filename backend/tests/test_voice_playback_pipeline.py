"""Persistence/backfill tests for RND-258 playable voice variants."""

from __future__ import annotations

from app.voice_transcode import VoiceTranscodeResult
from tests.test_reachability_audit import _TENANT_A, _insert_message, db  # noqa: F401
from tests.test_tenant_media_access import _insert_media_file


class _FakeProvider:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.saved = {}

    def read_bytes(self, ref):
        return self.objects[ref]

    def save_bytes(self, ref, data):
        self.saved[ref] = data
        return ref


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeMediaFile:
    def __init__(self):
        self.file_type = "voice"
        self.tenant_id = "tenant-a"
        self.storage_backend = "qiniu_kodo"
        self.storage_ref = "tenants/tenant-a/voice/42.amr"
        self.local_path = None
        self.playback_ref = None
        self.playback_status = "pending"


def test_generated_variant_is_co_located_and_marks_row(monkeypatch) -> None:
    from app import voice_playback_pipeline as pipeline

    mf = _FakeMediaFile()
    session = _FakeSession()
    provider = _FakeProvider({mf.storage_ref: b"#!AMR\nbytes"})
    monkeypatch.setattr(
        pipeline,
        "transcode_voice_to_playable",
        lambda _data: VoiceTranscodeResult(b"ID3playable", ".mp3"),
    )

    assert pipeline.generate_and_persist(session, provider, mf) == "generated"
    assert mf.playback_status == "generated"
    assert mf.playback_ref == "tenants/tenant-a/voice/42_play.mp3"
    assert provider.saved[mf.playback_ref] == b"ID3playable"


def test_unsupported_conversion_does_not_touch_original(monkeypatch) -> None:
    from app import voice_playback_pipeline as pipeline

    mf = _FakeMediaFile()
    session = _FakeSession()
    provider = _FakeProvider({mf.storage_ref: b"#!SILK_V3\nbytes"})
    monkeypatch.setattr(pipeline, "transcode_voice_to_playable", lambda _data: None)

    assert pipeline.generate_and_persist(session, provider, mf) == "unsupported_format"
    assert mf.playback_status == "unsupported_format"
    assert mf.playback_ref is None
    assert provider.saved == {}


def test_disabled_conversion_keeps_not_applicable(monkeypatch) -> None:
    from app import voice_playback_pipeline as pipeline

    monkeypatch.setenv("VOICE_TRANSCODE_ENABLED", "false")
    mf = _FakeMediaFile()
    mf.playback_status = "not_applicable"
    assert pipeline.generate_and_persist(_FakeSession(), _FakeProvider(), mf) == "skipped"
    assert mf.playback_status == "not_applicable"


def test_backfill_script_selects_and_generates_pending_voice_row(db, monkeypatch) -> None:
    """Historical downloaded voice is selected and completed in one batch."""
    from app import voice_playback_pipeline as pipeline
    from scripts import backfill_voice_transcode_once as script

    msg = _insert_message(
        db,
        msgid="backfill-voice",
        msgtype="voice",
        sender="staff_a",
        roomid="backfill-room",
        sdkfileid="backfill-sdk",
        tenant_id=_TENANT_A,
        msgtime=42,
    )
    media_file = _insert_media_file(
        db,
        msg.id,
        _TENANT_A,
        "backfill-sdk",
        local_path=None,
        storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/voice/77.amr",
    )
    media_file.file_type = "voice"
    media_file.playback_status = "pending"
    db.commit()
    monkeypatch.setattr(
        pipeline,
        "transcode_voice_to_playable",
        lambda _data: VoiceTranscodeResult(b"ID3backfill", ".mp3"),
    )
    provider = _FakeProvider({media_file.storage_ref: b"#!AMR\nsource"})

    candidates = script._select_candidates(db, _TENANT_A, False, 10, 5)
    assert [row.id for row in candidates] == [media_file.id]
    assert pipeline.generate_and_persist(db, provider, candidates[0]) == "generated"
    assert media_file.playback_status == "generated"
    assert media_file.playback_ref == "tenants/tenant-a/voice/77_play.mp3"
