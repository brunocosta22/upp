import asyncio
import sys
import serial
import time
from smpclient import SMPClient
from smpclient.transport.serial import SMPSerialTransport
import smpclient.requests.image_management as img
import smpclient.requests.os_management as os_mgmt

SERIAL_PORT = "/dev/ttymxc2"
BAUD_RATE   = 115200
FIRMWARE    = sys.argv[1] if len(sys.argv) > 1 else "zephyr.signed.bin"

# Raw FOTA reboot command to enter MCUboot retain boot mode
FOTA_REBOOT_CMD = bytes([
    0xa5, 0xa5, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x00, 0x00, 0x08, 0x12, 0x06,
    0x08, 0x7b, 0x10, 0x02, 0x18, 0x04, 0x50, 0x95,
    0x5a, 0x5a
])

def send_fota_reboot(port: str, baud: int, wait_s: float = 3.0):
    """Send raw FOTA reboot frame and wait for device to enter MCUboot."""
    print(f"Sending FOTA reboot command to {port}...")
    with serial.Serial(port, baud, timeout=2) as s:
        s.reset_input_buffer()
        s.reset_output_buffer()
        s.write(FOTA_REBOOT_CMD)
        s.flush()
        print(f"  Sent {len(FOTA_REBOOT_CMD)} bytes: {FOTA_REBOOT_CMD.hex(' ').upper()}")

    print(f"  Waiting {wait_s}s for device to reboot into MCUboot...")
    time.sleep(wait_s)
    print("  Device should now be in MCUboot retain boot mode.\n")

async def upload_firmware():
    transport = SMPSerialTransport()

    async with SMPClient(transport, SERIAL_PORT, BAUD_RATE) as client:

        # 1. Read current image state
        print("Reading image slots...")
        r = await client.request(img.ImageStatesRead())
        for image in r.images:
            print(f"  Slot {image.slot}: {image.version} "
                  f"active={image.active} confirmed={image.confirmed}")

        # 2. Upload firmware
        print(f"\nUploading {FIRMWARE}...")
        with open(FIRMWARE, "rb") as f:
            firmware_data = f.read()
        print(f"  Firmware size: {len(firmware_data)} bytes")

        async for offset in client.upload(firmware_data):
            pct = offset / len(firmware_data) * 100
            bar = '#' * int(pct // 5) + '-' * (20 - int(pct // 5))
            print(f"  [{bar}] {pct:.1f}% ({offset}/{len(firmware_data)} bytes)", end="\r")
        print(f"\n  Upload complete.")

        # 3. Get hash of uploaded image (slot 1)
        r = await client.request(img.ImageStatesRead())
        slot1 = next((i for i in r.images if i.slot == 1), None)
        if not slot1:
            print("Error: image not found in slot 1 after upload")
            return
        print(f"  Uploaded image hash: {slot1.hash.hex()}")

        # 4. Mark image for next boot (test mode)
        print("\nSetting image for next boot (test)...")
        r = await client.request(img.ImageStatesWrite(hash=slot1.hash))

        # 5. Reset via SMP
        print("Resetting device via SMP...")
        await client.request(os_mgmt.ResetWrite())

        print("\nDone! Device rebooting into new firmware.")
        print("If it boots successfully, run confirm to make it permanent.")

def main():
    if not FIRMWARE:
        print("Usage: python3 zephyr_ota.py <firmware.signed.bin>")
        sys.exit(1)

    # Step 1: send raw reboot frame → enters MCUboot retain mode
    send_fota_reboot(SERIAL_PORT, BAUD_RATE, wait_s=3.0)

    # Step 2: upload firmware via SMP
    asyncio.run(upload_firmware())

if __name__ == "__main__":
    main()
