"""CLI entry point for nrf-ota.

Usage::

    # via uvx (no install required):
    uvx nrf-ota firmware.zip

    # via python -m:
    python -m nrf_ota firmware.zip

    # installed:
    nrf-ota firmware.zip
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakScanner, BLEDevice

from . import perform_dfu
from .scan import _CB_MACOS


def main() -> None:
    """Synchronous entry point required by ``[project.scripts]``."""
    asyncio.run(_async_main())


async def _async_main() -> None:
    parser = argparse.ArgumentParser(
        prog="nrf-ota",
        description="Flash Nordic Legacy DFU firmware to an nRF5x device over BLE.",
    )
    parser.add_argument("zip_path", help="Path to the Nordic DFU ZIP file")
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="BLE scan timeout (default: 5 s)",
    )
    _default_prn = 8 if sys.platform == "darwin" else 10
    parser.add_argument(
        "--prn",
        type=int,
        default=_default_prn,
        metavar="N",
        help=f"Packets per receipt notification (default: {_default_prn} on this platform).",
    )
    parser.add_argument(
        "--device",
        metavar="ADDR_OR_NAME",
        help="Skip the device picker. Pass a full Bluetooth address or exact device name (case-insensitive).",
    )
    args = parser.parse_args()

    # ── Scan ──────────────────────────────────────────────────────────────
    print(f"Scanning for BLE devices ({args.timeout:.0f} s)…")
    raw_scan = await BleakScanner.discover(timeout=args.timeout, return_adv=True, **_CB_MACOS)

    # Prefer the live advertisement name over the cached device.name so that after a
    # successful flash the device shows as "OD*" rather than the stale "AdaDFU".
    devices: list[tuple[BLEDevice, str]] = [
        (dev, adv.local_name or dev.name or dev.address)
        for dev, adv in raw_scan.values()
        if adv.local_name or dev.name
    ]

    if not devices:
        print("No named BLE devices found.", file=sys.stderr)
        sys.exit(1)

    # ── --device matching (non-interactive) ───────────────────────────────
    if args.device:
        needle = args.device.strip().upper()
        matches = [
            (dev, name) for dev, name in devices
            if dev.address.upper() == needle or name.upper() == needle
        ]
        if not matches:
            print(f"No device found matching '{args.device}'.", file=sys.stderr)
            print("\nAvailable devices:", file=sys.stderr)
            for dev, name in devices:
                print(f"  {name}  ({dev.address})", file=sys.stderr)
            sys.exit(1)

        selected, selected_name = matches[0]
        print(f"Selected: {selected_name}  ({selected.address})")

    else:
        print(f"\nFound {len(devices)} device(s):")
        for i, (dev, name) in enumerate(devices):
            print(f"  [{i}] {name}  ({dev.address})")

        # ── Device picker ──────────────────────────────────────────────────
        selected_index: int | None = None
        while selected_index is None:
            try:
                raw = input(f"\nSelect device [0–{len(devices) - 1}]: ").strip()
                idx = int(raw)
                if 0 <= idx < len(devices):
                    selected_index = idx
                else:
                    print(f"  Please enter a number between 0 and {len(devices) - 1}.")
            except ValueError:
                print("  Please enter a number.")
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(0)

        selected, selected_name = devices[selected_index]
        print(f"\nSelected: {selected_name}  ({selected.address})")

    # ── DFU ───────────────────────────────────────────────────────────────
    last_pct = -1

    def on_progress(pct: float) -> None:
        nonlocal last_pct
        if int(pct) <= last_pct and pct < 100:
            return
        last_pct = int(pct)
        bar_len = 40
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {pct:5.1f}%", end="", flush=True)
        if pct >= 100:
            print()

    def on_log(msg: str) -> None:
        print(f"  {msg}", flush=True)

    print()
    try:
        await perform_dfu(
            args.zip_path,
            selected,  # BLEDevice
            on_progress=on_progress,
            on_log=on_log,
            packets_per_notification=args.prn,
        )
        print("\nUpdate complete.")
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as exc:
        print(f"\nDFU failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
