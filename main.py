import time
from datetime import datetime
import queue
import threading
from arena_api.enums import PixelFormat
from arena_api.system import system
from arena_api.buffer import BufferFactory
from arena_api.__future__.save import Writer
from multiprocessing import Value

SYS_TL_MAP = system.tl_system_nodemap
PTP_DELTA = 10000000  # 10ms


# Set all relevant config for a device
def configure_camera(device, id=0):
    # Shorthand for device nodemap & transport-layer nodemap
    dev_map = device.nodemap
    tl_map = device.tl_stream_nodemap

    # Print camera serial number and ID
    print(f".", end="", flush=True)

    # Set image width & height, and pixel format
    dev_map["Width"].value = dev_map["Width"].max
    dev_map["Height"].value = dev_map["Height"].max
    dev_map["PixelFormat"].value = "BayerRG16"

    # Set Exposure mode and Exposure time (Manual, 30ms)
    dev_map["ExposureAuto"].value = "Off"
    dev_map["ExposureTime"].value = 30000.0

    dev_map["AcquisitionMode"].value = "Continuous"

    # Scheduled action setup
    dev_map = device.nodemap
    dev_map["TriggerMode"].value = "On"
    dev_map["TriggerSource"].value = "Action0"
    dev_map["TriggerSelector"].value = "FrameStart"
    dev_map["ActionUnconditionalMode"].value = "On"
    dev_map["ActionSelector"].value = 0
    dev_map["ActionDeviceKey"].value = 1
    dev_map["ActionGroupKey"].value = 1
    dev_map["ActionGroupMask"].value = 1
    dev_map["TransferControlMode"].value = "UserControlled"
    dev_map["TransferOperationMode"].value = "Continuous"
    dev_map["TransferStop"].execute()

    # Enable PTP Sync
    dev_map["PtpEnable"].value = True
    dev_map["PtpSlaveOnly"].value = id != 0
    dev_map["AcquisitionStart"].value = "PTPSync"

    # As we're capturing images to use later, we should use "Oldest First" to ensure we keep all data.
    # Alternative is "NewestOnly" favouring the most recent image without finishing previous ones
    tl_map["StreamBufferHandlingMode"].value = "OldestFirst"

    # This should use 9014 byte jumbo frames. Should...
    tl_map["StreamAutoNegotiatePacketSize"].value = True

    # Resilience gud.
    tl_map["StreamPacketResendEnable"].value = True

    print(" | OK")


# Wait until there is exactly one master node
def wait_for_sync(devices):
    print("Waiting for PTP Sync...", end="", flush=True)
    masters = 0
    while masters != 1:
        status = [d.nodemap["PtpStatus"].value for d in devices]
        masters = status.count("Master")
        print(".", end="", flush=True)
        time.sleep(2)
    print(" | OK ")


# Start camera streams
def start_streams(devices):
    print("Starting Streams...")
    for device in devices:
        try:
            # serial = device.nodemap["DeviceSerialNumber"].value
            # print(f"Starting stream for Camera {id} ({serial})...", end="", flush=True)
            print(".", end="", flush=True)
            device.start_stream()

        except:
            print("*", end="", flush=True)
    print(" | OK ")


def fire_cameras(devices, buffer_queue, batch=0):
    # Grab PTP time from a device
    device = devices[0]
    device.nodemap["PtpDataSetLatch"].execute()
    ptp_time = device.nodemap["PtpDataSetLatchValue"].value

    # Set up command to run DELTA_TIME from now
    SYS_TL_MAP["ActionCommandExecuteTime"].value = ptp_time + PTP_DELTA

    # Execute scheduled action command - this is the photo trigger!
    SYS_TL_MAP["ActionCommandFireCommand"].execute()

    print("Retrieving images...")

    # Grab image from cameras
    for device in devices:
        try:
            device.nodemap["TransferStart"].execute()
            buffer = device.get_buffer(timeout=500)
            device.nodemap["TransferStop"].execute()
            print(".", end="", flush=True)
            # Copy buffer into save queue
            serial = device.nodemap["DeviceSerialNumber"].value
            buffer_queue.put((BufferFactory.copy(buffer), batch, serial))

            device.requeue_buffer(buffer)
        except Exception as e:
            print(e)
    print(" | OK")


def save_image_buffers(buffer_queue, to_do):
    """
    Creates a writer. While there are images in the queue, or
        while images will still be added to the queue, save images until the
        queue is empty. Then, wait one second before checking again.
    """
    sessionID = datetime.now().strftime("%Y%m%dT%H%M%S")
    writer = Writer()
    while to_do.value or not buffer_queue.empty():
        while not buffer_queue.empty():
            buffer, batch, serial = buffer_queue.get()
            # converted = BufferFactory.convert(buffer, PixelFormat.BayerRG16)
            writer.save(
                buffer,
                f"images/{sessionID}/Scene_{batch:03}/cam_{serial}.raw",
            )
            
            # print("*", end="", flush=True)
        time.sleep(1)


if __name__ == "__main__":

    # --== Step 1: Get all devices on network ==--
    devices = system.create_device()
    if not devices:
        raise Exception("No cameras connected")
    print(f"Found and connected to {len(devices)} cameras")

    # --== Step 2: Apply config to all cameras ==--
    print("Configuring Cameras...")
    id = 0
    for device in devices:
        try:
            configure_camera(device, id)
        except Exception as e:
            print(e)
        id += 1

    # Setup for action commands
    SYS_TL_MAP["ActionCommandDeviceKey"].value = 1
    SYS_TL_MAP["ActionCommandGroupKey"].value = 1
    SYS_TL_MAP["ActionCommandGroupMask"].value = 1
    SYS_TL_MAP["ActionCommandTargetIP"].value = 0xFFFFFFFF

    # --== Step 3: Wait for PTP Sync to settle ==--
    wait_for_sync(devices)

    # --== Step 4: Start Camera Streams ==--
    start_streams(devices)

    # --== Step 5: Buffer queue and consumer to save images ==--
    buffer_queue = queue.Queue()
    to_do_signal = Value("i", 1)
    save_thread = threading.Thread(
        target=save_image_buffers,
        args=(buffer_queue, to_do_signal),
    )
    save_thread.start()

    # --== Step 6: Operate the cameras! ==--
    # Separate batches by folder, start new session folder every run of the program
    batch = 0
    while input("Press enter to capture, or q then enter to exit") != "q":
        fire_cameras(devices, buffer_queue, batch)
        batch += 1

    # Signal that there is no further work coming, and join the consumer thread
    to_do_signal.value = 0
    save_thread.join()

    # --== Cleanup ==--
    # Destroy all created devices (optional, apparently)
    system.destroy_device()
