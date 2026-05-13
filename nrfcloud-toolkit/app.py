#!/usr/bin/env python3
"""nRF Cloud Device Manager — GUI application."""

import csv
import json
import logging
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import requests
import serial
import serial.tools.list_ports
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# ---------------------------------------------------------------------------
# Embedded Amazon Root CA 1
# ---------------------------------------------------------------------------
AMAZON_ROOT_CA1 = """-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL
MAkGA1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJv
b3QgQ0EgMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALJ4gHHKeNXj
ca9HgFB0fW7Y14h29Jlo91ghYPl0hAEvrAIthtOgQ3pOsqTQNroBvo3bSMgHFzZM
9O6II8c+6zf1tRn4SWiw3te5djgdYZ6k/oI2peVKVuRF4fn9tBb6dNqcmzU5L/qw
IFAGbHrQgLKm+a/sRxmPUDgH3KKHOVj4utWp+UhnMJbulHheb4mjUcAwhmahRWa6
VOujw5H5SNz/0egwLX0tdHA114gk957EWW67c4cX8jJGKLhD+rcdqsq08p8kDi1L
93FcXmn/6pUCyziKrlA4b9v7LWIbxcceVOF34GfID5yHI9Y/QCB/IIDEgEw+OyQm
jgSubJrIqg0CAwEAAaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMC
AYYwHQYDVR0OBBYEFIQYzIU07LwMlJQuCFmcx7IQTgoIMA0GCSqGSIb3DQEBCwUA
A4IBAQCY8jdaQZChGsV2USggNiMOruYou6r4lK5IpDB/G/wkjUu0yKGX9rbxenDI
U5PMCCjjmCXPI6T53iHTfIUJrU6adTrCC2qJeHZERxhlbI1Bjjt/msv0tadQ1wUs
N+gDS63pYaACbvXy8MWy7Vu33PqUXHeeE6V/Uq2V8viTO96LXFvKWlJbYK8U90vv
o/ufQJVtMVT8QtPHRh8jrdkPSHCa2XV4cdFyQzR1bldZwgJcJmApzyMZFo6IQ6XU
5MsI+yMRQ+hDKXJioaldXgjUkK642M4UwtBV8ob2xJNDd2ZhwLnoQdeXeGADbkpy
rqXRfboQnoZsG4q5WTP468SQvvG5
-----END CERTIFICATE-----
"""

NRFCLOUD_API = "https://api.nrfcloud.com/v1"
DEFAULT_SECTAG = 16842753
DEFAULT_CA_COUNTRY = "VN"
DEFAULT_CA_ORG = "nRF Cloud Toolkit"
DEFAULT_CA_COMMON_NAME = "nRF Cloud Toolkit Local CA"


def app_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Core logic (serial + crypto + API)
# ---------------------------------------------------------------------------

def at_cmd(ser, cmd, timeout=10.0):
    time.sleep(0.3)
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1).decode(errors="ignore")
        buf += chunk
        clean = "\n".join(l for l in buf.splitlines() if not l.startswith("["))
        if "OK\n" in clean or "OK\r\n" in clean or "ERROR\n" in clean:
            break
    return buf.strip()


def get_imei(ser):
    resp = at_cmd(ser, "AT+CGSN")
    m = re.search(r"(\d{15})", resp)
    if not m:
        raise RuntimeError(f"Could not read IMEI from: {resp[:200]!r}")
    return m.group(1)


def delete_sectag(ser, sectag):
    for t in (0, 1, 2):
        at_cmd(ser, f"AT%CMNG=3,{sectag},{t}")


def write_credential(ser, sectag, ctype, pem):
    resp = at_cmd(ser, f'AT%CMNG=0,{sectag},{ctype},"{pem}"', timeout=15)
    labels = {0: "CA cert", 1: "client cert", 2: "private key"}
    if "OK" not in resp:
        raise RuntimeError(f"Failed to write {labels[ctype]}: {resp!r}")
    return labels[ctype]


def verify_sectag(ser, sectag):
    return at_cmd(ser, f"AT%CMNG=1,{sectag}")


