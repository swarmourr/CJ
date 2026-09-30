"""Shared fixtures for evaluation tests."""

import os
import pytest

from evaluation.agents.fake_model import FakeModelServer
from evaluation.agents.base import ModelClient


@pytest.fixture(scope="session")
def fake_server():
    """Start one fake model server for the whole test session."""
    with FakeModelServer() as srv:
        yield srv


@pytest.fixture(autouse=True)
def set_fake_env(fake_server, monkeypatch):
    """Point CJ_EVAL_BASE_URL at the fake server for every test."""
    monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
    monkeypatch.setenv("CJ_EVAL_API_KEY",  "dummy")
    monkeypatch.setenv("CJ_EVAL_MODEL",    "fake")
    monkeypatch.setenv("CJ_EVAL_TEMPERATURE", "0.0")
    fake_server.reset_calls()


@pytest.fixture
def client(fake_server):
    return ModelClient()
