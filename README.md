# INFO0806_projet

Système de décompte du passage d’individus par une barrière RFID et analyse des effets des antennes.

Ce projet met en place une application de lecture RFID permettant de suivre le passage de coureurs ou d’individus équipés de tags EPC, de configurer le lecteur, d’exécuter des scans en temps réel et d’analyser les lectures obtenues selon les antennes utilisées.

## Objectif

Le sujet de base du projet est :

**Mise en place d’un système de décompte du passage d’individus grâce à des antennes RFID**

L’objectif est de :
- détecter le passage d’un individu grâce à un tag RFID,
- compter les lectures associées à un passage,
- centraliser et visualiser les résultats dans une interface simple.

## Fonctionnalités

- Lancement de scans RFID sur un lecteur Impinj R220.
- Gestion de presets de configuration.
- Arrêt propre des scans en cours.
- Journalisation des sorties terminal dans des fichiers de log horodatés.
- Gestion d’une whitelist d’EPC.
- Analyse et affichage des tags lus sous forme de tableau.
- Interface Streamlit pour piloter l’ensemble depuis un navigateur.

## Architecture

Le projet est composé de deux parties principales :

- [streamlit_app.py](c:\Users\Mathieu\Documents\cours\INFO0806_projet\streamlit_app.py) : interface web Streamlit, gestion des scans, logs, whitelist et affichage des données.
- [connexion_decodeur.py](c:\Users\Mathieu\Documents\cours\INFO0806_projet\connexion_decodeur.py) : logique de connexion au lecteur RFID et lancement des inventaires via LLRP.

## Prérequis

- Python 3.10 ou supérieur.
- Un lecteur RFID compatible Impinj / LLRP.
- Un environnement réseau permettant d’atteindre le lecteur.
- Les bibliothèques Python utilisées par le projet.

Dépendances principales utilisées par le code :
- streamlit
- pandas
- sllurp

## Installation

Créer et activer un environnement virtuel, puis installer les dépendances nécessaires :

```bash
python -m venv .venv
.venv\Scripts\activate
pip install streamlit pandas sllurp
```

Si vous disposez d’un fichier de dépendances, vous pouvez aussi l’utiliser à la place.

## Lancement de l’application
Démarrer l’interface Streamlit :

```bash
streamlit run streamlit_app.py
```

L’application permet ensuite de :

- configurer le lecteur,
- charger ou enregistrer des presets,
- lancer un scan,
- consulter les lectures et les logs.

## Utilisation en ligne de commande
Le script connexion_decodeur.py peut aussi être utilisé directement pour exécuter un inventaire RFID ou gérer les presets.

Exemples :

```bash
python connexion_decodeur.py scan --ip 10.42.0.15 --port 5084 --duration 10
python connexion_decodeur.py preset list
python connexion_decodeur.py preset save mon_preset --ip 10.42.0.15 --port 5084
```

## Données et fichiers générés
Le projet utilise plusieurs fichiers JSON et fichiers de sortie :

- config.json : configuration générale.
- presets.json : presets de connexion et de scan.
- whitelist.json : EPC autorisés et éventuelles correspondances avec des noms.
- tags_course.json : tags lus et historisés.
- tags_course8.json et variantes : jeux de données de course.
- rfid_terminal_live.log : log courant.
- rfid_terminal_archive.log : archive des logs.

## Principe de fonctionnement
L’utilisateur configure le lecteur RFID et choisit les antennes à utiliser.
L’application lance un scan en arrière-plan pour ne pas bloquer l’interface.
Les tags détectés sont récupérés et enregistrés.
Les logs du terminal sont conservés avec horodatage.
Les données peuvent ensuite être analysées pour étudier l’effet des antennes sur la détection.

## Notes
Le projet est conçu autour d’un lecteur RFID Impinj R220.
Les scans sont gérés de manière à pouvoir être arrêtés proprement.
Chaque session utilisateur possède un identifiant unique pour éviter les conflits entre instances.

Neo KRZANOWSKI
Mathieu STEPHAN

Projet réalisé dans le cadre d’INFO0806.
