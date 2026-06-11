"""Inventaire RFID en direct sur lecteur Impinj R220 via LLRP.

Prérequis (Python 3.10+ / testé avec 3.13):
	pip install sllurp

Commandes:
	python connexion_decodeur.py scan [--ip ...] [--preset nom]
	python connexion_decodeur.py preset save <nom> --ip ... --port ...
	python connexion_decodeur.py preset list
	python connexion_decodeur.py preset load <nom>
	python connexion_decodeur.py preset delete <nom>
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Optional


# ── Chemins des fichiers de stockage ──────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
PRESETS_FILE = SCRIPT_DIR / "presets.json"
TAGS_FILE = SCRIPT_DIR / "tags.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

# ── Valeurs par défaut ────────────────────────────────────────────────
# DEFAULT_IP = "169.254.1.1"
DEFAULT_IP = "10.42.0.15"
DEFAULT_PORT = 5084  # Port LLRP standard (lecteurs Impinj)
DEFAULT_TIMEOUT = 5.0
DEFAULT_ANTENNAS = [1, 2]


# =====================================================================
#  Utilitaires JSON
# =====================================================================

def load_json(path: Path) -> dict:
	"""Charge un fichier JSON. Retourne {} si le fichier n'existe pas."""
	if path.exists():
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	return {}


def save_json(path: Path, data: dict) -> None:
	"""Écrit un dictionnaire dans un fichier JSON."""
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as f:
			json.dump(data, f, indent=2, ensure_ascii=False)
			f.flush()
			os.fsync(f.fileno())
		Path(tmp_name).replace(path)
	finally:
		try:
			Path(tmp_name).unlink(missing_ok=True)
		except Exception:
			pass


# =====================================================================
#  Format presets.json 
# =====================================================================

def get_presets_root() -> dict:
	"""Retourne le JSON complet de presets.json"""
	return load_json(PRESETS_FILE)


def get_presets() -> dict:
	"""Retourne le mapping des presets (nom -> config)."""
	return get_presets_root()["presets"]


def get_last_used_preset() -> Optional[str]:
	"""Retourne le nom du dernier preset utilisé, ou None si aucun ou invalide."""
	root = get_presets_root()
	value = root.get("last_used_preset")
	return value if isinstance(value, str) and value.strip() else None


def set_last_used_preset(name: Optional[str]) -> None:
	"""Enregistre le nom du dernier preset utilisé (ou None pour réinitialiser)."""
	root = get_presets_root()
	if name is None or not str(name).strip():
		root["last_used_preset"] = None
	else:
		root["last_used_preset"] = str(name).strip()
	save_json(PRESETS_FILE, root)


def get_storage_preferences(preset_name: Optional[str] = None) -> dict[str, str]:
	"""Retourne les préférences concernant le nom des fichiers de stockage de configuration selon un preset donné.
	"""
	if not preset_name:
		return {}
	root = get_presets_root()
	cfg = root["presets"].get(preset_name)
	if isinstance(cfg, dict):
		prefs = cfg.get("storage_preferences")
		if isinstance(prefs, dict):
			return {
				"presets_filename": str(prefs.get("presets_filename", "")).strip(),
				"tags_filename": str(prefs.get("tags_filename", "")).strip(),
				"config_filename": str(prefs.get("config_filename", "")).strip(),
			}
	return {}


def update_preset_storage_preferences(preset_name: str, prefs: dict[str, str]) -> bool:
	"""Met à jour les préférences de nom de fichiers de stockage de configuration d'un preset existant.

	Retourne False si le preset n'existe pas.
	"""
	root = get_presets_root()
	if preset_name not in root["presets"] or not isinstance(root["presets"].get(preset_name), dict):
		return False
	root["presets"][preset_name]["storage_preferences"] = {
		"presets_filename": str(prefs.get("presets_filename", "")).strip(),
		"tags_filename": str(prefs.get("tags_filename", "")).strip(),
		"config_filename": str(prefs.get("config_filename", "")).strip(),
	}
	save_json(PRESETS_FILE, root)
	return True


# =====================================================================
#  Validation
# =====================================================================

def parse_antennas(value: str) -> list[int]:
	"""Parse une liste d'antennes au format '1,2,3'. et retourne une liste."""
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


