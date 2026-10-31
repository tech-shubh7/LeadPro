import os

from app.config import Settings  # noqa: E402

# Same server/credentials as .env, separate database so tests never touch real data.
_base = Settings().database_url.rsplit("/", 1)[0]
os.environ["DATABASE_URL"] = f"{_base}/leadpro_test"
os.environ["APP_ENV"] = "test"

import pytest  # noqa: E402

from app.db import engine  # noqa: E402
from app.models.tables import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def clean_db():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield
