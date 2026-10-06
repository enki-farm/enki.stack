"""KServe custom predictor for the native Laya inference format."""

from typing import Any

from kserve import Model, ModelServer
from laya import Router


def validate_instance(
    instance: Any,
) -> None:

    if not isinstance(instance, dict):
        raise ValueError("instance must be a JSON object")

    state = instance.get("state")
    questions = instance.get("questions")
    model = instance.get("model")

    if not isinstance(state, dict):
        raise ValueError("'state' must be a JSON object")

    if not isinstance(questions, dict) or not questions:
        raise ValueError("'questions' must be a non-empty JSON object")

    if model is not None and not isinstance(model, str):
        raise ValueError("'model' must be a string when provided")



class LayaModel(Model):
    """Serve Laya through KServe's custom predictor protocol."""

    def __init__(self, name: str):
        super().__init__(name)
        self.router: Any | None = None

    def load(self) -> bool:
        self.router = Router(preload=True)
        self.ready = True
        return self.ready

    def predict(self, payload: Any, headers: dict[str, str] | None = None) -> Any:
        if self.router is None:
            raise RuntimeError("Laya router is not loaded")

        instances = payload.get("instances", [])
        if not isinstance(instances, list) or not instances:
            raise ValueError("'instances' must be a non-empty list")

        
        for instance in instances:
            validate_instance(instance)

        pred = self.router.predict_batch(instances)
        return {
            "predictions": pred
        }


if __name__ == "__main__":
    model = LayaModel("laya")
    model.load()
    ModelServer().start([model])