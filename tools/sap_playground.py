#!/usr/bin/env python3
"""
Standalone SAP Playground — independent of the PORTMAN web app.

Configure multiple SAP servers, paste JSON, send it, view the raw response.
Mirrors the auth/post flow of sap_client.py:
  1. OAuth2 client_credentials -> POST token_url with creds as QUERY params
  2. POST payload -> {base_url}{endpoint_path} with Bearer token

Run on the server with:  python sap_playground.py
Servers are saved next to this file in  sap_servers.json

Deps:  pip install requests PySide6   (PyQt6/PyQt5 also work)
Self-test (no Qt needed):  python sap_playground.py --selftest
"""
import html
import json
import os
import sys
import time

import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sap_servers.json")
DEFAULT_TOKEN_PATH = "/RESTAdapter/OAuthServer"
DEFAULT_ENDPOINT_PATH = "/RESTAdapter/DynaportInvoice"


# ── persistence ────────────────────────────────────────────────────────────
def load_servers():
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_servers(servers):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)


def blank_server(name="New server"):
    return {
        "name": name,
        "base_url": "",
        "token_url": "",          # blank -> base_url + DEFAULT_TOKEN_PATH
        "client_id": "",
        "client_secret": "",
        "endpoint_path": DEFAULT_ENDPOINT_PATH,
    }