def ensure_ca_files(ca_cert_path, ca_key_path, country, organization, common_name):
    ca_cert_path = Path(ca_cert_path)
    ca_key_path = Path(ca_key_path)
    cert_exists = ca_cert_path.exists()
    key_exists = ca_key_path.exists()

    if cert_exists and key_exists:
        return False
    if cert_exists != key_exists:
        missing = ca_key_path if cert_exists else ca_cert_path
        raise RuntimeError(
            "CA cert/key must be created as a pair. Missing file:\n"
            f"{missing}\n\n"
            "Select a matching CA pair, or delete the remaining CA file and run again "
            "to generate a fresh pair."
        )

    ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
    ca_key_path.parent.mkdir(parents=True, exist_ok=True)

    country = country.strip().upper()
    organization = organization.strip()
    common_name = common_name.strip()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise RuntimeError("CA Country must be exactly 2 letters, for example VN or US.")
    if not organization:
        raise RuntimeError("CA Organization is required.")
    if not common_name:
        raise RuntimeError("CA Common Name is required.")

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ca_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return True


def generate_device_cert(ca_cert_pem, ca_key_pem, device_id, days=3650):
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key  = serialization.load_pem_private_key(ca_key_pem, password=None)
    dev_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
        .issuer_name(ca_cert.subject)
        .public_key(dev_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem  = dev_key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption())
    return cert_pem, key_pem


def onboard_to_nrfcloud(api_key, device_id, cert_pem):
    cert_str = cert_pem.decode().rstrip("\r\n") + "\n"
    r = requests.post(
        f"{NRFCLOUD_API}/devices/{device_id}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"certificate": cert_str},
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Onboarding failed {r.status_code}: {r.text}")


def get_last_position(api_key, device_id):
    r = requests.get(
        f"{NRFCLOUD_API}/location/history",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"deviceId": device_id, "latest": "true"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"API error {r.status_code}: {r.text}")
    items = r.json().get("items", [])
    return items[0] if items else None


def get_location_history(api_key, device_id, hours=24, limit=20):
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end   = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get(
        f"{NRFCLOUD_API}/location/history",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"deviceId": device_id, "start": start, "end": end, "pageLimit": limit},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"API error {r.status_code}: {r.text}")
    return r.json().get("items", [])


