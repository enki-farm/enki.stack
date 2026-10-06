import sys
import types


class FakeModel:
    def __init__(self, name):
        self.name = name
        self.ready = False


sys.modules["kserve"] = types.SimpleNamespace(Model=FakeModel, ModelServer=object)

from predictor import LayaModel, validate_payload


class FakeRouter:
    def __init__(self):
        self.calls = []

    def predict(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        return {"answers": {"result": {"noul": 0.9}}}


def test_payload_validation():
    assert validate_payload(
        {"state": {"text": "hello"}, "questions": {"result": {"type": "noul"}}}
    ) == ({"text": "hello"}, {"result": {"type": "noul"}}, None)


def test_model_predict_preserves_native_result_and_model_override():
    router = FakeRouter()
    model = LayaModel("laya")
    model.router = router
    model.ready = True

    result = model.predict(
        {
            "state": {"text": "hello"},
            "questions": {"result": {"type": "noul"}},
            "model": "multilingual",
        }
    )

    assert result["answers"]["result"]["noul"] == 0.9
    assert router.calls == [
        ({"text": "hello"}, {"result": {"type": "noul"}}, {"model": "multilingual"})
    ]


def test_load_marks_model_ready(monkeypatch):
    fake_laya = types.SimpleNamespace(Router=lambda preload: FakeRouter())
    monkeypatch.setitem(sys.modules, "laya", fake_laya)

    model = LayaModel("laya")

    assert model.load() is True
    assert model.ready is True
    assert model.router is not None


def test_invalid_requests_are_rejected():
    invalid_payloads = [
        [],
        {"questions": {"x": {}}},
        {"state": {}, "questions": {}},
        {"state": {}, "questions": {"x": {}}, "model": 42},
    ]

    for payload in invalid_payloads:
        try:
            validate_payload(payload)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected validation failure for {payload!r}")