def valider_parametres(ip: str, port: int, timeout: float,
                       antennas: list[int], duration: int) -> list[str]:
	"""Vérifie tous les paramètres du lecteur. Retourne la liste des erreurs."""
	erreurs: list[str] = []

	err_ip = addresse_ipv4_valide(ip)
	if err_ip:
		erreurs.append(err_ip)
	if port < 1 or port > 65535:
		erreurs.append(f"Port invalide: {port} (attendu: 1-65535)")
	if timeout <= 0:
		erreurs.append(f"Timeout invalide: {timeout} (doit etre > 0)")
	if not antennas:
		erreurs.append("Aucune antenne valide.")
	if duration < 0:
		erreurs.append(f"Duree invalide: {duration} (doit etre >= 0)")

	return erreurs


# =====================================================================
#  Gestion de la configuration lecteur / antennes
# =====================================================================

def sauvegarder_config(ip: str, port: int, timeout: float,
                       antennas: list[int], duration: int,
                       afficher_antennes: bool) -> None:
	"""Enregistre la config courante du lecteur dans config.json."""
	config = {
		"ip": ip,
		"port": port,
		"timeout": timeout,
		"antennas": antennas,
		"duration": duration,
		"afficher_antennes": afficher_antennes,
	}
	save_json(CONFIG_FILE, config)
	print(f"Configuration enregistree dans {CONFIG_FILE.name}")


# =====================================================================
#  Gestion des presets
# =====================================================================

def sauvegarder_preset(name: str, ip: str, port: int, timeout: float,
                       antennas: list[int], duration: int,
					   afficher_antennes: bool,
					   global_tx_power: Optional[float] = None,
					   global_rx_sensitivity: Optional[float] = None,
					   antenna_params: Optional[dict] = None,
					   storage_preferences: Optional[dict[str, str]] = None,
					   set_as_last: bool = False) -> None:
	"""Sauvegarde un preset dans presets.json."""
	root = get_presets_root()
	presets = root["presets"]
	preset_data = {
		"ip": ip,
		"port": port,
		"timeout": timeout,
		"antennas": antennas,
		"duration": duration,
		"afficher_antennes": afficher_antennes,
	}
	if global_tx_power is not None:
		preset_data["global_tx_power"] = global_tx_power
	if global_rx_sensitivity is not None:
		preset_data["global_rx_sensitivity"] = global_rx_sensitivity
	if antenna_params is not None:
		preset_data["antenna_params"] = antenna_params
	if storage_preferences is not None:
		preset_data["storage_preferences"] = {
			"presets_filename": str(storage_preferences.get("presets_filename", "")).strip(),
			"tags_filename": str(storage_preferences.get("tags_filename", "")).strip(),
			"config_filename": str(storage_preferences.get("config_filename", "")).strip(),
		}

	presets[name] = preset_data
	if set_as_last:
		root["last_used_preset"] = name
	save_json(PRESETS_FILE, root)
	print(f"Preset '{name}' sauvegarde.")


def charger_preset(name: str) -> Optional[dict]:
	"""Charge un preset. Retourne None si introuvable."""
	root = get_presets_root()
	presets = root["presets"]
	if name not in presets:
		print(f"[ERREUR] Preset '{name}' introuvable.")
		return None
	print(f"Preset '{name}' charge.")
	return presets[name]


def supprimer_preset(name: str) -> bool:
	"""Supprime un preset. Retourne True si succès."""
	root = get_presets_root()
	presets = root["presets"]
	if name not in presets:
		print(f"[ERREUR] Preset '{name}' introuvable.")
		return False
	del presets[name]
	if root.get("last_used_preset") == name:
		root["last_used_preset"] = None
	save_json(PRESETS_FILE, root)
	print(f"Preset '{name}' supprime.")
	return True


def lister_presets() -> None:
	"""Affiche tous les presets enregistrés."""
	root = get_presets_root()
	presets = root["presets"]
	if not presets:
		print("Aucun preset enregistre.")
		return
	print(f"{'Nom':<15} {'IP':<16} {'Port':<6} {'Antennes':<10} {'Duree':<6} {'Afficher ant.'}")
	print("-" * 70)
	for name, cfg in presets.items():
		ant_str = ",".join(str(a) for a in cfg["antennas"])
		aff = "oui" if cfg.get("afficher_antennes") else "non"
		print(f"{name:<15} {cfg['ip']:<16} {cfg['port']:<6} {ant_str:<10} {cfg['duration']:<6} {aff}")


# =====================================================================
#  Stockage des tags lus
# =====================================================================

