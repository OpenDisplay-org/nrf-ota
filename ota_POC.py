import asyncio
import struct
import zipfile
import sys
from bleak import BleakClient, BleakScanner, BleakError

LEGACY_DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
LEGACY_DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
LEGACY_DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"
LEGACY_DFU_VERSION_UUID = "00001534-1212-efde-1523-785feabcd123"

BUTTONLESS_SERVICE_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
BUTTONLESS_CP_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"

OP_START_DFU = 0x01
OP_INIT_DFU_PARAMS = 0x02
OP_RECEIVE_FW = 0x03
OP_VALIDATE_FW = 0x04
OP_ACTIVATE_N_RESET = 0x05
OP_PACKET_RECEIPT_NOTIF_REQ = 0x08

TYPE_APPLICATION = 0x04

def parse_dfu_zip(path):
    print(f"Parsing ZIP file: {path}")
    try:
        with zipfile.ZipFile(path, "r") as z:
            bin_name = next((n for n in z.namelist() if n.lower().endswith(".bin")), None)
            dat_name = next((n for n in z.namelist() if n.lower().endswith(".dat")), None)

            if not bin_name:
                raise ValueError("No .bin file found in ZIP")
            if not dat_name:
                raise ValueError("No .dat file found in ZIP")
            
            print(f"  Found Firmware: {bin_name}")
            print(f"  Found Init Packet: {dat_name}")
            return z.read(dat_name), z.read(bin_name)
    except Exception as e:
        print(f"Error parsing ZIP: {e}")
        sys.exit(1)

_disconnected = False

def on_disconnect(client):
    global _disconnected
    _disconnected = True