def get_connection_status(api_key, device_id):
    r = requests.get(
        f"{NRFCLOUD_API}/devices/{device_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if r.status_code != 200:
        return "unknown"
    return r.json().get("state", {}).get("reported", {}).get("connection", {}).get("status", "unknown")


def list_serial_ports():
    return [p.device for p in serial.tools.list_ports.comports()
            if "Bluetooth" not in p.description]


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    # iOS palette
    IOS_BG      = "#F2F2F7"
    IOS_CARD    = "#FFFFFF"
    IOS_BLUE    = "#007AFF"
    IOS_LABEL   = "#000000"
    IOS_SUB     = "#6C6C70"
    IOS_BORDER  = "#C6C6C8"
    IOS_GREEN   = "#34C759"
    IOS_RED     = "#FF3B30"
    IOS_FILL    = "#E5E5EA"
    IOS_FONT    = "Helvetica Neue"

    def __init__(self):
        super().__init__()
        self.title("nRF Cloud Device Manager")
        self.resizable(True, True)
        self.minsize(720, 600)
        self._log_q = queue.Queue()
        self._apply_ios_theme()
        self._build_ui()
        self._poll_log()
        self._refresh_ports()

    def _apply_ios_theme(self):
        self.configure(bg=self.IOS_BG)
        s = ttk.Style(self)
        s.theme_use("clam")

        F  = self.IOS_FONT
        BG = self.IOS_BG;  CARD = self.IOS_CARD;  BLUE = self.IOS_BLUE
        LB = self.IOS_LABEL; SB = self.IOS_SUB;   BD = self.IOS_BORDER
        FL = self.IOS_FILL

        s.configure(".",
                    background=BG, foreground=LB,
                    font=(F, 11), borderwidth=0, relief="flat")

        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)

        s.configure("TLabel", background=BG, foreground=LB, font=(F, 11))
        s.configure("Sub.TLabel", background=BG, foreground=SB, font=(F, 10))

        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
        s.configure("TNotebook.Tab",
                    background=FL, foreground=SB,
                    padding=[18, 8], font=(F, 11, "bold"), borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", BLUE)])

        s.configure("TButton",
                    background=BLUE, foreground="white",
                    font=(F, 11, "bold"), padding=[18, 9],
                    borderwidth=0, relief="flat")
        s.map("TButton",
              background=[("active", "#0062D6"), ("disabled", "#AEAEB2")],
              foreground=[("disabled", "white")])

        s.configure("TEntry",
                    fieldbackground=CARD, foreground=LB,
                    bordercolor=BD, lightcolor=BD, darkcolor=BD,
                    insertcolor=BLUE, padding=[8, 7], font=(F, 11))
        s.map("TEntry",
              bordercolor=[("focus", BLUE)],
              lightcolor=[("focus", BLUE)])

        s.configure("TCombobox",
                    fieldbackground=CARD, background=FL,
                    foreground=LB, bordercolor=BD,
                    arrowcolor=SB, padding=[8, 7], font=(F, 11))
        s.map("TCombobox",
              fieldbackground=[("readonly", CARD)],
              bordercolor=[("focus", BLUE)])

        s.configure("TSpinbox",
                    fieldbackground=CARD, foreground=LB,
                    bordercolor=BD, arrowcolor=SB,
                    padding=[8, 7], font=(F, 11))
        s.map("TSpinbox", bordercolor=[("focus", BLUE)])

        s.configure("TLabelframe", background=CARD, bordercolor=BD,
                    relief="groove", padding=6)
        s.configure("TLabelframe.Label",
                    background=CARD, foreground=SB,
                    font=(F, 10, "bold"))

        s.configure("TScrollbar",
                    background=FL, troughcolor=BG,
                    borderwidth=0, arrowsize=12, arrowcolor=SB)
        s.map("TScrollbar", background=[("active", BD)])

        s.configure("TProgressbar",
                    troughcolor=FL, background=BLUE,
                    borderwidth=0, thickness=5)

        s.configure("Treeview",
                    background=CARD, foreground=LB,
                    fieldbackground=CARD, borderwidth=0,
                    font=(F, 10), rowheight=30)
        s.configure("Treeview.Heading",
                    background=FL, foreground=SB,
                    font=(F, 10, "bold"), borderwidth=0, relief="flat")
        s.map("Treeview",
              background=[("selected", "#D1E9FF")],
              foreground=[("selected", BLUE)])

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        self._tab_provision(nb)
        self._tab_position(nb)
        self._tab_onboard_csv(nb)

        # Log pane
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 8))
        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, state=tk.DISABLED,
            font=("Menlo", 10) if sys.platform == "darwin" else ("Consolas", 10),
            bg=self.IOS_CARD, fg=self.IOS_LABEL,
            insertbackground=self.IOS_BLUE,
            relief="flat", borderwidth=0,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.log_box.tag_config("ok",   foreground=self.IOS_GREEN)
        self.log_box.tag_config("err",  foreground=self.IOS_RED)
        self.log_box.tag_config("info", foreground=self.IOS_LABEL)
        self.log_box.tag_config("step", foreground=self.IOS_BLUE)

        btn_clear = ttk.Button(log_frame, text="Clear Log", command=self._clear_log)
        btn_clear.pack(anchor=tk.E, padx=4, pady=(0, 4))

    def _tab_provision(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="  Provision Device  ")

        pad = {"padx": 8, "pady": 4}

        # Serial port row
        row = ttk.Frame(f); row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="Serial Port:", width=14).pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_cb  = ttk.Combobox(row, textvariable=self.port_var, width=12, state="readonly")
        self.port_cb.pack(side=tk.LEFT)
        ttk.Button(row, text="Refresh", command=self._refresh_ports).pack(side=tk.LEFT, padx=4)

        # API key
        row2 = ttk.Frame(f); row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="API Key:", width=14).pack(side=tk.LEFT)
        self.prov_api_key = tk.StringVar()
        ttk.Entry(row2, textvariable=self.prov_api_key, width=48, show="*").pack(side=tk.LEFT)
        ttk.Button(row2, text="Show", command=lambda: self._toggle_show(row2)).pack(side=tk.LEFT, padx=4)

        # CA cert
        row3 = ttk.Frame(f); row3.pack(fill=tk.X, **pad)
        ttk.Label(row3, text="CA Cert:", width=14).pack(side=tk.LEFT)
        self.ca_path = tk.StringVar(value=str(app_base_dir() / "ca" / "ca.pem"))
        ttk.Entry(row3, textvariable=self.ca_path, width=44).pack(side=tk.LEFT)
        ttk.Button(row3, text="Browse", command=lambda: self._browse(self.ca_path, "*.pem")).pack(side=tk.LEFT, padx=4)

        # CA key
        row4 = ttk.Frame(f); row4.pack(fill=tk.X, **pad)
        ttk.Label(row4, text="CA Key:", width=14).pack(side=tk.LEFT)
        self.ca_key_path = tk.StringVar(value=str(app_base_dir() / "ca" / "ca_key.pem"))
        ttk.Entry(row4, textvariable=self.ca_key_path, width=44).pack(side=tk.LEFT)
        ttk.Button(row4, text="Browse", command=lambda: self._browse(self.ca_key_path, "*.pem")).pack(side=tk.LEFT, padx=4)

        # CA subject
        row_ca1 = ttk.Frame(f); row_ca1.pack(fill=tk.X, **pad)
        ttk.Label(row_ca1, text="CA Country:", width=14).pack(side=tk.LEFT)
        self.ca_country = tk.StringVar(value=DEFAULT_CA_COUNTRY)
        ttk.Entry(row_ca1, textvariable=self.ca_country, width=8).pack(side=tk.LEFT)
        ttk.Label(row_ca1, text="  Organization:", width=16).pack(side=tk.LEFT)
        self.ca_org = tk.StringVar(value=DEFAULT_CA_ORG)
        ttk.Entry(row_ca1, textvariable=self.ca_org, width=31).pack(side=tk.LEFT)

        row_ca2 = ttk.Frame(f); row_ca2.pack(fill=tk.X, **pad)
        ttk.Label(row_ca2, text="CA Common:", width=14).pack(side=tk.LEFT)
        self.ca_common_name = tk.StringVar(value=DEFAULT_CA_COMMON_NAME)
        ttk.Entry(row_ca2, textvariable=self.ca_common_name, width=58).pack(side=tk.LEFT)

        # Output dir
        row5 = ttk.Frame(f); row5.pack(fill=tk.X, **pad)
        ttk.Label(row5, text="Output Dir:", width=14).pack(side=tk.LEFT)
        self.out_dir = tk.StringVar(value=str(app_base_dir() / "devices"))
        ttk.Entry(row5, textvariable=self.out_dir, width=44).pack(side=tk.LEFT)
        ttk.Button(row5, text="Browse", command=lambda: self._browse_dir(self.out_dir)).pack(side=tk.LEFT, padx=4)

        # Sectag
        row6 = ttk.Frame(f); row6.pack(fill=tk.X, **pad)
        ttk.Label(row6, text="Sec Tag:", width=14).pack(side=tk.LEFT)
        self.sectag_var = tk.StringVar(value=str(DEFAULT_SECTAG))
        ttk.Entry(row6, textvariable=self.sectag_var, width=12).pack(side=tk.LEFT)

        # Progress + button
        self.prov_progress = ttk.Progressbar(f, mode="indeterminate")
        self.prov_progress.pack(fill=tk.X, padx=8, pady=(8, 2))
        self.prov_btn = ttk.Button(f, text="Provision Device", command=self._run_provision)
        self.prov_btn.pack(pady=4)

    def _tab_position(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="  Device Position  ")

        pad = {"padx": 8, "pady": 4}

        row1 = ttk.Frame(f); row1.pack(fill=tk.X, **pad)
        ttk.Label(row1, text="API Key:", width=14).pack(side=tk.LEFT)
        self.pos_api_key = tk.StringVar()
        ttk.Entry(row1, textvariable=self.pos_api_key, width=48, show="*").pack(side=tk.LEFT)

        row2 = ttk.Frame(f); row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="Device ID:", width=14).pack(side=tk.LEFT)
        self.pos_device_id = tk.StringVar()
        ttk.Entry(row2, textvariable=self.pos_device_id, width=36).pack(side=tk.LEFT)
        ttk.Label(row2, text="  e.g. nrf-351034927403950", foreground="gray").pack(side=tk.LEFT)

        row3 = ttk.Frame(f); row3.pack(fill=tk.X, **pad)
        ttk.Label(row3, text="History (h):", width=14).pack(side=tk.LEFT)
        self.hours_var = tk.StringVar(value="24")
        ttk.Spinbox(row3, from_=1, to=720, textvariable=self.hours_var, width=6).pack(side=tk.LEFT)

        # Position result box
        result_frame = ttk.LabelFrame(f, text="Last Known Position")
        result_frame.pack(fill=tk.X, padx=8, pady=4)
        self.pos_result = tk.Text(
            result_frame, height=5, state=tk.DISABLED,
            font=("Menlo", 10) if sys.platform == "darwin" else ("Consolas", 10),
            bg=self.IOS_CARD, fg=self.IOS_LABEL,
            relief="flat", borderwidth=0,
        )
        self.pos_result.pack(fill=tk.X, padx=4, pady=4)

        # History table
        hist_frame = ttk.LabelFrame(f, text="Location History")
        hist_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        cols = ("Timestamp", "Latitude", "Longitude", "Uncertainty (m)", "Source")
        self.hist_tree = ttk.Treeview(hist_frame, columns=cols, show="headings", height=6)
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=120 if c == "Timestamp" else 90, anchor=tk.CENTER)
        self.hist_tree.column("Timestamp", width=180)
        sb = ttk.Scrollbar(hist_frame, orient=tk.VERTICAL, command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=sb.set)
        self.hist_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.pos_btn = ttk.Button(f, text="Get Position", command=self._run_get_position)
        self.pos_btn.pack(pady=4)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = list_serial_ports()
        self.port_cb["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _browse(self, var, pattern):
        path = filedialog.askopenfilename(filetypes=[("PEM files", pattern), ("All", "*")])
        if path:
            var.set(path)

    def _browse_dir(self, var):
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _toggle_show(self, row):
        for w in row.winfo_children():
            if isinstance(w, ttk.Entry):
                w.config(show="" if w.cget("show") == "*" else "*")

    def _log(self, msg, tag="info"):
        self._log_q.put((msg, tag))

    def _poll_log(self):
        try:
            while True:
                msg, tag = self._log_q.get_nowait()
                self.log_box.config(state=tk.NORMAL)
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_box.insert(tk.END, f"[{ts}] {msg}\n", tag)
                self.log_box.see(tk.END)
                self.log_box.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _clear_log(self):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.config(state=tk.DISABLED)

    # ── Provision thread ──────────────────────────────────────────────────

    def _run_provision(self):
        port    = self.port_var.get().strip()
        api_key = self.prov_api_key.get().strip()
        ca_p    = self.ca_path.get().strip()
        ca_k    = self.ca_key_path.get().strip()
        ca_country = self.ca_country.get().strip()
        ca_org = self.ca_org.get().strip()
        ca_common_name = self.ca_common_name.get().strip()
        out     = self.out_dir.get().strip()
        try:
            sectag = int(self.sectag_var.get())
        except ValueError:
            messagebox.showerror("Input Error", "Sec Tag must be an integer.")
            return

        if not port:
            messagebox.showerror("Input Error", "Select a serial port.")
            return
        if not api_key:
            messagebox.showerror("Input Error", "Enter your nRF Cloud API key.")
            return

        self.prov_btn.config(state=tk.DISABLED)
        self.prov_progress.start(10)
        threading.Thread(target=self._provision_worker,
                         args=(port, api_key, ca_p, ca_k, ca_country, ca_org,
                               ca_common_name, out, sectag),
                         daemon=True).start()

    def _provision_worker(self, port, api_key, ca_p, ca_k, ca_country, ca_org,
                          ca_common_name, out, sectag):
        try:
            ca_created = ensure_ca_files(ca_p, ca_k, ca_country, ca_org, ca_common_name)
            if ca_created:
                self._log(f"Generated local CA: {ca_p}", "ok")
                self._log(f"Generated local CA key: {ca_k}", "ok")

            ca_cert_pem = Path(ca_p).read_bytes()
            ca_key_pem  = Path(ca_k).read_bytes()
            Path(out).mkdir(parents=True, exist_ok=True)

            self._log(f"Opening {port}...", "step")
            ser = serial.Serial(port, 115200, timeout=2)
            time.sleep(0.5)

            try:
                imei      = get_imei(ser)
                device_id = f"nrf-{imei}"
                self._log(f"IMEI: {imei}  Device ID: {device_id}", "ok")

                self._log("Generating device certificate...", "step")
                cert_pem, key_pem = generate_device_cert(ca_cert_pem, ca_key_pem, device_id)
                self._log("Certificate generated.", "ok")

                self._log(f"Clearing sectag {sectag}...", "step")
                delete_sectag(ser, sectag)

                self._log("Installing Amazon Root CA 1...", "step")
                write_credential(ser, sectag, 0, AMAZON_ROOT_CA1.strip())
                self._log("Amazon Root CA 1 installed.", "ok")

                write_credential(ser, sectag, 1, cert_pem.decode().strip())
                self._log("Device certificate installed.", "ok")

                write_credential(ser, sectag, 2, key_pem.decode().strip())
                self._log("Private key installed.", "ok")

                result = verify_sectag(ser, sectag)
                for line in result.splitlines():
                    if line.startswith("%CMNG"):
                        self._log(f"  {line}", "info")
                self._log("Modem credentials verified.", "ok")

                # Save device files
                device_dir = Path(out) / device_id
                device_dir.mkdir(parents=True, exist_ok=True)
                cert_file = device_dir / f"{device_id}_{sectag}_client-cert.pem"
                key_file  = device_dir / f"{device_id}_{sectag}_private-key.pem"
                cert_file.write_bytes(cert_pem)
                key_file.write_bytes(key_pem)
                self._log(f"Saved device files in: {device_dir}", "info")

                # Onboard
                self._log("Onboarding to nRF Cloud...", "step")
                onboard_to_nrfcloud(api_key, device_id, cert_pem)
                self._log(f"Device '{device_id}' onboarded to nRF Cloud!", "ok")

                # Save CSV
                csv_path = device_dir / "onboarding.csv"
                cert_str = cert_pem.decode().rstrip("\r\n") + "\n"
                mode = "a" if csv_path.exists() else "w"
                with open(csv_path, mode, newline="") as f:
                    w = csv.writer(f)
                    if mode == "w":
                        w.writerow(["deviceId", "subType", "tags",
                                    "supportedFirmwareTypes", "certificate"])
                    w.writerow([device_id, "", "", "APP|MODEM", cert_str])
                self._log(f"Onboarding CSV saved: {csv_path}", "info")

                # Auto-fill position tab
                self.pos_api_key.set(api_key)
                self.pos_device_id.set(device_id)

                self._log("=" * 50, "ok")
                self._log(f"Done! Device {device_id} is ready.", "ok")
                self._log("=" * 50, "ok")
                self.after(0, lambda: messagebox.showinfo(
                    "Success",
                    f"Device provisioned!\n\nDevice ID: {device_id}\n\nReboot the device to connect to nRF Cloud."
                ))

            finally:
                ser.close()

        except Exception as e:
            self._log(f"ERROR: {e}", "err")
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.after(0, self._provision_done)

    def _provision_done(self):
        self.prov_progress.stop()
        self.prov_btn.config(state=tk.NORMAL)

    def _tab_onboard_csv(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="  Onboard from CSV  ")

        pad = {"padx": 8, "pady": 4}

        # CSV file
        row1 = ttk.Frame(f); row1.pack(fill=tk.X, **pad)
        ttk.Label(row1, text="CSV File:", width=14).pack(side=tk.LEFT)
        self.csv_path = tk.StringVar()
        ttk.Entry(row1, textvariable=self.csv_path, width=44).pack(side=tk.LEFT)
        ttk.Button(row1, text="Browse",
                   command=lambda: self._browse(self.csv_path, "*.csv")).pack(side=tk.LEFT, padx=4)

        # API key
        row2 = ttk.Frame(f); row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="API Key:", width=14).pack(side=tk.LEFT)
        self.csv_api_key = tk.StringVar()
        ttk.Entry(row2, textvariable=self.csv_api_key, width=48, show="*").pack(side=tk.LEFT)
        ttk.Button(row2, text="Show", command=lambda: self._toggle_show(row2)).pack(side=tk.LEFT, padx=4)

        # Load button
        btn_row = ttk.Frame(f); btn_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        ttk.Button(btn_row, text="Load CSV", command=self._load_csv).pack(side=tk.LEFT)
        self.csv_count_label = ttk.Label(btn_row, text="", foreground="gray")
        self.csv_count_label.pack(side=tk.LEFT, padx=8)

        # Device treeview
        tree_frame = ttk.LabelFrame(f, text="Devices")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        cols = ("Device ID", "Status")
        self.csv_tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=8)
        self.csv_tree.heading("Device ID", text="Device ID")
        self.csv_tree.heading("Status", text="Status")
        self.csv_tree.column("Device ID", width=260)
        self.csv_tree.column("Status", width=120, anchor=tk.CENTER)
        self.csv_tree.tag_configure("ok",      foreground="#1a7f37")
        self.csv_tree.tag_configure("err",     foreground="#c0392b")
        self.csv_tree.tag_configure("pending", foreground="#555555")
        sb2 = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.csv_tree.yview)
        self.csv_tree.configure(yscrollcommand=sb2.set)
        self.csv_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)

        # Progress + button
        self.csv_progress = ttk.Progressbar(f, mode="determinate")
        self.csv_progress.pack(fill=tk.X, padx=8, pady=(4, 2))
        self.csv_btn = ttk.Button(f, text="Onboard All", command=self._run_onboard_csv)
        self.csv_btn.pack(pady=4)

    # ── Position thread ───────────────────────────────────────────────────

    def _run_get_position(self):
        api_key   = self.pos_api_key.get().strip()
        device_id = self.pos_device_id.get().strip()
        if not api_key or not device_id:
            messagebox.showerror("Input Error", "Enter API key and Device ID.")
            return
        self.pos_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._position_worker,
                         args=(api_key, device_id),
                         daemon=True).start()

    def _position_worker(self, api_key, device_id):
        try:
            self._log(f"Fetching position for {device_id}...", "step")
            loc = get_last_position(api_key, device_id)

            self.pos_result.config(state=tk.NORMAL)
            self.pos_result.delete("1.0", tk.END)

            if loc:
                lat = loc.get("lat", "?")
                lon = loc.get("lon", "?")
                unc = loc.get("uncertainty", "?")
                src = loc.get("serviceType", "?")
                ts  = loc.get("insertedAt", "?")
                lines = [
                    f"  Latitude   : {lat}",
                    f"  Longitude  : {lon}",
                    f"  Uncertainty: {unc} m",
                    f"  Source     : {src}",
                    f"  Timestamp  : {ts}",
                    f"  Maps       : https://maps.google.com/?q={lat},{lon}",
                ]
                self.pos_result.insert(tk.END, "\n".join(lines))
                self._log(f"Position: lat={lat}, lon={lon}, src={src}", "ok")
            else:
                self.pos_result.insert(tk.END, "  No location data yet.\n  Device may not have reported a fix.")
                self._log("No location data available.", "err")
            self.pos_result.config(state=tk.DISABLED)

            # Populate history table
            hours = int(self.hours_var.get() or 24)
            history = get_location_history(api_key, device_id, hours=hours)
            self.hist_tree.delete(*self.hist_tree.get_children())
            for h in history:
                self.hist_tree.insert("", tk.END, values=(
                    h.get("insertedAt", "?")[:19].replace("T", " "),
                    h.get("lat", "?"),
                    h.get("lon", "?"),
                    h.get("uncertainty", "?"),
                    h.get("serviceType", "?"),
                ))
            self._log(f"Loaded {len(history)} history entries.", "info")

            # Connection status
            status = get_connection_status(api_key, device_id)
            self._log(f"Device connection: {status}", "ok" if status == "connected" else "info")

        except Exception as e:
            self._log(f"ERROR: {e}", "err")
        finally:
            self.after(0, lambda: self.pos_btn.config(state=tk.NORMAL))

    # ── Onboard from CSV ──────────────────────────────────────────────────

    def _load_csv(self):
        path = self.csv_path.get().strip()
        if not path or not Path(path).exists():
            messagebox.showerror("Input Error", f"CSV file not found:\n{path}")
            return
        try:
            rows = self._parse_csv(path)
        except Exception as e:
            messagebox.showerror("CSV Error", str(e))
            return
        self.csv_tree.delete(*self.csv_tree.get_children())
        for device_id, _ in rows:
            self.csv_tree.insert("", tk.END, iid=device_id,
                                 values=(device_id, "Pending"), tags=("pending",))
        self.csv_count_label.config(text=f"{len(rows)} device(s) loaded")
        self._log(f"Loaded {len(rows)} device(s) from CSV.", "info")

    def _parse_csv(self, path):
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                device_id = row.get("deviceId", "").strip()
                cert = row.get("certificate", "").strip()
                if device_id and cert:
                    cert = cert.rstrip("\r\n") + "\n"
                    rows.append((device_id, cert))
        if not rows:
            raise ValueError("No valid rows found. CSV must have 'deviceId' and 'certificate' columns.")
        return rows

    def _run_onboard_csv(self):
        api_key = self.csv_api_key.get().strip()
        path    = self.csv_path.get().strip()
        if not api_key:
            messagebox.showerror("Input Error", "Enter your nRF Cloud API key.")
            return
        if not path or not Path(path).exists():
            messagebox.showerror("Input Error", "Load a valid CSV file first.")
            return
        if not self.csv_tree.get_children():
            messagebox.showerror("Input Error", "Click 'Load CSV' to load devices first.")
            return
        self.csv_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._onboard_csv_worker,
                         args=(api_key, path), daemon=True).start()

    def _onboard_csv_worker(self, api_key, path):
        try:
            rows = self._parse_csv(path)
        except Exception as e:
            self._log(f"CSV parse error: {e}", "err")
            self.after(0, lambda: self.csv_btn.config(state=tk.NORMAL))
            return

        total = len(rows)
        self.after(0, lambda: self.csv_progress.config(maximum=total, value=0))
        ok_count = fail_count = 0

        for i, (device_id, cert) in enumerate(rows, 1):
            self._log(f"[{i}/{total}] Onboarding {device_id}...", "step")
            self.after(0, lambda d=device_id: self._csv_set_status(d, "Onboarding…", None))
            try:
                r = requests.post(
                    f"{NRFCLOUD_API}/devices/{device_id}",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"certificate": cert},
                    timeout=30,
                )
                if r.status_code in (200, 202):
                    self._log(f"  ✓ {device_id}", "ok")
                    self.after(0, lambda d=device_id: self._csv_set_status(d, "OK", "ok"))
                    ok_count += 1
                else:
                    msg = r.json().get("message", r.text[:80]) if r.text else str(r.status_code)
                    self._log(f"  ✗ {device_id}: {r.status_code} {msg}", "err")
                    self.after(0, lambda d=device_id, m=f"Failed ({r.status_code})":
                               self._csv_set_status(d, m, "err"))
                    fail_count += 1
            except Exception as e:
                self._log(f"  ✗ {device_id}: {e}", "err")
                self.after(0, lambda d=device_id: self._csv_set_status(d, "Error", "err"))
                fail_count += 1

            self.after(0, lambda v=i: self.csv_progress.config(value=v))

        summary = f"Done: {ok_count} succeeded, {fail_count} failed (of {total})"
        self._log(summary, "ok" if fail_count == 0 else "err")
        self.after(0, lambda: messagebox.showinfo("Onboarding Complete", summary))
        self.after(0, lambda: self.csv_btn.config(state=tk.NORMAL))

    def _csv_set_status(self, device_id, status, tag):
        try:
            self.csv_tree.item(device_id, values=(device_id, status),
                               tags=(tag,) if tag else ("pending",))
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
