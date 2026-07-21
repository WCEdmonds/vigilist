import uuid

from app.routers.documents import ai_decision_map


def test_maps_rows():
    d = uuid.uuid4()
    out = ai_decision_map([(d, "relevant", 92, None), ])
    assert out[str(d)] == {"ai_decision": "relevant", "ai_confidence": 92, "ai_decided": False}


def test_decided_flag():
    d = uuid.uuid4()
    out = ai_decision_map([(d, "relevant", 92, "agree")])
    assert out[str(d)]["ai_decided"] is True


def test_empty():
    assert ai_decision_map([]) == {}
