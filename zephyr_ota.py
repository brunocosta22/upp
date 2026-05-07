#!/usr/bin/env python3

import argparse
import asyncio
import json
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import serial
import smpclient.requests.image_management as img
import smpclient.requests.os_management as os_mgmt
from smpclient import SMPClient
from smpclient.transport.serial import SMPSerialTransport


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIRMWARE = REPO_ROOT / "build" / "ccpl_dk" / "zephyr" / "dk_app.signed.bin"
DEFAULT_SERIAL_PORT = "/dev/ttymxc2"
DEFAULT_BAUD_RATE = 115200

# DK_CMD_REBOOT (cmd=3): request MCUboot serial recovery via retention boot mode.
SERIAL_RECOVERY_REBOOT_CMD = bytes(
    [
        0xA5,
        0xA5,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x01,
        0x00,
        0x00,
        0x08,
        0x12,
        0x06,
        0x08,
        0x7B,
        0x10,
        0x02,
        0x18,
        0x03,
        0x20,
        0x72,
        0x5A,
        0x5A,
    ]
)


@dataclass(frozen=True)
class FirmwareImage:
    path: Path
    image_index: int
    name: str


def send_serial_recovery_reboot(port: str, baud: int, wait_s: float) -> None:
    print(f"Requesting MCUboot serial recovery on {port}...")
    with serial.Serial(port, baud, timeout=2) as serial_port:
        serial_port.reset_input_buffer()
        serial_port.reset_output_buffer()
        serial_port.write(SERIAL_RECOVERY_REBOOT_CMD)
        serial_port.flush()
        print(f"  Sent {len(SERIAL_RECOVERY_REBOOT_CMD)} bytes")

    print(f"  Waiting {wait_s:.1f}s for MCUboot...")
    time.sleep(wait_s)


def infer_image_index(firmware: Path, image: str) -> int:
    if image == "app":
        return 0
    if image == "net":
        return 1

    name = firmware.name.lower()
    if "net" in name or "ipc_radio" in name or "cpunet" in name:
        return 1

    return 0


def expand_firmware(firmware: Path, temp_dir: tempfile.TemporaryDirectory, image: str) -> list[FirmwareImage]:
    firmware = firmware.expanduser().resolve()
    if not firmware.exists():
        raise FileNotFoundError(f"Firmware file not found: {firmware}")

    if firmware.suffix.lower() != ".zip":
        return [FirmwareImage(path=firmware, image_index=infer_image_index(firmware, image), name=firmware.name)]

    extract_dir = Path(temp_dir.name)
    with zipfile.ZipFile(firmware) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        archive.extractall(extract_dir)

    images: list[FirmwareImage] = []
    for entry in manifest.get("files", []):
        file_name = entry["file"]
        image_index = int(entry.get("image_index", 0))
        images.append(
            FirmwareImage(
                path=extract_dir / file_name,
                image_index=image_index,
                name=file_name,
            )
        )

    if not images:
        raise ValueError(f"No images found in DFU manifest: {firmware}")

    return images


def print_image_states(response) -> None:
    for image_state in response.images:
        image = getattr(image_state, "image", 0)
        slot = getattr(image_state, "slot", "?")
        version = getattr(image_state, "version", "?")
        active = getattr(image_state, "active", False)
        confirmed = getattr(image_state, "confirmed", False)
        pending = getattr(image_state, "pending", False)
        print(
            f"  image={image} slot={slot} version={version} "
            f"active={active} confirmed={confirmed} pending={pending}"
        )


async def upload_images(client: SMPClient, images: list[FirmwareImage], mark_pending: bool) -> None:
    try:
        print("Reading image slots...")
        print_image_states(await client.request(img.ImageStatesRead()))
    except Exception as exc:
        print(f"  Image state read skipped: {exc}")

    uploaded_hashes: list[bytes] = []
    for firmware_image in images:
        firmware_data = firmware_image.path.read_bytes()
        print(
            f"\nUploading {firmware_image.name} "
            f"(image={firmware_image.image_index}, {len(firmware_data)} bytes)..."
        )

        async for offset in client.upload(firmware_data, slot=firmware_image.image_index):
            pct = 100.0 if not firmware_data else offset / len(firmware_data) * 100.0
            bar = "#" * int(pct // 5) + "-" * (20 - int(pct // 5))
            print(f"  [{bar}] {pct:.1f}% ({offset}/{len(firmware_data)} bytes)", end="\r")
        print("\n  Upload complete.")

    try:
        print("\nReading uploaded image states...")
        state_response = await client.request(img.ImageStatesRead())
        print_image_states(state_response)

        for image_state in state_response.images:
            if getattr(image_state, "active", False):
                continue
            image_hash = getattr(image_state, "hash", None)
            if image_hash:
                uploaded_hashes.append(image_hash)
    except Exception as exc:
        if mark_pending:
            raise
        print(f"  Image state read skipped: {exc}")

    if mark_pending:
        if not uploaded_hashes:
            raise RuntimeError("No non-active uploaded image hash found to mark pending")

        # MCUboot accepts one hash per request. For nRF53 multi-image builds, the
        # image-management hook links the app and net-core updates.
        print("\nMarking uploaded image for next boot...")
        await client.request(img.ImageStatesWrite(hash=uploaded_hashes[0]))

    print("Resetting device via SMP...")
    await client.request(os_mgmt.ResetWrite())


async def run(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="ccpl-dfu-") as temp_dir_path:
        temp_dir = tempfile.TemporaryDirectory(prefix="work-", dir=temp_dir_path)
        try:
            images = expand_firmware(Path(args.firmware), temp_dir, args.image)

            if not args.no_reboot:
                send_serial_recovery_reboot(args.port, args.baud, args.wait)

            transport = SMPSerialTransport(baudrate=args.baud)

            async with SMPClient(transport, args.port, timeout_s=args.timeout) as client:
                await upload_images(client, images, mark_pending=args.mark_pending)
        finally:
            temp_dir.cleanup()

    print("\nDone.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload CCPL DK firmware using MCUmgr SMP.")
    parser.add_argument(
        "firmware",
        nargs="?",
        default=str(DEFAULT_FIRMWARE),
        help=f"DFU zip or signed bin. Default: {DEFAULT_FIRMWARE}",
    )
    parser.add_argument("--port", default=DEFAULT_SERIAL_PORT, help="Serial device for MCUboot serial recovery.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE, help="Serial baud rate.")
    parser.add_argument("--wait", type=float, default=5.0, help="Seconds to wait after serial recovery reboot.")
    parser.add_argument("--timeout", type=float, default=10.0, help="SMP request timeout in seconds.")
    parser.add_argument(
        "--image",
        choices=("auto", "app", "net"),
        default="auto",
        help="Image index for raw .bin files: app=0, net=1. ZIP manifests ignore this.",
    )
    parser.add_argument("--no-reboot", action="store_true", help="Do not send the serial recovery reboot frame first.")
    parser.add_argument(
        "--mark-pending",
        action="store_true",
        help="Mark uploaded image pending in serial-recovery mode before reset.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
    