class LegacyDFU:
    def __init__(self, client: BleakClient):
        self.client = client
        self._evt = asyncio.Event()
        self.last_rsp = None

    async def read_version(self):
        version_data = await self.client.read_gatt_char(LEGACY_DFU_VERSION_UUID)
        version = struct.unpack('<H', version_data)[0]
        major = (version >> 8) & 0xFF
        minor = version & 0xFF
        return major, minor

    async def start(self):
        await self.client.start_notify(LEGACY_DFU_CONTROL_POINT_UUID, self._on_notify)

    def _on_notify(self, sender, data: bytearray):
        self.last_rsp = data
        self._evt.set()

    async def _wait_for_response(self, silent=False):
        if self.last_rsp is not None and self._evt.is_set():
            rsp = self.last_rsp
            self._evt.clear()
            self.last_rsp = None
            return rsp
        
        self._evt.clear()
        self.last_rsp = None
        try:
            await asyncio.wait_for(self._evt.wait(), timeout=30.0)
            if self.last_rsp is None:
                raise RuntimeError("Notification received but no data")
            rsp = self.last_rsp
            self._evt.clear()
            self.last_rsp = None
            return rsp
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout waiting for DFU response")

    async def start_dfu(self, mode=TYPE_APPLICATION, image_size=0):
        self._evt.clear()
        self.last_rsp = None
        
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_START_DFU, mode]), response=True)
        
        size_packet = struct.pack('<III', 0, 0, image_size)
        await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, size_packet, response=False)
        
        rsp = await self._wait_for_response()
        if len(rsp) < 3:
            raise RuntimeError(f"Invalid Start DFU response: {list(rsp)}")
        status = rsp[2]
        if status not in (0x01, 0x02):
            raise RuntimeError(f"Start DFU failed with status {status}")

    async def init_dfu(self, init_packet):
        self._evt.clear()
        self.last_rsp = None
        
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_INIT_DFU_PARAMS, 0x00]), response=True)
        await asyncio.sleep(0.05)
        
        chunk_size = 20
        if len(init_packet) > chunk_size:
            for i in range(0, len(init_packet), chunk_size):
                chunk = init_packet[i:i+chunk_size]
                await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, chunk, response=False)
                if i + chunk_size < len(init_packet):
                    await asyncio.sleep(0.02)
        else:
            await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, init_packet, response=False)
        
        await asyncio.sleep(0.05)
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_INIT_DFU_PARAMS, 0x01]), response=True)
        
        rsp = await self._wait_for_response()
        if len(rsp) < 3:
            raise RuntimeError(f"Invalid Init Packet response: {list(rsp)}")
        status = rsp[2]
        if status not in (0x01, 0x02):
            raise RuntimeError(f"Init Packet failed with status {status}")

    async def send_firmware(self, firmware, packets_per_notification=30):
        print(f"Sending Firmware ({len(firmware)} bytes)...")
        
        self._evt.clear()
        self.last_rsp = None
        
        prn_value = struct.pack('<H', packets_per_notification)
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, 
                                         bytes([OP_PACKET_RECEIPT_NOTIF_REQ]) + prn_value, 
                                         response=True)
        
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_RECEIVE_FW]), response=True)
        
        chunk_size = 20
        total = len(firmware)
        sent = 0
        packet_count = 0
        
        for i in range(0, total, chunk_size):
            chunk = firmware[i : i + chunk_size]
            await self.client.write_gatt_char(LEGACY_DFU_PACKET_UUID, chunk, response=False)
            sent += len(chunk)
            packet_count += 1
            
            if packet_count >= packets_per_notification:
                try:
                    await self._wait_for_response(silent=True)
                    packet_count = 0
                except RuntimeError as e:
                    if "Timeout" not in str(e):
                        raise
            
            if i % 4000 == 0:
                print(f"Progress: {sent/total*100:.1f}%")
        
        rsp = None
        try:
            rsp = await self._wait_for_response()
        except RuntimeError as e:
            if "Timeout" in str(e) and packet_count == 0:
                self._evt.clear()
                self.last_rsp = None
                rsp = await self._wait_for_response()
            else:
                raise
        
        if rsp and len(rsp) >= 3:
            if rsp[0] == 0x10 and rsp[1] == OP_RECEIVE_FW:
                status = rsp[2]
                if status not in (0x01, 0x02):
                    raise RuntimeError(f"Firmware upload failed with status {status}")
            elif rsp[0] == 0x11:
                self._evt.clear()
                self.last_rsp = None
                rsp = await self._wait_for_response()
                if len(rsp) < 3 or rsp[0] != 0x10 or rsp[1] != OP_RECEIVE_FW:
                    raise RuntimeError(f"Unexpected response to RECEIVE_FW: {list(rsp)}")
                status = rsp[2]
                if status not in (0x01, 0x02):
                    raise RuntimeError(f"Firmware upload failed with status {status}")
            else:
                raise RuntimeError(f"Unexpected notification format: {list(rsp)}")
        else:
            raise RuntimeError(f"Invalid notification received: {list(rsp) if rsp else 'None'}")

        self._evt.clear()
        self.last_rsp = None
        await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_VALIDATE_FW]), response=True)
        
        rsp = await self._wait_for_response()
        if len(rsp) < 3:
            raise RuntimeError(f"Invalid validation response: {list(rsp)}")
        status = rsp[2]
        if status not in (0x01, 0x02):
            raise RuntimeError(f"Validation failed with status {status}")

    async def activate_and_reset(self):
        self._evt.clear()
        self.last_rsp = None
        
        try:
            await self.client.write_gatt_char(LEGACY_DFU_CONTROL_POINT_UUID, bytes([OP_ACTIVATE_N_RESET]), response=True)
            await asyncio.sleep(1.0)
        except Exception as e:
            msg = str(e).lower()
            if not any(x in msg for x in ["not connected", "disconnect", "eof", "connection"]):
                print(f"Warning: Activate and Reset command: {e}")