def enregistrer_tag(epc: str, rssi, ant_id: int, seen: int, reader_timestamp=None) -> None:
	"""Ajoute un tag lu dans tags.json."""
	data = load_json(TAGS_FILE)
	if "tags" not in data:
		data["tags"] = []
	data["tags"].append({
		"epc": epc,
		"rssi": rssi,
		"antenna": ant_id,
		"seen_count": seen,
		"reader_timestamp": reader_timestamp,
		"client_timestamp": datetime.now().isoformat(),
	})
	save_json(TAGS_FILE, data)


# =====================================================================
#  Extraction EPC & callback tag
# =====================================================================

def extract_epc(tag: dict) -> str:
	"""Extrait l'EPC d'un tag."""
	if "EPC-96" in tag:
		return str(tag["EPC-96"])
	epc_data = tag.get("EPCData")
	if isinstance(epc_data, dict) and "EPC" in epc_data:
		return str(epc_data["EPC"])
	if "EPC" in tag:
		return str(tag["EPC"])
	return "EPC_INCONNU"


def on_tag_report(_reader, tags, antennas, affiche_antennes=False):
	"""Traite les tags reçus du lecteur. Affiche les informations et les stocke dans un fichier JSON."""
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
		
		# Récupérer le timestamp du lecteur (format: secondes*1e6 + microsecondes)
		reader_timestamp = tag.get("LastSeenTimestampUTC") or tag.get("FirstSeenTimestampUTC") or tag.get("Timestamp")
		print(f"[{reader_timestamp}] EPC={epc} | RSSI={rssi} | ANT={ant_id} | Seen={seen}")
		if reader_timestamp and isinstance(reader_timestamp, (int, float)):
			try:
				# Diviser par 1e6 pour obtenir les secondes (format Impinj: secondes*1e6 + µs)
				ts_seconds = int(reader_timestamp) // 1000000
				# convertit automatiquement l'heure du lecteur impinj en heure locale et formate l'affichage
				ts_str = datetime.fromtimestamp(ts_seconds).strftime("%H:%M:%S.%f")[:-3]
			except (ValueError, OSError, OverflowError):
				ts_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
		else:
			ts_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

		# Stockage dans le fichier JSON
		enregistrer_tag(epc, rssi, ant_id, seen, reader_timestamp)
		
		print(f"[{ts_str}] EPC={epc} | RSSI={rssi} | ANT={ant_id} | Seen={seen}")


# =====================================================================
#  Inventaire RFID
# =====================================================================

def run_inventory(ip: str, port: int, timeout: float, antennas: list[int],
				  duration: int, afficher_antennes: bool = False,
				  stop_event: Optional[Event] = None,
				  client_holder: Optional[dict] = None) -> int:
	# Validation complète des paramètres
	erreurs = valider_parametres(ip, port, timeout, antennas, duration)
	if erreurs:
		for e in erreurs:
			print(f"[ERREUR] {e}")
		return 2

	try:
		from sllurp.llrp import LLRPReaderClient, LLRPReaderConfig
	except ImportError:
		print("[ERREUR] Dependances manquantes.")
		print("Installe: pip install sllurp")
		return 10
	except Exception as exc:
		print(f"[ERREUR] Impossible d'importer sllurp ({exc.__class__.__name__}): {exc}")
		return 10

	# Sauvegarde automatique de la config utilisée
	sauvegarder_config(ip, port, timeout, antennas, duration, afficher_antennes)

	print(f"Connexion LLRP a {ip}:{port} (antennes={','.join(str(a) for a in antennas)})...")
	print("Lecture des tags en cours (Ctrl+C pour arreter).")

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
			"tag_content_selector": {
				"EnableAntennaID": True,
				"EnablePeakRSSI": True,
				"EnableTagSeenCount": True,
			},
			'EnableFirstSeenTimestamp': True,
			'EnableLastSeenTimestamp': True,
			'EnableTagSeenCount': True
		}
	)

	client = LLRPReaderClient(ip, port=port, config=config, timeout=timeout)
	if client_holder is not None:
		client_holder["client"] = client

	def tag_report_callback(reader, tags):
		on_tag_report(reader, tags, antennas, afficher_antennes)

	client.add_tag_report_callback(tag_report_callback)
	client.add_disconnected_callback(on_disconnected)

	try:
		client.connect()
		start_time = time.monotonic()
		while not disconnected.is_set():
			if stop_event is not None and stop_event.is_set():
				try:
					client.disconnect()
				except Exception:
					pass
				break
			if duration > 0 and (time.monotonic() - start_time) >= duration:
				try:
					client.disconnect()
				except Exception:
					pass
				break
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


# =====================================================================
#  CLI – sous-commandes  (scan / preset)
# =====================================================================

