#!/usr/bin/env python3
"""Force a USB bus reset of the Pico, as if its cable were unplugged and replugged.

Why: resetting the chip from the debugger (`monitor reset halt`, `load`)
restarts the firmware's USB stack without the PC noticing a disconnect. The PC
then still talks to the old USB address, the restarted device is back at the
default address, and every transfer fails (the kernel log shows
"can't set config #1, error -71" and the client just times out). A real bus
reset makes both sides start over. (Writing 0/1 to the sysfs `authorized` file
does NOT do this: it only re-sends SET_CONFIGURATION.)

usage: sudo python3 pico/tools/usb_reset.py [--serial 006A4832AA2CAE78] [--dry-run]

Without --serial it picks the one Raspberry Pi device that is not a debug probe.
Needs root (it opens /dev/bus/usb/BBB/DDD for writing).
"""
import argparse, fcntl, glob, os, sys, time

USBDEVFS_RESET = 0x5514          # _IO('U', 20)
VID = "2e8a"                     # Raspberry Pi
PROBE_PIDS = {"000c"}            # the CMSIS-DAP debug probe: never reset this one


def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def candidates():
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        if read(d + "/idVendor") != VID:
            continue
        yield {"path": os.path.basename(d), "pid": read(d + "/idProduct"), "product": read(d + "/product"),
               "serial": read(d + "/serial"), "bus": int(read(d + "/busnum") or 0), "dev": int(read(d + "/devnum") or 0)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="USB serial number of the device to reset")
    ap.add_argument("--dry-run", action="store_true", help="only show which device would be reset")
    a = ap.parse_args()

    found = [c for c in candidates() if c["pid"] not in PROBE_PIDS and "probe" not in c["product"].lower()]
    if a.serial:
        found = [c for c in found if c["serial"] == a.serial]
    if len(found) != 1:
        sys.exit(f"expected exactly one matching device, found {len(found)}: "
                 f"{[(c['product'], c['serial']) for c in found]}; use --serial")
    c = found[0]
    node = "/dev/bus/usb/%03d/%03d" % (c["bus"], c["dev"])
    print(f"{'would reset' if a.dry_run else 'resetting'}: {c['product']!r} serial {c['serial']} at {c['path']} ({node})")
    if a.dry_run:
        return
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET)
    finally:
        os.close(fd)
    time.sleep(1.0)
    # Linux restores the configuration the device had BEFORE the reset. If it was
    # left unconfigured (e.g. after a failed SET_CONFIGURATION), nothing would
    # bind the serial driver, so configure it explicitly.
    cfg = "/sys/bus/usb/devices/%s/bConfigurationValue" % c["path"]
    if os.path.exists(cfg) and not read(cfg):
        try:
            with open(cfg, "w") as f:
                f.write("1")
            print("device was unconfigured after the reset: set configuration 1")
        except OSError as e:
            print("could not set configuration 1:", e)
    time.sleep(1.0)
    now = [d for d in candidates() if d["serial"] == c["serial"]]
    ports = sorted(glob.glob("/dev/serial/by-id/*" + c["serial"] + "*"))
    print("after the reset:", now[0]["path"] + " is present" if now else "device not (yet) back",
          "| serial port:", ", ".join(os.path.basename(p) for p in ports) or "none yet (wait a second and look again)")


if __name__ == "__main__":
    main()