async def trigger_bootloader(device):
    if device.name and any(x in device.name for x in ["AdaDFU", "DfuTarg", "DFU"]):
        return False

    print(f"Device '{device.name}' appears to be an Application. Attempting to trigger Bootloader...")

    async with BleakClient(device, disconnected_callback=on_disconnect) as client:
        buttonless_char = None
        for service in client.services:
            for char in service.characteristics:
                if str(char.uuid).lower() == BUTTONLESS_CP_UUID.lower():
                    buttonless_char = char
                    break
        
        if buttonless_char:
            print("Found Buttonless DFU characteristic.")
            await client.start_notify(BUTTONLESS_CP_UUID, lambda s,d: None)
            await client.write_gatt_char(BUTTONLESS_CP_UUID, b"\x01", response=True)
            return True

        legacy_dfu_char = None
        for service in client.services:
            for char in service.characteristics:
                if str(char.uuid).lower() == LEGACY_DFU_CONTROL_POINT_UUID.lower():
                    legacy_dfu_char = char
                    break
        
        if legacy_dfu_char:
            print("Found Legacy DFU Service on App.")
            await client.start_notify(legacy_dfu_char.uuid, lambda s,d: None)
            print("Sending 'Start DFU' command to reboot...")
            try:
                await client.write_gatt_char(legacy_dfu_char, bytes([OP_START_DFU, TYPE_APPLICATION]), response=True)
                print("Reboot trigger accepted.")
            except (BleakError, EOFError, ConnectionError, OSError) as e:
                msg = str(e).lower()
                if any(x in msg for x in ["unlikely error", "0x0e", "not connected", "eof", "connection", "disconnect"]):
                    print("Reboot trigger accepted (device disconnected).")
                elif _disconnected:
                    print("Reboot trigger accepted (device disconnected).")
                else:
                    print(f"Warning: Error during reboot trigger: {e}")
                    print("Assuming reboot trigger was successful.")
            return True

    print("No DFU trigger found. Assuming manual reset.")
    return False

