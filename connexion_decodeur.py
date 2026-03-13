"""Inventaire RFID en direct sur lecteur Impinj R220 via LLRP.

Prérequis (Python 3.10+ / testé avec 3.13):
	pip install sllurp

Exemple:
	python connexion_decodeur.py

Option multi-antennes:
	python connexion_decodeur.py --antennas 1,2
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
import time
from datetime import datetime
from threading import Event
from typing import Optional


DEFAULT_IP = "169.254.1.1"
DEFAULT_PORT = 5084  # Port LLRP standard (lecteurs Impinj)
DEFAULT_TIMEOUT = 5.0
DEFAULT_ANTENNAS = [1, 2]


def parse_antennas(value: str) -> list[int]:
	"""Parse une liste d'antennes au format '1,2,3'."""
	params = [part.strip() for part in value.split(",") if part.strip()]
	if not params:
		raise argparse.ArgumentTypeError("Aucune antenne fournie.")

	antennas: list[int] = []
	for num_antenne in params:
		try:
			antenna = int(num_antenne)
		except ValueError as exc:
			raise argparse.ArgumentTypeError(f"Antenne invalide: {num_antenne}") from exc

		if antenna <= 0:
			raise argparse.ArgumentTypeError(f"Antenne invalide: {antenna}")

		if antenna not in antennas:
			antennas.append(antenna)

	return antennas


def addresse_ipv4_valide(ip: str) -> Optional[str]:
	"""Valide une adresse IPv4. Retourne un message d'erreur si invalide."""
	try:
		ipaddress.IPv4Address(ip)
		return None
	except ipaddress.AddressValueError:
		return (
			f"Adresse IPv4 invalide: {ip}. "
			"Verifie les octets (0-255). Exemple plausible: 169.254.1.1"
		)


def extract_epc(tag: dict) -> str:
	if "EPC-96" in tag:
		return str(tag["EPC-96"])
	epc_data = tag.get("EPCData")
	if isinstance(epc_data, dict) and "EPC" in epc_data:
		return str(epc_data["EPC"])
	if "EPC" in tag:
		return str(tag["EPC"])
	return "EPC_INCONNU"

def on_tag_report(_reader, tags, antennas, affiche_antennes=False):
	if isinstance(tags, dict):
		tags = [tags]
	if not isinstance(tags, list):
		return

	for tag in tags:
		if not isinstance(tag, dict):
			continue
		epc = extract_epc(tag)
		rssi = tag.get("PeakRSSI", "?")
		ant_id = tag.get("AntennaID", antennas[0])
		seen = tag.get("TagSeenCount", 1)
		now = datetime.now().strftime("%H:%M:%S")
		if affiche_antennes:
			print(f"[{now}] EPC={epc} | RSSI={rssi} | ANT={ant_id} | Seen={seen}")

def run_inventory(
	ip: str,
	port: int,
	timeout: float,
	antennas: list[int],
	duration: int,
	afficher_antennes: bool = False) -> int:
	error = addresse_ipv4_valide(ip)
	if error:
		print(f"[ERREUR] {error}")
		return 2

	try:
		from sllurp.llrp import LLRPReaderClient, LLRPReaderConfig
	except ImportError:
		print("[ERREUR] Dépendances manquantes.")
		print("Installe: pip install sllurp")
		return 10
	except Exception as exc:
		print(f"[ERREUR] Impossible d'importer sllurp ({exc.__class__.__name__}): {exc}")
		return 10

	print(f"Connexion LLRP a {ip}:{port} (antennes={','.join(str(a) for a in antennas)})...")
	print("Lecture des tags en cours (Ctrl+C pour arrrter).")

	disconnected = Event()

	def on_disconnected(_reader):
		disconnected.set()

	config = LLRPReaderConfig(
		{
			"start_inventory": True,
			"report_every_n_tags": 1,
			"antennas": antennas,
			"duration": None if duration == 0 else duration,
			"reconnect": False,
			"disconnect_when_done": False if duration == 0 else True,
		}
	)

	client = LLRPReaderClient(ip, port=port, config=config, timeout=timeout)
	client.add_tag_report_callback(on_tag_report, antennas=antennas)
	client.add_disconnected_callback(on_disconnected)

	try:
		client.connect()
		while not disconnected.is_set():
			time.sleep(0.2)
		return 0
	except KeyboardInterrupt:
		print("\nArret demande par l'utilisateur.")
		try:
			client.disconnect()
		except Exception:
			pass
		return 0
	except Exception as exc:
		print(f"[ERREUR] Inventaire impossible ({exc.__class__.__name__}): {exc}")
		try:
			client.disconnect()
		except Exception:
			pass
		return 11


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Lit les tags RFID en direct depuis un lecteur Impinj R220 (LLRP).")
	parser.add_argument("--ip", default=DEFAULT_IP, help="Adresse IPv4 du lecteur")
	parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port TCP (LLRP: 5084)")
	parser.add_argument(
		"--timeout",
		type=float,
		default=DEFAULT_TIMEOUT,
		help="Timeout reseau en secondes",
	)
	parser.add_argument(
		"--antennas",
		type=parse_antennas,
		default=DEFAULT_ANTENNAS,
		help="Liste d'antennes, ex: 1,2 (defaut: 1,2)",
	)
	parser.add_argument(
		"--duration",
		type=int,
		default=0,
		help="Duree en secondes (0 = infini)",
	)
	parser.add_argument(
		"--afficher-antennes",
		action="store_true",
		help="Afficher les antennes dans les rapports de tags",
	)
	return parser


def main() -> int:
	parser = build_parser()
	args = parser.parse_args()

	if args.port < 1 or args.port > 65535:
		print(f"[ERREUR] Port invalide: {args.port}")
		return 2

	if args.timeout <= 0:
		print(f"[ERREUR] Timeout invalide: {args.timeout}")
		return 2

	if not args.antennas:
		print("[ERREUR] Aucune antenne valide.")
		return 2

	if args.duration < 0:
		print(f"[ERREUR] Duree invalide: {args.duration}")
		return 2
	
	if args.afficher_antennes:
		print("Affichage des antennes actif.")
	else :
		print("Affichage des antennes inactif.")

	return run_inventory(args.ip, args.port, args.timeout, args.antennas, args.duration, afficher_antennes=args.afficher_antennes)

if __name__ == "__main__":
	sys.exit(main())
