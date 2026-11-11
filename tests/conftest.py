import pytest


@pytest.fixture(autouse=True)
def no_real_whisper(monkeypatch):
    """Tests never load (or download) a real Whisper model, even when faster-whisper is
    installed: the transcribe stage is off unless a test turns it on with a fake engine.
    Nor the word-timing model: transcribing then keeps Whisper's times."""
    from chatwatcher.subs import align, transcribe

    def refuse(self, *a, **k):
        raise AssertionError("a test tried to load the real Whisper model; pass Deps(whisper=FakeWhisper())")

    monkeypatch.setattr(transcribe, "available", lambda: False)
    monkeypatch.setattr(transcribe.Whisper, "_model", refuse)

    def no_model(*a, **k):
        raise align.AlignError("no word-timing model in tests")

    monkeypatch.setattr(align, "download", no_model)
    from chatwatcher.subs import speakers

    def no_speaker_model(*a, **k):
        raise speakers.SpeakerError("no speaker models in tests")

    monkeypatch.setattr(speakers, "download", no_speaker_model)