async def perform_dfu(zip_path):
    init_packet, firmware = parse_dfu_zip(zip_path)

    print("Scanning for BLE devices (5s)...")
    devices = await BleakScanner.discover(timeout=5)
    named_devices = [d for d in devices if d.name]

    if not named_devices:
        print("No named devices found.")
        sys.exit(1)

    print("\nFound devices:")
    for i, d in enumerate(named_devices):
        print(f"[{i}] {d.name} ({d.address})")

    while True:
        try:
            selection = input("\nSelect device index to update: ")
            index = int(selection)
            if 0 <= index < len(named_devices):
                selected_device = named_devices[index]
                break
        except ValueError:
            pass

    original_mac = selected_device.address
    
    needs_reboot = await trigger_bootloader(selected_device)
    
    if needs_reboot:
        print("Waiting for device to reboot into DFU mode...")
        await asyncio.sleep(1.5)
        
        print(f"Scanning for DFU target...")
        found = False
        dfu_device = None
        for attempt in range(10):
            devices = await BleakScanner.discover(timeout=2)
            for d in devices:
                mac_parts = original_mac.split(':')
                if len(mac_parts) == 6:
                    last_byte = int(mac_parts[5], 16)
                    new_last_byte = (last_byte + 1) % 256
                    expected_mac = ':'.join(mac_parts[:-1] + [f"{new_last_byte:02X}"])
                else:
                    expected_mac = original_mac
                
                if d.address.upper() == expected_mac.upper() or (d.name and "DFU" in d.name.upper()):
                    dfu_device = d
                    found = True
                    break
            if found:
                break
            if attempt < 9:
                print(f"Retrying scan... (attempt {attempt + 1}/10)")
            await asyncio.sleep(1)
        
        if not found or dfu_device is None:
            print("Target not found. Exiting.")
            sys.exit(1)
    else:
        dfu_device = selected_device

    if dfu_device is None:
        print("ERROR: DFU device is None. Exiting.")
        sys.exit(1)

    device_name = dfu_device.name if dfu_device.name else "Unknown"
    print(f"Connecting to DFU Target: {device_name} ({dfu_device.address})")
    
    global _disconnected
    
    if needs_reboot:
        print("Waiting for bootloader to initialize...")
        await asyncio.sleep(1.5)
        
        fresh_device = None
        for scan_attempt in range(5):
            devices = await BleakScanner.discover(timeout=2)
            for d in devices:
                if d.address.upper() == dfu_device.address.upper() or (d.name and "DFU" in d.name.upper()):
                    fresh_device = d
                    break
            if fresh_device:
                break
            if scan_attempt < 4:
                await asyncio.sleep(0.3)
        
        if not fresh_device:
            print("WARNING: Could not find device in scan, using original address...")
            fresh_device = dfu_device
        else:
            dfu_device = fresh_device
        
        await asyncio.sleep(0.2)

    max_connect_attempts = 5
    client = None
    
    for connect_attempt in range(max_connect_attempts):
        try:
            global _disconnected
            _disconnected = False
            
            if connect_attempt > 0:
                await asyncio.sleep(1.5)
            
            fresh_device = None
            for scan_attempt in range(10):
                devices = await BleakScanner.discover(timeout=2)
                for d in devices:
                    if d.address.upper() == dfu_device.address.upper() or (d.name and "DFU" in d.name.upper()):
                        fresh_device = d
                        break
                if fresh_device:
                    break
                if scan_attempt < 9:
                    await asyncio.sleep(0.5)
            
            if not fresh_device:
                if connect_attempt < max_connect_attempts - 1:
                    continue
                else:
                    raise RuntimeError("Device not found after multiple scans")
            
            client = BleakClient(fresh_device, disconnected_callback=on_disconnect)
            await client.connect(timeout=30.0)
            
            if _disconnected:
                if client:
                    try:
                        await client.disconnect()
                    except:
                        pass
                if connect_attempt < max_connect_attempts - 1:
                    continue
                else:
                    raise RuntimeError("Device keeps disconnecting immediately")
            
            if not client.is_connected:
                if connect_attempt < max_connect_attempts - 1:
                    continue
                else:
                    raise RuntimeError("Connection not established")
            
            if _disconnected or not client.is_connected:
                if client:
                    try:
                        await client.disconnect()
                    except:
                        pass
                if connect_attempt < max_connect_attempts - 1:
                    continue
                else:
                    raise RuntimeError("Device disconnected")
            
            break
            
        except (TimeoutError, BleakError) as e:
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
            if connect_attempt < max_connect_attempts - 1:
                await asyncio.sleep(3)
                continue
            else:
                raise RuntimeError(f"Failed to connect after {max_connect_attempts} attempts: {e}")
    
    if not client or not client.is_connected:
        raise RuntimeError("Failed to establish stable connection")
    
    try:
        services = client.services
        
        dfu_service_found = False
        for service in services:
            if str(service.uuid).lower() == LEGACY_DFU_SERVICE_UUID.lower():
                dfu_service_found = True
                break
        
        if not dfu_service_found:
            await client.disconnect()
            raise RuntimeError("DFU Service not found on device")
        
        try:
            if hasattr(client, 'set_mtu'):
                await client.set_mtu(517)
        except Exception:
            pass
        
        dfu = LegacyDFU(client)
        
        try:
            await dfu.read_version()
        except Exception as e:
            print(f"WARNING: Could not read DFU version: {e}")
        
        await dfu.start()
        
        if _disconnected or not client.is_connected:
            raise RuntimeError("Device disconnected before DFU start")
        
        await dfu.start_dfu(TYPE_APPLICATION, len(firmware))
        await dfu.init_dfu(init_packet)
        await dfu.send_firmware(firmware)
        await dfu.activate_and_reset()

        print("\n---------------------------------------------------")
        print(" DFU Update Complete! Device is rebooting.")
        print("---------------------------------------------------")
        
        try:
            await client.disconnect()
        except:
            pass
            
    except Exception as e:
        print(f"\n!!! DFU FAILED !!! Error: {e}")
        import traceback
        traceback.print_exc()
        raise

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python dfu_ble.py <firmware.zip>")
        sys.exit(1)

    asyncio.run(perform_dfu(sys.argv[1]))
