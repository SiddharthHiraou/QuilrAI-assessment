import pytest


# anyio's pytest plugin rather than pytest-asyncio: it runs async fixtures in
# the same task as the test, which the SDK's task groups insist on. Async test
# modules opt in with `pytestmark = pytest.mark.anyio`.
@pytest.fixture
def anyio_backend():
    return "asyncio"