def _add_scan_args(parser: argparse.ArgumentParser) -> None:
	"""Ajoute les arguments de scan au parser."""
	parser.add_argument("--ip", default=DEFAULT_IP, help="Adresse IPv4 du lecteur")
	parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port TCP (LLRP: 5084)")
	parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
	                    help="Timeout reseau en secondes")
	parser.add_argument("--antennas", type=parse_antennas, default=DEFAULT_ANTENNAS,
	                    help="Liste d'antennes, ex: 1,2 (defaut: 1,2)")
	parser.add_argument("--duration", type=int, default=0,
	                    help="Duree en secondes (0 = infini)")
	parser.add_argument("--afficher-antennes", action="store_true",
	                    help="Afficher les antennes dans les rapports de tags")


def build_parser() -> argparse.ArgumentParser:
	"""Construit le parser d'arguments pour le CLI."""
	parser = argparse.ArgumentParser(
		description="Lecteur RFID Impinj R220 – inventaire LLRP avec gestion de presets.")
	subparsers = parser.add_subparsers(dest="command")

	# scan
	scan_p = subparsers.add_parser("scan", help="Lancer l'ecoute RFID")
	_add_scan_args(scan_p)
	scan_p.add_argument("--preset", type=str, default=None,
	                    help="Charger un preset avant le scan")

	# preset 
	preset_p = subparsers.add_parser("preset", help="Gestion des presets")
	preset_sub = preset_p.add_subparsers(dest="preset_action")

	# preset save
	save_p = preset_sub.add_parser("save", help="Sauvegarder un preset")
	save_p.add_argument("name", help="Nom du preset")
	_add_scan_args(save_p)

	# preset load
	load_p = preset_sub.add_parser("load", help="Afficher un preset")
	load_p.add_argument("name", help="Nom du preset")

	# preset delete
	del_p = preset_sub.add_parser("delete", help="Supprimer un preset")
	del_p.add_argument("name", help="Nom du preset")

	# preset list
	preset_sub.add_parser("list", help="Lister les presets")

	return parser


# =====================================================================
#  Point d'entrée
# =====================================================================

def main() -> int:
	parser = build_parser()
	args = parser.parse_args()

	# --- Commande : preset ----------------------------------------
	if args.command == "preset":
		if args.preset_action == "save":
			erreurs = valider_parametres(args.ip, args.port, args.timeout,
			                             args.antennas, args.duration)
			if erreurs:
				for e in erreurs:
					print(f"[ERREUR] {e}")
				return 2
			sauvegarder_preset(args.name, args.ip, args.port, args.timeout,
			                   args.antennas, args.duration, args.afficher_antennes)
			return 0

		if args.preset_action == "load":
			cfg = charger_preset(args.name)
			if cfg is None:
				return 1
			ant_str = ",".join(str(a) for a in cfg["antennas"])
			print(f"  ip             : {cfg['ip']}")
			print(f"  port           : {cfg['port']}")
			print(f"  timeout        : {cfg['timeout']}")
			print(f"  antennas       : {ant_str}")
			print(f"  duration       : {cfg['duration']}")
			print(f"  afficher_ant.  : {'oui' if cfg.get('afficher_antennes') else 'non'}")
			return 0

		if args.preset_action == "delete":
			return 0 if supprimer_preset(args.name) else 1

		if args.preset_action == "list":
			lister_presets()
			return 0

		parser.parse_args(["preset", "--help"])
		return 1

	# --- Commande : scan (ou par défaut) --------------------------
	# Si aucune commande -> scan par défaut
	if args.command is None:
		args = parser.parse_args(["scan"])

	# Si un preset est demandé, on charge ses valeurs
	if args.preset:
		cfg = charger_preset(args.preset)
		if cfg is None:
			return 1
		args.ip = cfg["ip"]
		args.port = cfg["port"]
		args.timeout = cfg["timeout"]
		args.antennas = cfg["antennas"]
		args.duration = cfg["duration"]
		args.afficher_antennes = cfg.get("afficher_antennes", False)

	# Validation
	erreurs = valider_parametres(args.ip, args.port, args.timeout,
	                             args.antennas, args.duration)
	if erreurs:
		for e in erreurs:
			print(f"[ERREUR] {e}")
		return 2

	if args.afficher_antennes:
		print("Affichage des antennes actif.")

	return run_inventory(args.ip, args.port, args.timeout, args.antennas,
	                     args.duration, afficher_antennes=args.afficher_antennes)


if __name__ == "__main__":
	sys.exit(main())