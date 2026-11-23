"""Shared SQLite-backed database fixture for the *arr feature tests."""

import os
import tempfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.database.connection as connection
from src.database.models import (
    Artist,
    Download,
    IndexerGrab,
    IndexerSearch,
    MediaList,
    MediaListEntry,
    Setting,
    Video,
)
from src.services.settings_service import SettingsService


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext_on_sqlite(type_, compiler, **kw):
    return "TEXT"


TABLES = [
    Setting.__table__,
    Artist.__table__,
    Video.__table__,
    Download.__table__,
    IndexerSearch.__table__,
    IndexerGrab.__table__,
    MediaList.__table__,
    MediaListEntry.__table__,
]


@pytest.fixture
def arr_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    connection.Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    class _Manager:
        def get_session(self):
            return _session()

        def create_engine(self):
            return engine

    monkeypatch.setattr(connection, "db_manager", _Manager())
    monkeypatch.setattr(SettingsService, "_cache", {})
    monkeypatch.setattr(SettingsService, "_cache_loaded", True)
    yield factory
    engine.dispose()
    os.remove(path)


def set_settings(**values):
    for key, value in values.items():
        SettingsService._cache[key] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
