#!/usr/bin/env python3
"""Load a CA certificate (e.g. the EMQX broker CA) into an nRF91 modem sec_tag.

CLI counterpart to app.py — reuses the same AT%CMNG serial helpers, but writes
ONLY a root CA certificate (credential type 0) to a chosen security tag.

Examples
--------
List available serial ports:
    python emqx_ca_cli.py --list-ports

Load the bundled EMQX CA into sec_tag 42 on COM5:
    python emqx_ca_cli.py --port COM5

Load a custom CA file into a different sec_tag and verify:
    python emqx_ca_cli.py --port COM5 --sectag 100 --ca my_ca.pem --verify
"""

import argparse
import sys
import time
from pathlib import Path

import serial

# Reuse the serial/AT helpers from the GUI app. app.py is guarded by
# `if __name__ == "__main__"`, so importing it does NOT open the GUI.
from app import (
    at_cmd,
    delete_sectag,
    write_credential,
    verify_sectag,
    list_serial_ports,
)

DEFAULT_SECTAG = 42
DEFAULT_CA_FILE = Path(__file__).resolve().parent / "emqx_ca.pem"


def log(msg):
    print(msg, flush=True)


def pick_port(requested):
    """Return a usable port, auto-selecting when exactly one is present."""
    ports = list_serial_ports()
    if requested:
        return requested
    if not ports:
        sys.exit("No serial ports found. Connect the device and try again.")
    if len(ports) == 1:
        log(f"Auto-selected the only serial port: {ports[0]}")
        return ports[0]
    sys.exit(
        "Multiple serial ports found — specify one with --port:\n  "
        + "\n  ".join(ports)
    )


def read_ca(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"CA file not found: {p}")
    pem = p.read_text(encoding="utf-8").strip()
    if "BEGIN CERTIFICATE" not in pem:
        sys.exit(f"{p} does not look like a PEM certificate.")
    return pem


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write a CA certificate into an nRF91 modem security tag.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", help="Serial port (e.g. COM5). Auto-detected if only one.")
    parser.add_argument("--sectag", type=int, default=DEFAULT_SECTAG,
                        help="Security tag to write the CA into.")
    parser.add_argument("--ca", default=str(DEFAULT_CA_FILE),
                        help="Path to the CA certificate (PEM).")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate.")
    parser.add_argument("--list-ports", action="store_true",
                        help="List serial ports and exit.")
    parser.add_argument("--keep", action="store_true",
                        help="Do NOT delete the existing CA cert in the sec_tag first.")
    parser.add_argument("--no-offline", action="store_true",
                        help="Skip putting the modem offline (AT+CFUN=4) before writing.")
    parser.add_argument("--verify", action="store_true",
                        help="Read the sec_tag back (AT%%CMNG=1) after writing.")
    args = parser.parse_args(argv)

    if args.list_ports:
        ports = list_serial_ports()
        if ports:
            log("Serial ports:")
            for p in ports:
                log(f"  {p}")
        else:
            log("No serial ports found.")
        return 0

    pem = read_ca(args.ca)
    port = pick_port(args.port)

    log(f"CA file : {args.ca}")
    log(f"Port    : {port} @ {args.baud}")
    log(f"Sec tag : {args.sectag}  (credential type 0 = root CA)")

    try:
        ser = serial.Serial(port, args.baud, timeout=2)
    except serial.SerialException as e:
        sys.exit(f"Could not open {port}: {e}")

    time.sleep(0.5)
    try:
        # Make sure this is a responsive modem AT interface before touching
        # any credentials: send a bare AT and require an OK.
        log("Checking modem (AT)...")
        resp = at_cmd(ser, "AT")
        if "OK" not in resp:
            sys.exit(
                f"No OK from modem on {port} (got: {resp!r}). "
                "Wrong port, or modem not responding — CA not written."
            )
        log("  OK")

        # Credentials can only be written while the modem is deactivated.
        if not args.no_offline:
            log("Setting modem offline (AT+CFUN=4)...")
            at_cmd(ser, "AT+CFUN=4")

        if not args.keep:
            log(f"Clearing existing CA in sec_tag {args.sectag}...")
            # delete_sectag clears types 0/1/2; only type 0 (CA) matters here,
            # but reusing it keeps behaviour identical to the GUI flow.
            at_cmd(ser, f"AT%CMNG=3,{args.sectag},0")

        log("Writing CA certificate...")
        label = write_credential(ser, args.sectag, 0, pem)
        log(f"  OK: {label} written to sec_tag {args.sectag}.")

        if args.verify:
            log("Verifying (AT%CMNG=1)...")
            result = verify_sectag(ser, args.sectag)
            for line in result.splitlines():
                if line.startswith("%CMNG"):
                    log(f"  {line}")
    except Exception as e:
        sys.exit(f"ERROR: {e}")
    finally:
        ser.close()

    log("")
    log("Done. Reboot the device (or AT+CFUN=1) before it connects to the broker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
