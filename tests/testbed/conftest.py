import pytest

from tests.testbed.helpers import Svc


@pytest.fixture
def svc(tmp_path):
    s = Svc(tmp_path)
    yield s
    s.core.close()
