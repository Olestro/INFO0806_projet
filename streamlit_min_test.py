"""Mini app Streamlit pour tester le start/stop d'un scan RFID."""

from __future__ import annotations

import threading
import time

import streamlit as st

from connexion_decodeur import (
    DEFAULT_ANTENNAS,
    DEFAULT_IP,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    parse_antennas,
    run_inventory,
)


st.set_page_config(page_title="RFID Mini Test", page_icon="📡", layout="centered")

_SCAN_STATE = {"status": "idle"}
_SCAN_LOCK = threading.Lock()


def _init_state() -> None:
    if "scan_thread" not in st.session_state:
        st.session_state.scan_thread = None
    if "scan_stop_event" not in st.session_state:
        st.session_state.scan_stop_event = None
    if "scan_status" not in st.session_state:
        st.session_state.scan_status = "idle"


def _scan_worker(cfg: dict, stop_event: threading.Event) -> None:
    with _SCAN_LOCK:
        _SCAN_STATE["status"] = "running"
    try:
        run_inventory(
            cfg["ip"],
            cfg["port"],
            cfg["timeout"],
            cfg["antennas"],
            cfg["duration"],
            cfg["afficher_antennes"],
            stop_event=stop_event,
            client_holder=None,
        )
    finally:
        with _SCAN_LOCK:
            _SCAN_STATE["status"] = "idle"


_init_state()

st.title("📡 Mini test RFID - Start/Stop")

with st.form("rfid_config"):
    ip_addr = st.text_input("Adresse IP", value=DEFAULT_IP)
    port = st.number_input("Port", min_value=1, max_value=65535, value=DEFAULT_PORT)
    timeout = st.number_input("Timeout (s)", min_value=0.1, step=0.1, value=DEFAULT_TIMEOUT)
    antennas_raw = st.text_input(
        "Antennes (séparées par virgules)",
        value=",".join(str(a) for a in DEFAULT_ANTENNAS),
    )
    duration = st.number_input("Durée (s, 0=infini)", min_value=0, value=0)
    afficher_antennes = st.checkbox("Afficher les antennes", value=False)
    submitted = st.form_submit_button("Mettre à jour")

try:
    antennas = parse_antennas(antennas_raw)
except Exception as exc:
    st.error(f"Antennes invalides: {exc}")
    antennas = DEFAULT_ANTENNAS

cfg = {
    "ip": ip_addr,
    "port": int(port),
    "timeout": float(timeout),
    "antennas": antennas,
    "duration": int(duration),
    "afficher_antennes": afficher_antennes,
}

thread = st.session_state.scan_thread
with _SCAN_LOCK:
    shared_status = _SCAN_STATE["status"]
if thread is not None and thread.is_alive() and shared_status == "idle":
    shared_status = "running"
st.session_state.scan_status = shared_status
status = shared_status
st.info(f"Statut: {status}")

col_start, col_stop = st.columns(2)

with col_start:
    if st.button("🚀 Démarrer", use_container_width=True, disabled=status in {"starting", "running", "stopping"}):
        stop_event = threading.Event()
        thread = threading.Thread(target=_scan_worker, args=(cfg, stop_event), daemon=True)
        st.session_state.scan_stop_event = stop_event
        st.session_state.scan_thread = thread
        st.session_state.scan_status = "starting"
        with _SCAN_LOCK:
            _SCAN_STATE["status"] = "starting"
        thread.start()
        st.rerun()

with col_stop:
    if st.button("⏹ Arrêter", use_container_width=True, disabled=status == "idle"):
        stop_event = st.session_state.scan_stop_event
        if stop_event is not None:
            st.session_state.scan_status = "stopping"
            with _SCAN_LOCK:
                _SCAN_STATE["status"] = "stopping"
            stop_event.set()
        st.rerun()

if status in {"starting", "running", "stopping"}:
    st.caption("Le scan tourne. Cliquez sur Arrêter pour déclencher client.disconnect().")
    time.sleep(0.5)
    st.rerun()