# ── SAP calls (same contract as sap_client.py) ─────────────────────────────
def get_token(srv):
    base = (srv["base_url"] or "").rstrip("/")
    token_url = srv.get("token_url") or (base + DEFAULT_TOKEN_PATH)
    resp = requests.post(token_url, params={
        "client_id": srv["client_id"],
        "client_secret": srv["client_secret"],
        "grant_type": "client_credentials",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_payload(srv, payload):
    """Returns a dict describing the round-trip (never raises for HTTP errors)."""
    base = (srv["base_url"] or "").rstrip("/")
    url = base + (srv.get("endpoint_path") or DEFAULT_ENDPOINT_PATH)
    token = get_token(srv)
    started = time.time()
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, timeout=60)
    duration_ms = int((time.time() - started) * 1000)
    ctype = resp.headers.get("content-type", "")
    body = resp.json() if ctype.startswith("application/json") else resp.text
    return {
        "url": url,
        "status": resp.status_code,
        "ok": resp.ok,
        "duration_ms": duration_ms,
        "headers": dict(resp.headers),
        "body": body,
    }


# ── Qt UI ──────────────────────────────────────────────────────────────────
def run_gui():
    # ponytail: try the bindings in popularity order so it runs on whatever's installed
    QtWidgets = QtCore = None
    for mod in ("PySide6", "PyQt6", "PyQt5"):
        try:
            QtWidgets = __import__(mod + ".QtWidgets", fromlist=["x"])
            QtCore = __import__(mod + ".QtCore", fromlist=["x"])
            break
        except ImportError:
            continue
    if QtWidgets is None:
        sys.exit("No Qt binding found. Install one:  pip install PySide6")

    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("SAP Playground")
            self.resize(1100, 700)
            self.servers = load_servers() or [blank_server()]

            # left: server picker + form
            self.picker = QtWidgets.QComboBox()
            self.picker.currentIndexChanged.connect(self.load_form)
            btn_add = QtWidgets.QPushButton("Add")
            btn_del = QtWidgets.QPushButton("Delete")
            btn_save = QtWidgets.QPushButton("Save")
            btn_add.clicked.connect(self.add_server)
            btn_del.clicked.connect(self.del_server)
            btn_save.clicked.connect(self.save_form)
            topbar = QtWidgets.QHBoxLayout()
            topbar.addWidget(self.picker, 1)
            topbar.addWidget(btn_add)
            topbar.addWidget(btn_del)
            topbar.addWidget(btn_save)

            self.fields = {}
            form = QtWidgets.QFormLayout()
            for key, label in [
                ("name", "Name"), ("base_url", "Base URL"),
                ("token_url", "Token URL (blank=base+OAuthServer)"),
                ("client_id", "Client ID"), ("client_secret", "Client Secret"),
                ("endpoint_path", "Endpoint path"),
            ]:
                w = QtWidgets.QLineEdit()
                if key == "client_secret":
                    w.setEchoMode(QtWidgets.QLineEdit.Password)
                self.fields[key] = w
                form.addRow(label, w)

            left = QtWidgets.QVBoxLayout()
            left.addLayout(topbar)
            left.addLayout(form)
            left.addStretch(1)
            left_w = QtWidgets.QWidget()
            left_w.setLayout(left)

            # right: payload editor + send + response
            self.payload = QtWidgets.QPlainTextEdit()
            self.payload.setPlaceholderText('{\n  "Record_Header": []\n}')
            mono = self.payload.font()
            mono.setFamily("Consolas")
            self.payload.setFont(mono)
            btn_send = QtWidgets.QPushButton("Send to SAP")
            btn_send.clicked.connect(self.do_send)
            self.response = QtWidgets.QPlainTextEdit()
            self.response.setReadOnly(True)
            self.response.setFont(mono)

            right = QtWidgets.QVBoxLayout()
            right.addWidget(QtWidgets.QLabel("Request JSON"))
            right.addWidget(self.payload, 3)
            right.addWidget(btn_send)
            right.addWidget(QtWidgets.QLabel("Response"))
            right.addWidget(self.response, 2)
            right_w = QtWidgets.QWidget()
            right_w.setLayout(right)

            split = QtWidgets.QSplitter()
            split.addWidget(left_w)
            split.addWidget(right_w)
            split.setStretchFactor(1, 1)
            split.setSizes([380, 720])
            outer = QtWidgets.QVBoxLayout(self)
            outer.addWidget(split)

            self.refresh_picker()

        def refresh_picker(self):
            self.picker.blockSignals(True)
            self.picker.clear()
            self.picker.addItems([s.get("name") or "(unnamed)" for s in self.servers])
            self.picker.blockSignals(False)
            self.load_form()

        def load_form(self):
            i = self.picker.currentIndex()
            if i < 0:
                return
            srv = self.servers[i]
            for key, w in self.fields.items():
                w.setText(str(srv.get(key, "")))

        def save_form(self):
            i = self.picker.currentIndex()
            if i < 0:
                return
            self.servers[i] = {k: w.text() for k, w in self.fields.items()}
            save_servers(self.servers)
            self.refresh_picker()
            self.picker.setCurrentIndex(i)
            self.status("Saved.")

        def add_server(self):
            self.servers.append(blank_server(f"Server {len(self.servers) + 1}"))
            save_servers(self.servers)
            self.refresh_picker()
            self.picker.setCurrentIndex(len(self.servers) - 1)

        def del_server(self):
            i = self.picker.currentIndex()
            if i < 0 or len(self.servers) == 1:
                return
            del self.servers[i]
            save_servers(self.servers)
            self.refresh_picker()

        def status(self, msg):
            self.response.setPlainText(msg)

        def do_send(self):
            try:
                payload = json.loads(self.payload.toPlainText())
            except json.JSONDecodeError as e:
                self.status(f"Invalid JSON: {e}")
                return
            srv = {k: w.text() for k, w in self.fields.items()}
            self.status("Sending...")
            QtWidgets.QApplication.processEvents()
            try:
                result = send_payload(srv, payload)
            except Exception as e:
                self.status(f"Request failed: {e}")
                return
            head = (f"HTTP {result['status']}  ({'OK' if result['ok'] else 'ERROR'})  "
                    f"{result['duration_ms']} ms\n{result['url']}\n" + "-" * 60 + "\n")
            body = result["body"]
            body_str = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
            self.status(head + html.unescape(body_str))  # SAP HTML-escapes error text

    app = QtWidgets.QApplication(sys.argv)
    win = Window()
    win.show()
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())


# ── self-test ──────────────────────────────────────────────────────────────
def _selftest():
    global CONFIG_PATH
    import tempfile
    CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "sap_servers.json")
    assert load_servers() == [], "missing file should load as empty"
    s = blank_server("X")
    assert s["endpoint_path"] == DEFAULT_ENDPOINT_PATH
    save_servers([s, blank_server("Y")])
    back = load_servers()
    assert len(back) == 2 and back[0]["name"] == "X", "round-trip failed"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        run_gui()
