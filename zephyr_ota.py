import asyncio
import sys
from smpclient import SMPClient
from smpclient.transport.serial import SMPSerialTransport
from smpclient.requests.image_management import ImageUploadWrite
from smpclient.generics import error, success
import smpclient.requests.image_management as img
import smpclient.requests.os_management as os_mgmt

SERIAL_PORT = "/dev/ttymxc2"
BAUD_RATE   = 115200
FIRMWARE    = sys.argv[1] if len(sys.argv) > 1 else "zephyr.signed.bin"

async def upload_firmware():
    transport = SMPSerialTransport()

    async with SMPClient(transport, SERIAL_PORT, BAUD_RATE) as client:

        # 1. Read current image state
        print("Reading image slots...")
        r = await client.request(img.ImageStatesRead())
        if error(r):
            print(f"Error reading image state: {r}")
            return
        for image in r.images:
            print(f"  Slot {image.slot}: {image.version} active={image.active} confirmed={image.confirmed}")

        # 2. Upload firmware
        print(f"\nUploading {FIRMWARE}...")
        with open(FIRMWARE, "rb") as f:
            firmware_data = f.read()

        async for offset in client.upload(firmware_data):
            pct = offset / len(firmware_data) * 100
            print(f"  Progress: {pct:.1f}% ({offset}/{len(firmware_data)} bytes)", end="\r")
        print("\nUpload complete.")

        # 3. Get hash of uploaded image (slot 1)
        r = await client.request(img.ImageStatesRead())
        slot1 = next((i for i in r.images if i.slot == 1), None)
        if not slot1:
            print("Error: image not found in slot 1 after upload")
            return
        print(f"Uploaded image hash: {slot1.hash.hex()}")

        # 4. Test image (mark for next boot)
        print("Setting image for next boot (test)...")
        r = await client.request(img.ImageStatesWrite(hash=slot1.hash))
        if error(r):
            print(f"Error setting test image: {r}")
            return

        # 5. Reset device
        print("Resetting device...")
        r = await client.request(os_mgmt.ResetWrite())
        if error(r):
            print(f"Error resetting: {r}")
            return

        print("\nDone! Device is rebooting into new firmware.")
        print("If firmware boots successfully, run confirm to make it permanent.")

asyncio.run(upload_firmware())