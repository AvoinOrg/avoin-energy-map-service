"""Synthetic product adoption; never authority for the actual registry pin."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from src.purpose_catalog import SNAPSHOT, content_version


def synthetic_catalog():
    document = json.loads(SNAPSHOT.read_text())
    document.update(product_support='adopted', approval_reference='synthetic-test-only: no owner adoption')
    for i, choice in enumerate(document['choices']):
        # Deliberately retain upstream DRAFT and unknown lifecycle for current
        # choices. Product support does not manufacture an upstream promotion.
        choice.update(selectable=i < 5, lifecycle='unknown' if i < 5 else 'deprecated')
    document['choices'][0]['label_en'] = None
    document['catalog_version'] = content_version(document)
    return document


@contextmanager
def adopted_pin():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'synthetic-catalog.json'
        path.write_text(json.dumps(synthetic_catalog()))
        with patch('src.purpose_catalog.SNAPSHOT', path):
            yield path
