"""Regression coverage for the batch AI-tagging job.

The job runs in a background thread against a single long-lived SQLModel
Session for the whole batch (see app/routers/ai.py::_run_batch_tagging).
Without periodic session.expunge_all(), every tagged Model3D (plus its tags/
embedding) stays pinned in the SQLAlchemy identity map for the life of the
job -- on a library of thousands of untagged models that grows unbounded
across a job that can run for hours, which is the likely cause of reports of
the container dying partway through a "Tag All" run with no error logged.
"""


class _FakeProvider:
    def tag_image(self, image_path):
        return {"tags": ["fixture"], "description": "a fixture model"}

    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class _FakeModel:
    def __init__(self, id):
        self.id = id
        self.ai_tagged = False
        self.thumbnail_path = "fake.png"
        self.tags = []
        self.filename = f"model-{id}.stl"
        self.ai_description = None
        self.embedding = None


class _FakeSession:
    def __init__(self, models_by_id):
        self.models_by_id = models_by_id
        self.expunges = 0
        self.commits = 0

    def get(self, model_cls, mid):
        return self.models_by_id.get(mid)

    def exec(self, stmt):
        class _Result:
            def first(self_inner):
                return None

        return _Result()

    def add(self, obj):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass

    def expunge_all(self):
        self.expunges += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_batch_tagging_checkpoints_session_and_resets_running_flag(monkeypatch):
    import app.routers.ai as ai_module
    import app.ai.tagging as tagging_module

    n_models = 45
    models_by_id = {i: _FakeModel(i) for i in range(n_models)}
    fake_session = _FakeSession(models_by_id)

    monkeypatch.setattr(ai_module, "Session", lambda eng: fake_session)
    monkeypatch.setattr(ai_module, "get_provider", lambda session: _FakeProvider())
    # tag_model() (called from the batch loop) resolves its own get_provider
    # from app.ai.tagging's namespace -- a separate name binding from
    # app.routers.ai's, even though both originally point at the same
    # function, so it needs patching independently.
    monkeypatch.setattr(tagging_module, "get_provider", lambda session: _FakeProvider())
    monkeypatch.setattr(ai_module, "get_setting", lambda session, key, default=None: default)
    monkeypatch.setattr(ai_module, "notify", lambda *a, **k: None)

    ai_module._job_state.update(running=False, done=0, total=0, cancel=False, estimated_cost_usd=0.0)
    ai_module._run_batch_tagging(list(models_by_id.keys()))

    assert ai_module._job_state["running"] is False
    assert ai_module._job_state["done"] == n_models
    assert all(m.ai_tagged for m in models_by_id.values())
    # 45 models at a checkpoint interval of 20 -> checkpoints at 20 and 40.
    assert fake_session.expunges == 2


def test_batch_tagging_resets_running_flag_even_if_job_crashes(monkeypatch):
    """A crash partway through must not leave the job permanently "running",
    which would otherwise make every future Tag All request 409 forever."""
    import app.routers.ai as ai_module

    def boom(eng):
        raise RuntimeError("could not open a database session")

    monkeypatch.setattr(ai_module, "Session", boom)

    ai_module._job_state.update(running=False, done=0, total=0, cancel=False, estimated_cost_usd=0.0)
    ai_module._run_batch_tagging([1, 2, 3])

    assert ai_module._job_state["running"] is False


def test_batch_tagging_continues_past_a_single_model_failure(monkeypatch):
    """One bad model (e.g. a corrupt thumbnail) must not abort the whole batch."""
    import app.routers.ai as ai_module
    import app.ai.tagging as tagging_module

    models_by_id = {1: _FakeModel(1), 2: _FakeModel(2), 3: _FakeModel(3)}
    fake_session = _FakeSession(models_by_id)

    class _FlakyProvider(_FakeProvider):
        def tag_image(self, image_path):
            if _FlakyProvider.calls == 0:
                _FlakyProvider.calls += 1
                raise RuntimeError("simulated provider failure")
            return super().tag_image(image_path)

    _FlakyProvider.calls = 0
    flaky = _FlakyProvider()

    monkeypatch.setattr(ai_module, "Session", lambda eng: fake_session)
    monkeypatch.setattr(ai_module, "get_provider", lambda session: flaky)
    monkeypatch.setattr(tagging_module, "get_provider", lambda session: flaky)
    monkeypatch.setattr(ai_module, "get_setting", lambda session, key, default=None: default)
    monkeypatch.setattr(ai_module, "notify", lambda *a, **k: None)

    ai_module._job_state.update(running=False, done=0, total=0, cancel=False, estimated_cost_usd=0.0)
    ai_module._run_batch_tagging(list(models_by_id.keys()))

    assert ai_module._job_state["done"] == 3
    assert ai_module._job_state["running"] is False
    # model 1's tag_image call fails, but 2 and 3 still get tagged.
    assert sum(1 for m in models_by_id.values() if m.ai_tagged) == 2
