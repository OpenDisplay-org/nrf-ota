"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from bleak import BleakClient

from nrf_ota.dfu import LegacyDFU


@pytest.fixture
def dfu_zip(tmp_path: Path) -> Path:
    """A minimal but valid Nordic DFU ZIP containing dummy .bin and .dat files."""
    zip_path = tmp_path / "firmware.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("application.bin", b"\xde\xad\xbe\xef" * 64)
        z.writestr("application.dat", b"\x01\x02\x03\x04")
    return zip_path


@pytest.fixture
def mock_ble_client() -> MagicMock:
    """A MagicMock of BleakClient with async GATT methods stubbed out."""
    client = MagicMock(spec=BleakClient)
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray(b"\x06\x01"))  # version 6.1
    client.start_notify = AsyncMock()
    client.is_connected = True
    client.services = []
    return client


@pytest.fixture
def dfu(mock_ble_client: MagicMock) -> LegacyDFU:
    """A LegacyDFU instance wired to the mock BleakClient."""
    return LegacyDFU(mock_ble_client)
