import pytest


@pytest.fixture(autouse=True)
def no_real_whisper(monkeypatch):
    """Tests never load (or download) a real Whisper model, even when faster-whisper is
    installed: the transcribe stage is off unless a test turns it on with a fake engine."""
    from chatwatcher.subs import transcribe

    def refuse(self, *a, **k):
        raise AssertionError("a test tried to load the real Whisper model; pass Deps(whisper=FakeWhisper())")

    monkeypatch.setattr(transcribe, "available", lambda: False)
    monkeypatch.setattr(transcribe.Whisper, "_model", refuse)
