"""cluster_label_map: rows -> per-document cluster info for list enrichment."""

import uuid

from app.routers.documents import cluster_label_map


def test_maps_rows_and_stringifies_ids():
    d1, d2 = uuid.uuid4(), uuid.uuid4()
    out = cluster_label_map([(d1, 7, "Recall timeline"), (d2, 9, None)])
    assert out[str(d1)] == {"cluster_id": 7, "cluster_label": "Recall timeline"}
    assert out[str(d2)] == {"cluster_id": 9, "cluster_label": None}


def test_empty_rows_give_empty_map():
    assert cluster_label_map([]) == {}
