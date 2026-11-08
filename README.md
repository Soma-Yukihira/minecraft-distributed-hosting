# Minecraft Distributed Hosting

**Hébergement Minecraft partagé entre les ordinateurs d'un groupe, avec une adresse unique.**

Minecraft Distributed Hosting permet à un groupe de joueurs d'héberger un serveur Minecraft Java
sur ses propres ordinateurs, sans serveur dédié coûteux. Le serveur tourne sur l'un des PC du
groupe et passe automatiquement sur un autre quand c'est nécessaire, avec le même monde et la
même adresse pour les joueurs.

Un petit VPS coordonne l'ensemble : il gère les comptes, désigne l'ordinateur qui héberge,
conserve les sauvegardes du monde et relaie les connexions des joueurs. Il ne fait jamais
tourner Minecraft lui-même.

---

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Fonctionnement](#fonctionnement)
- [Prérequis](#prérequis)
- [Installation du serveur central](#installation-du-serveur-central)
- [Installation de l'application](#installation-de-lapplication)
- [Prise en main](#prise-en-main)
- [Connexion des joueurs](#connexion-des-joueurs)
- [Publication d'une nouvelle version](#publication-dune-nouvelle-version)
- [Sécurité](#sécurité)
- [Développement](#développement)
- [Limites connues](#limites-connues)
- [Licence](#licence)

---

## Fonctionnalités

### Hébergement
- Hébergement tournant entre les PC du groupe, selon une priorité définie ou la puissance de
  chaque machine.
- Changement d'hôte sans déconnexion des joueurs (Minecraft 1.20.5 et versions ultérieures).
- Reprise rapide après une panne grâce aux copies de secours tenues à jour sur les autres PC.
- Démarrage automatique à la première connexion d'un joueur, arrêt automatique quand le serveur
  est vide.
- Mise en veille du PC hôte empêchée pendant l'hébergement.

### Monde et sauvegardes
- Sauvegardes incrémentales pendant la partie, historique des versions et restauration.
- Import d'un monde existant et export au format `.zip`.
- Création d'un nouveau monde (graine aléatoire ou choisie), l'ancien restant restaurable.
- Plusieurs serveurs par groupe.
- Pré-génération du monde avec Chunky.

### Mods et plugins
- Serveurs Vanilla, Paper (plugins) et Fabric (mods).
- Mods, plugins, modpacks et datapacks issus de Modrinth, vérifiés par empreinte et approuvés par
  chaque membre sur son propre PC.
- Mise à jour des mods en un clic, pack de ressources, fichier d'installation des mods pour les
  joueurs.
- Carte du monde dans le navigateur (BlueMap).

### Modération et protection
- Anti-triche côté serveur (Grim) et anti-X-ray.
- Liste blanche, demandes d'accès, rôles (propriétaire, administrateur, modérateur, membre).
- Expulsion, bannissement et commandes depuis l'application.
- Protection des pseudos, y compris avec des comptes non officiels.

### Supervision
- Console et journal du serveur en temps réel.
- Statistiques, graphiques de performance et analyse du lag (spark).
- Diagnostic des plantages, avec identification du mod en cause.
- Page de statut publique et alertes du serveur central.

### Application
- Disponible pour Windows et macOS (puces Apple).
- Mises à jour automatiques et signées, détectées dès leur publication.

---

## Fonctionnement

```
          Joueurs (Minecraft Java)
                    │
                    │  adresse unique : serveur.203.0.113.10.nip.io
                    ▼
     ┌──────────────────────────────┐
     │      Serveur central (VPS)   │   comptes, choix de l'hôte,
     │    relais Minecraft + API    │   sauvegardes du monde
     └──────────────────────────────┘
          ▲            ▲            ▲
          │  connexions sortantes uniquement (aucun port à ouvrir)
     ┌──────────┐ ┌──────────┐ ┌──────────┐
     │   PC A   │ │   PC B   │ │   PC C   │
     │ héberge  │ │en attente│ │  éteint  │
     └──────────┘ └──────────┘ └──────────┘
```

1. **Démarrage.** Un membre lance le serveur depuis l'application, ou un joueur tente de se
   connecter si le démarrage automatique est activé.
2. **Élection de l'hôte.** Le serveur central choisit, parmi les PC disponibles, celui qui
   hébergera : le plus prioritaire, ou le plus performant.
3. **Bail exclusif.** Ce PC reçoit un bail : tant qu'il le détient, il est le seul autorisé à
   modifier le monde. Deux PC ne peuvent jamais écrire le monde en même temps.
4. **Récupération du monde.** Le PC télécharge la dernière version validée du monde, vérifie
   chaque fichier, puis démarre Minecraft.
5. **Connexion des joueurs.** Les joueurs se connectent à l'adresse du serveur central. Le relais
   les achemine par un tunnel que le PC hôte a ouvert vers le VPS : aucune redirection de port
   n'est nécessaire, y compris derrière une box en CGNAT.
6. **Sauvegardes continues.** Pendant la partie, le PC envoie régulièrement les fichiers modifiés
   au serveur central.
7. **Changement d'hôte.** Lorsque l'hôte doit s'arrêter, le serveur est transféré sur un autre PC,
   avec le même monde et la même adresse.

---

## Prérequis

| Rôle | Configuration requise |
|------|-----------------------|
| Serveur central | VPS sous **Ubuntu 24.04**, 2 Go de RAM, adresse IP publique, Nginx installé. Ports TCP **8443** (application) et **25565** (Minecraft) ouverts. |
| PC hôtes | **Windows 10 ou 11**, ou **Mac à puce Apple** sous macOS 12 ou ultérieur. Mémoire suffisante pour Minecraft (6 Go par défaut, réglable). Java est installé automatiquement. |
| Joueurs | **Minecraft Java Edition**. L'application n'est pas nécessaire pour jouer, seulement pour héberger. |
| Fabrication | PC Windows avec **Python 3.12** et [Inno Setup 6](https://jrsoftware.org/isinfo.php). La version macOS se fabrique sur GitHub Actions ou sur un Mac. |

Aucun nom de domaine n'est requis : les adresses utilisent [nip.io](https://nip.io), qui associe
un nom à l'adresse IP du VPS.

---

## Installation du serveur central

L'installation est conçue pour cohabiter avec les services existants du VPS : elle ajoute un
service systemd, un site Nginx sur le port 8443 et deux règles de pare-feu, sans modifier le
reste. Chaque étape est lancée manuellement et dispose de sa commande d'annulation.

### 1. Fabriquer l'archive (sur un PC Windows)

```powershell
powershell -ExecutionPolicy Bypass -File deployment\vps\package.ps1
```

L'archive `dist\mdh-server-X.Y.Z.tar.gz` contient le code et toutes ses dépendances :
l'installation sur le VPS ne télécharge rien. Le script affiche l'empreinte SHA-256 de
l'archive, à comparer après transfert.

### 2. Transférer l'archive

```powershell
scp dist\mdh-server-X.Y.Z.tar.gz utilisateur@203.0.113.10:
```

### 3. Installer (sur le VPS)

```bash
tar -xzf mdh-server-X.Y.Z.tar.gz && cd mdh-server-X.Y.Z
sudo bash install.sh check
```

La commande `check` ne modifie rien : elle vérifie le système, Python, les ports et Nginx, et
s'arrête au moindre problème. Les étapes suivent dans cet ordre :

| Étape | Commande | Effet | Annulation |
|-------|----------|-------|------------|
| Service | `sudo bash install.sh app` | Installe le code et démarre le service, à l'écoute sur `127.0.0.1` uniquement | `sudo bash rollback.sh app` |
| HTTPS | `sudo bash install.sh nginx` | Génère le certificat et le site Nginx sur le port 8443 | `sudo bash rollback.sh nginx` |
| Pare-feu | `sudo bash install.sh firewall` | Ouvre les ports 8443 et 25565 dans UFW | `sudo bash rollback.sh firewall` |
| Premier compte | `sudo bash install.sh create-user <pseudo>` | Crée le compte de l'administrateur ; les autres membres s'inscrivent avec un code d'invitation | — |

Si l'hébergeur dispose de son propre pare-feu, les ports **TCP 8443** et **TCP 25565** doivent
également y être ouverts.

Pour utiliser un autre port Minecraft que 25565, indiquez-le dès la première installation :
`sudo MDH_RELAY_PORT=25570 bash install.sh app`, puis `sudo bash install.sh firewall`. Le port est
ensuite conservé dans `/etc/mdh/mdh.env`, et les joueurs ajoutent `:25570` à l'adresse du serveur.

### 4. Obtenir l'adresse du serveur central

```bash
sudo bash install.sh address
```

```
https://203.0.113.10:8443#q3mF0kV2aB9...
```

Cette adresse est à transmettre **en entier** aux membres du groupe. La partie située après `#`
est l'empreinte du certificat : l'application refuse toute connexion à un autre serveur, ce qui
protège les identifiants même sur un réseau public.

### Administration

```bash
sudo bash install.sh status                    # état du service, disque, mémoire, erreurs récentes
sudo bash install.sh reset-password <pseudo>   # réinitialisation d'un mot de passe
sudo bash rollback.sh release                  # retour à la version précédente du serveur central
```

Pour mettre à jour le serveur central, transférez la nouvelle archive puis lancez
`sudo bash install.sh app` depuis son dossier. La base de données est sauvegardée avant chaque
mise à jour. Le serveur central doit toujours être mis à jour **avant** les applications.

---

## Installation de l'application

### Windows

Exécutez `MinecraftDistributedHosting-Setup-X.Y.Z.exe`. L'installation se fait pour l'utilisateur
courant et ne demande pas de droits administrateur.

L'installateur n'étant pas signé par un certificat Microsoft, Windows SmartScreen peut afficher
un avertissement : choisissez **Informations complémentaires**, puis **Exécuter quand même**.

### macOS (puces Apple)

1. Ouvrez le fichier `.dmg` et faites glisser l'application dans **Applications**.
2. Au premier lancement, macOS bloque l'application, qui n'est pas signée par Apple. Ouvrez
   **Réglages Système → Confidentialité et sécurité** et cliquez sur **Ouvrir quand même**.
3. L'application s'affiche ensuite dans la barre des menus.

### Mises à jour

L'application se met à jour automatiquement. Une nouvelle version publiée sur le serveur central
est détectée en moins d'une minute, puis installée lorsque la fenêtre est fermée et que le PC
n'héberge pas le serveur. Le bouton **Paramètres → Vérifier les mises à jour** permet de lancer
une vérification immédiate.

---

## Prise en main

### Administrateur du groupe

1. Dans l'application, saisissez l'adresse du serveur central et connectez-vous avec le compte
   créé sur le VPS.
2. Créez un **groupe**, puis un **serveur**. Des modèles sont proposés :

   | Modèle | Contenu |
   |--------|---------|
   | Vanilla simple | Minecraft officiel, sans ajout |
   | Survie optimisée | Fabric avec des mods d'optimisation, sans installation côté joueurs |
   | Serveur à plugins | Paper avec LuckPerms |
   | Moddé populaire | Un modpack Modrinth (Create, Cobblemon…) |

3. Choisissez la version de Minecraft, la mémoire allouée et, si besoin, la graine du monde.
4. Dans l'onglet **Groupe**, générez un **code d'invitation** pour chaque membre.
5. Dans **Paramètres**, acceptez le **contrat de licence de Minecraft (EULA)**. Cette acceptation
   est requise sur chaque PC qui héberge.

### Membres

1. Installez l'application.
2. Saisissez l'adresse du serveur central, cochez **Créer un nouveau compte** et indiquez le code
   d'invitation reçu.
3. Pour pouvoir héberger, acceptez l'EULA dans **Paramètres**.

### Choix de l'hôte

Chaque PC dispose d'une **priorité**, réglable dans l'onglet **Groupe** : le serveur est confié
au PC disponible le plus prioritaire. La configuration du serveur permet aussi de privilégier
**le PC le plus performant**. Chaque membre peut retirer son PC de l'hébergement dans ses
paramètres.

---

## Connexion des joueurs

Dans Minecraft, ouvrez **Multijoueur → Ajouter un serveur** et saisissez l'adresse indiquée sur
la page **Serveur** de l'application, par exemple :

```
serveur.203.0.113.10.nip.io
```

Cette adresse reste la même quel que soit le PC qui héberge.

Lorsque le démarrage automatique est activé, une tentative de connexion suffit à lancer le
serveur : Minecraft affiche « Le serveur démarre pour toi », et la connexion est possible environ
une minute plus tard.

---

## Publication d'une nouvelle version

Chaque version est publiée simultanément pour Windows et macOS.

### 1. Clé de signature (une seule fois)

```powershell
.venv\Scripts\python deployment\windows\sign_release.py keygen
```

La clé privée est créée dans `%USERPROFILE%\.mdh-release\`. Elle ne doit jamais être placée dans
le dépôt, sur le VPS ou sur GitHub, et doit être sauvegardée hors du PC : sans elle, les
applications installées n'acceptent plus de mise à jour. La clé publique affichée est à
renseigner dans `TRUSTED_KEYS` (`client/mdh_client/system/updates.py`).

Toute personne qui déploie le projet pour son propre groupe doit générer sa propre clé et
remplacer celle présente dans `TRUSTED_KEYS`.

### 2. Version macOS

La version macOS est fabriquée par GitHub Actions, sans Mac :

```powershell
gh workflow run build-macos.yml
gh run watch
gh run download --name mac-X.Y.Z --dir dist\macos
```

Sur un Mac, la commande équivalente est `bash deployment/macos/build.sh`.

### 3. Version Windows et signature

```powershell
powershell -ExecutionPolicy Bypass -File deployment\windows\build.ps1 -Notes "Résumé des changements"
```

Le script fabrique l'installateur Windows, puis signe celui-ci et la version macOS présente dans
`dist\macos`.

### 4. Publication sur le serveur central

```powershell
scp dist\installer\MinecraftDistributedHosting-Setup-X.Y.Z.exe* dist\macos\MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip* utilisateur@203.0.113.10:
```

```bash
sudo bash install.sh publish-client ~/MinecraftDistributedHosting-Setup-X.Y.Z.exe ~/MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip
```

Les applications connectées sont averties immédiatement. Une publication peut être retirée avec
`sudo bash rollback.sh client` (Windows) ou `sudo bash rollback.sh client mac` (macOS).

---

## Sécurité

### Principes

- **Un seul écrivain à la fois.** Un bail associé à un numéro d'époque, contrôlé par le serveur
  central à chaque sauvegarde, empêche deux PC de modifier le monde simultanément.
- **Intégrité vérifiée.** Une sauvegarde n'est validée qu'après contrôle SHA-256 de chaque
  fichier.
- **Aucune perte en cas d'échec.** Un changement d'hôte interrompu ne supprime jamais la dernière
  sauvegarde valide.
- **Aucun exécutable synchronisé.** Les mods proviennent exclusivement de Modrinth, sont vérifiés
  par empreinte et doivent être approuvés sur chaque PC.
- **Mises à jour signées hors ligne** (Ed25519). Une compromission du VPS ne permet pas de
  distribuer une application modifiée.
- **API non exposée.** Elle n'écoute que sur `127.0.0.1` ; Nginx assure le HTTPS.
- **Aucune commande arbitraire.** L'API n'accepte qu'une liste fixe d'actions.
- **Mots de passe hachés** avec Argon2id.
- **Aucun secret dans le dépôt.** La configuration du VPS est fournie sous forme de modèle
  (`.env.example`).

### Points d'attention

- Pendant son tour, le PC hôte a accès au trafic des joueurs et au monde. Les versions
  précédentes du monde restent restaurables depuis le serveur central.
- Un mod s'exécute avec les droits de Minecraft sur le PC hôte, d'où l'approbation demandée à
  chaque membre.
- Les applications ne sont signées ni par Microsoft ni par Apple.

---

## Développement

### Environnement

Python 3.12 est requis.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e shared -e "server[test]" -e "client[test]"
.\.venv\Scripts\python.exe -m pytest
```

Les tests démarrent un véritable serveur central et de véritables agents, avec un serveur
Minecraft simulé. La suite complète dure une dizaine de minutes.

Avant de publier une version, la vérification complète (tests, puis recherche de vulnérabilités
connues dans les dépendances avec `pip-audit`) se lance en une commande :

```powershell
powershell -ExecutionPolicy Bypass -File deployment\scripts\check.ps1
```

Le workflow GitHub « Tests » fait la même chose, mais seulement à la demande.

### Essai en local

```powershell
powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-local.ps1
```

Ce script lance un serveur central local et l'application. Après la création d'un compte, d'un
groupe et d'un serveur, et l'acceptation de l'EULA, le serveur est joignable dans Minecraft à
l'adresse `localhost:25566`.

Pour simuler un second PC sur la même machine :

```powershell
powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-second-pc.ps1
```

Les données locales sont stockées dans `data\` ; supprimer ce dossier réinitialise
l'environnement.

### Structure du dépôt

```
shared/        Code commun : modèles, protocole entre le VPS et les PC, manifestes du monde
server/        Serveur central : API, WebSocket, comptes, élection de l'hôte, baux, relais
client/        Application : hébergement, synchronisation, gestion de Minecraft, interface
deployment/
  vps/         Installation sur le VPS (install.sh, rollback.sh, service, Nginx)
  windows/     Installateur Windows et signature des mises à jour
  macos/       Fabrication de la version macOS
  scripts/     Essais en local
tools/         Test de charge
.github/       Fabrication de la version macOS (GitHub Actions)
```

### Technologies

| Composant | Technologies |
|-----------|--------------|
| Serveur central | Python 3.12, FastAPI, SQLite, WebSocket, relais Minecraft (asyncio) |
| Application | Python 3.12, PySide6 (Qt), PyInstaller, Inno Setup |
| Serveurs Minecraft | Vanilla, Paper, Fabric ; Java Temurin téléchargé et vérifié automatiquement |

---

## Limites connues

- **Minecraft Java Edition uniquement** : Bedrock n'est pas pris en charge.
- **Un seul hôte à la fois** : les performances dépendent de la connexion de ce PC, notamment de
  son débit montant.
- **Mods des joueurs non détectables** : aucun serveur Minecraft ne peut connaître les mods
  installés côté client. L'anti-triche agit sur les effets de la triche.
- **Bannissements par IP inopérants** derrière le relais : le bannissement se fait par pseudo.
- **macOS sur processeur Intel non pris en charge.**

---

## Licence

Licence non définie à ce jour.
