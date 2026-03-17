# Unit3Dup — Fork G3MINI

Fork de [Unit3Dup](https://github.com/31December99/Unit3Dup) adapté pour **G3MINI Tracker**.

Ce fork ajoute la normalisation automatique des noms de release selon les conventions du tracker, la détection du flag `personal_release` par tag d'équipe, et le nettoyage automatique des fichiers `.nfo` orphelins.

---

## Fonctionnalités ajoutées

- **Normalisation des noms de release** : les noms sont automatiquement reformatés selon les conventions G3MINI (`Titre.Année.Langue.Résolution.HDR.Source.Audio.Codec-TEAM`)
- **Détection `personal_release`** : si le tag de la release (ex: `-KFL`) correspond à un tag configuré dans `TAGS_TEAM`, le champ `personal_release` est automatiquement coché à l'upload
- **Nettoyage des `.nfo` orphelins** : le watcher supprime automatiquement les fichiers `.nfo` isolés après traitement

---

## Installation

### Prérequis

```bash
sudo apt install ffmpeg python3 python3-pip python3-venv git
```

### Cloner le repo

```bash
git clone https://github.com/lantiumBot/Unit3Dup-G3MINI /opt/unit3dup
cd /opt/unit3dup
```

> Le chemin `/opt/unit3dup` est utilisé dans les exemples ci-dessous. Si tu clones ailleurs, adapte-le partout (notamment dans le wrapper).

### Créer un environnement virtuel et installer

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

L'option `-e` (editable) permet de recevoir les mises à jour du fork simplement avec un `git pull`, sans réinstaller.

### Vérifier l'installation

Avant de configurer quoi que ce soit, vérifie que le tool est bien accessible :

```bash
unit3dup --help
```

Si la commande n'est pas trouvée, c'est que le venv n'est pas activé (`source venv/bin/activate`) ou que le wrapper n'est pas en place (voir ci-dessous).

---

### Wrapper (optionnel mais recommandé)

Le wrapper permet d'utiliser `unit3dup` depuis n'importe où **sans activer le venv manuellement** à chaque fois.

Ouvre le fichier `unit3dup-wrapper.sh` et vérifie que le chemin vers le repo est correct :

```bash
nano /opt/unit3dup/unit3dup-wrapper.sh
```

Cherche la ligne qui définit le chemin du projet et assure-toi qu'elle correspond à l'endroit où tu as cloné le repo (par défaut `/opt/unit3dup`).

Ensuite, rends-le exécutable et crée le symlink :

```bash
chmod +x /opt/unit3dup/unit3dup-wrapper.sh
sudo ln -s /opt/unit3dup/unit3dup-wrapper.sh /usr/local/bin/unit3dup
```

Vérifie que ça fonctionne :

```bash
which unit3dup
unit3dup --help
```

---

## Configuration

### Étape 1 — Générer la configuration initiale

Au premier lancement, unit3dup crée automatiquement le dossier `~/Unit3Dup_config/` avec un fichier `Unit3Dbot.json` pré-rempli :

```bash
unit3dup --help
```

### Étape 2 — Remplir la configuration

```bash
nano ~/Unit3Dup_config/Unit3Dbot.json
```

Les champs essentiels à renseigner :

| Champ | Description | Requis |
|---|---|---|
| `Gemini_URL` | URL de G3MINI | ✅ |
| `Gemini_APIKEY` | Clé API (profil G3MINI) | ✅ |
| `Gemini_PID` | Ton passkey | ✅ |
| `TMDB_APIKEY` | Clé gratuite sur [themoviedb.org](https://www.themoviedb.org/settings/api) | ✅ |
| `WATCHER_PATH` | Chemin vers ton dossier de watch | ✅ |
| `WATCHER_DESTINATION_PATH` | Chemin de destination des fichiers `.torrent` | ✅ |
| `IMGBB_KEY` | Clé gratuite sur [imgbb.com](https://imgbb.com) pour les screenshots | ⬜ optionnel |

> **Note :** `IMGBB_KEY` est optionnel. Si absent, les screenshots ne seront pas uploadés mais le reste fonctionnera normalement.

> **Permissions :** Assure-toi que l'utilisateur qui lance unit3dup a bien les droits en lecture sur `WATCHER_PATH` et en écriture sur `WATCHER_DESTINATION_PATH`. Si ces dossiers sont sur un montage NFS ou un partage réseau, vérifie aussi que le montage est actif avant de lancer le watcher.

### Étape 3 — Ajouter tes tags d'équipe

La section `uploader_tag` n'est **pas générée automatiquement**, il faut l'ajouter manuellement dans le JSON :

```json
"uploader_tag": {
    "TAGS_TEAM": ["MONTAG"]
}
```

Si ta release se termine par `-MONTAG`, le champ `personal_release` sera automatiquement activé à l'upload. Tu peux mettre plusieurs tags dans le tableau.

---

## Utilisation

```bash
# Uploader un fichier
unit3dup -u /chemin/vers/fichier.mkv

# Uploader un dossier entier
unit3dup -f /chemin/vers/dossier

# Scanner un dossier
unit3dup -scan /chemin/vers/dossier
```

---

## Mise à jour

```bash
cd /opt/unit3dup
git pull
```

Pas besoin de réinstaller grâce au mode `-e`. Si des nouvelles dépendances ont été ajoutées :

```bash
source venv/bin/activate
pip install -e .
```

---

## Projet original

Ce fork est basé sur [Unit3Dup](https://github.com/31December99/Unit3Dup) — licence MIT.