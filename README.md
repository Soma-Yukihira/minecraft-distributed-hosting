# Minecraft Distributed Hosting

Un serveur Minecraft pour un groupe d'amis, qui tourne **sur nos PC à tour de rôle**, avec
**une seule adresse** à taper dans Minecraft.

J'ai fait ce projet parce que louer un serveur Minecraft assez puissant coûte cher, alors qu'on
a tous un PC qui peut le faire tourner. Le problème, c'est que le serveur dépend du PC de celui
qui l'héberge : s'il éteint, plus personne ne joue. Ici, le serveur passe tout seul sur le PC
d'un autre, avec le même monde et la même adresse.

---

## Sommaire

1. [Comment ça marche](#comment-ça-marche)
2. [Ce qu'il faut](#ce-quil-faut)
3. [Installer le serveur central (VPS)](#installer-le-serveur-central-vps)
4. [Installer l'application (Windows et Mac)](#installer-lapplication-windows-et-mac)
5. [Première utilisation](#première-utilisation)
6. [Jouer](#jouer)
7. [Ce que l'application sait faire](#ce-que-lapplication-sait-faire)
8. [Publier une nouvelle version](#publier-une-nouvelle-version)
9. [Sécurité](#sécurité)
10. [Développement](#développement)
11. [Limites connues](#limites-connues)

---

## Comment ça marche

Il y a deux morceaux :

- **Un petit VPS** (le « serveur central »). Il ne fait jamais tourner Minecraft. Il gère les
  comptes, décide quel PC héberge, garde les sauvegardes du monde, et fait passer les joueurs
  vers le bon PC. 2 Go de RAM suffisent.
- **L'application**, installée sur le PC de chaque membre du groupe. Quand c'est son tour, elle
  télécharge le monde, lance le serveur Minecraft, et envoie des sauvegardes pendant la partie.

```
     Joueurs (Minecraft Java)
              │
              │  une seule adresse : monserveur.203.0.113.10.nip.io
              ▼
   ┌───────────────────────────┐
   │        VPS (central)      │   comptes, choix du PC hôte,
   │  relais Minecraft + API   │   sauvegardes du monde
   └───────────────────────────┘
        ▲            ▲            ▲
        │ tunnel     │            │      chaque PC se connecte au VPS
        │ sortant    │            │      (rien à ouvrir sur la box)
   ┌─────────┐  ┌─────────┐  ┌─────────┐
   │  PC de  │  │  PC de  │  │  PC de  │
   │  Alex   │  │  Léa    │  │  Tom    │
   │(héberge)│  │(attend) │  │(éteint) │
   └─────────┘  └─────────┘  └─────────┘
```

Dans l'ordre :

1. Quelqu'un clique sur **Démarrer** (ou un joueur essaie de se connecter, si le réveil
   automatique est activé).
2. Le VPS choisit le PC qui va héberger : selon la priorité de chacun, ou le plus puissant des
   PC allumés.
3. Ce PC reçoit un **bail** : tant qu'il l'a, il est le seul à pouvoir écrire le monde. Deux PC
   ne peuvent jamais modifier le monde en même temps.
4. Il récupère la dernière version du monde depuis le VPS, vérifie chaque fichier, et lance
   Minecraft.
5. Les joueurs se connectent à l'adresse du VPS. Le relais les fait passer par un tunnel que le
   PC hôte a ouvert vers le VPS. Pas de port à ouvrir chez soi, et ça marche même derrière une
   box en CGNAT.
6. Pendant la partie, le PC envoie des sauvegardes régulières au VPS (seulement ce qui a changé).
7. Si l'hôte doit partir, le serveur passe sur un autre PC. Avec Minecraft 1.20.5 ou plus, les
   joueurs sont transférés sans être déconnectés.

---

## Ce qu'il faut

| Pour quoi | Ce qu'il faut |
|-----------|---------------|
| Le serveur central | Un VPS **Ubuntu 24.04** avec 2 Go de RAM, une IP publique, et Nginx installé. Les ports TCP **8443** (application) et **25565** (Minecraft) doivent pouvoir être ouverts. |
| Les PC qui hébergent | **Windows 10 ou 11**, ou un **Mac à puce Apple** (M1, M2…) avec macOS 12 ou plus. Assez de RAM pour Minecraft : un serveur prend 6 Go par défaut (réglable), plus avec beaucoup de mods. Java s'installe tout seul. |
| Les joueurs | **Minecraft Java Edition**. Un joueur n'a pas besoin de l'application pour jouer, seulement pour héberger. |
| Fabriquer les fichiers | Un PC Windows avec **Python 3.12** (voir [Développement](#développement)). Pour Inno Setup et la version Mac, voir plus bas. |

Pas besoin d'acheter un nom de domaine : l'adresse utilise [nip.io](https://nip.io), qui
transforme l'IP du VPS en nom (`203.0.113.10.nip.io`).

---

## Installer le serveur central (VPS)

L'installation ne touche pas à ce qui tourne déjà sur le VPS : elle ajoute un service, un site
Nginx sur le port 8443 et deux règles de pare-feu. Chaque étape se lance à la main et a sa
commande de retour arrière.

### 1. Fabriquer l'archive (sur le PC Windows)

```powershell
powershell -ExecutionPolicy Bypass -File deployment\vps\package.ps1
```

Ça crée `dist\mdh-server-X.Y.Z.tar.gz` avec le code et toutes ses dépendances : le VPS installe
sans rien télécharger. Le script affiche aussi l'empreinte SHA-256 de l'archive.

### 2. Copier l'archive sur le VPS

```powershell
scp dist\mdh-server-X.Y.Z.tar.gz utilisateur@203.0.113.10:
```

### 3. Installer, étape par étape (sur le VPS)

```bash
tar -xzf mdh-server-X.Y.Z.tar.gz && cd mdh-server-X.Y.Z
sudo bash install.sh check
```

`check` ne modifie rien : il vérifie que tout est prêt (Python, ports libres, Nginx) et s'arrête
au moindre problème. Ensuite :

| Étape | Commande | Ce que ça fait | Pour annuler |
|-------|----------|----------------|--------------|
| Service | `sudo bash install.sh app` | Installe le code et démarre le service (API sur `127.0.0.1` seulement) | `sudo bash rollback.sh app` |
| HTTPS | `sudo bash install.sh nginx` | Crée un certificat et le site Nginx sur le port 8443 | `sudo bash rollback.sh nginx` |
| Pare-feu | `sudo bash install.sh firewall` | Ouvre 8443 et 25565 dans UFW | `sudo bash rollback.sh firewall` |
| Premier compte | `sudo bash install.sh create-user tonpseudo` | Crée ton compte. Les autres s'inscrivent avec un code d'invitation. | — |

Ouvre aussi **TCP 8443** et **TCP 25565** dans le pare-feu du panneau de ton hébergeur, s'il en
a un (chez IONOS : *Réseau → Stratégies de pare-feu*).

### 4. Récupérer l'adresse du groupe

```bash
sudo bash install.sh address
```

Elle ressemble à ça :

```
https://203.0.113.10:8443#q3mF0kV2aB9...
```

C'est cette adresse, **en entier**, que les membres collent dans l'application. La partie
après `#` est l'empreinte du certificat du VPS : l'application refuse de parler à un autre
serveur, même si quelqu'un essaie de s'interposer sur un Wi-Fi public.

### Commandes utiles ensuite

```bash
sudo bash install.sh status                  # état du service, disque, mémoire, dernières erreurs
sudo bash install.sh reset-password pseudo   # mot de passe oublié
sudo bash rollback.sh release                # revenir à la version précédente du serveur central
```

Pour mettre à jour le serveur central : fabrique la nouvelle archive, copie-la, puis
`sudo bash install.sh app` depuis son dossier. **Toujours le VPS d'abord, l'application
ensuite.**

---

## Installer l'application (Windows et Mac)

### Windows

Lance `MinecraftDistributedHosting-Setup-X.Y.Z.exe`. Il s'installe pour ton compte Windows, sans
droits administrateur.

L'installateur n'est pas signé par Microsoft : Windows SmartScreen peut afficher « Windows a
protégé votre ordinateur ». Clique sur **Informations complémentaires**, puis **Exécuter quand
même**.

### Mac (puce Apple)

1. Ouvre le fichier `.dmg` et glisse l'application dans **Applications**.
2. Lance-la. La première fois, macOS la bloque parce qu'elle n'est pas signée par Apple :
   va dans **Réglages Système → Confidentialité et sécurité**, descends tout en bas et clique sur
   **Ouvrir quand même**.
3. L'icône apparaît dans la barre des menus, en haut de l'écran.

### Ensuite

L'application **se met à jour toute seule**. Quand une nouvelle version est publiée sur le VPS,
elle la reçoit en moins d'une minute et l'installe quand la fenêtre est fermée et que le PC
n'héberge pas le serveur. Le bouton **Paramètres → Vérifier les mises à jour** vérifie tout de
suite.

---

## Première utilisation

### Pour celui qui crée le groupe (l'admin)

1. Ouvre l'application, colle l'adresse du groupe dans **Serveur central**, et connecte-toi avec
   le compte créé sur le VPS.
2. Crée un **groupe**, puis un **serveur**. Tu peux partir d'un modèle :
   - **Vanilla simple** : le Minecraft officiel, sans rien ajouter ;
   - **Survie optimisée** : Fabric avec des mods qui allègent le serveur, rien à installer pour
     les joueurs ;
   - **Serveur à plugins** : Paper avec LuckPerms ;
   - **Moddé populaire** : un modpack de Modrinth (Create, Cobblemon…).
3. Choisis la version de Minecraft, la mémoire, la graine du monde si tu en veux une.
4. Va dans **Groupe** et crée un **code d'invitation** pour chaque ami.
5. Dans **Paramètres**, lis et accepte l'**EULA de Minecraft** (c'est obligatoire pour héberger,
   et chacun le fait sur son propre PC).

### Pour les amis

1. Installe l'application.
2. Colle l'adresse du groupe, coche **Créer un nouveau compte**, et entre le code d'invitation.
3. Accepte l'EULA dans **Paramètres** si tu veux pouvoir héberger.

### Qui héberge ?

Dans **Groupe**, chaque PC a une **priorité** : le serveur va d'abord sur le PC allumé qui a la
plus haute. Tu peux aussi choisir **le PC le plus performant** dans la configuration du serveur.
Chacun peut décocher **Autoriser ce PC à héberger** dans ses paramètres.

---

## Jouer

Dans Minecraft : **Multijoueur → Ajouter un serveur**, et mets l'adresse affichée sur la page
**Serveur** de l'application, par exemple :

```
monserveur.203.0.113.10.nip.io
```

C'est la même adresse, quel que soit le PC qui héberge. Un joueur n'a pas besoin de
l'application pour jouer.

Si le **réveil automatique** est activé, il suffit d'essayer de se connecter : Minecraft
affiche « Le serveur démarre pour toi », le serveur se lance sur un PC disponible, et on se
reconnecte une minute après. Il peut aussi s'éteindre tout seul quand plus personne ne joue.

---

## Ce que l'application sait faire

**Hébergement**
- Le serveur passe d'un PC à l'autre : sur demande, quand l'hôte s'en va, ou quand un PC plus
  prioritaire revient (après un compte à rebours, ou seulement quand le serveur est vide).
- Changement de PC sans déconnecter les joueurs (Minecraft 1.20.5 ou plus).
- Secours à chaud : les autres PC gardent une copie du monde à jour, pour reprendre vite si
  l'hôte plante.
- Réveil au premier joueur et arrêt quand c'est vide.
- Le PC ne se met pas en veille pendant qu'il héberge.

**Le monde**
- Sauvegardes pendant la partie, historique des versions, restauration en un clic.
- Import d'un monde existant, export en `.zip`.
- Nouveau monde (graine au hasard ou choisie), l'ancien est gardé dans l'historique.
- Plusieurs serveurs par groupe.
- Pré-génération du monde avec Chunky, pour moins de lag en exploration.

**Mods et plugins**
- Mods (Fabric) et plugins (Paper) depuis Modrinth, vérifiés par leur empreinte, et autorisés par
  chaque membre sur son propre PC avant d'être lancés.
- Modpacks Modrinth, avec un fichier tout prêt pour que les joueurs installent les mêmes mods.
- Mises à jour des mods en un clic, datapacks, pack de ressources.
- Carte du monde dans le navigateur (BlueMap).

**Protection**
- Anti-triche : Grim (vol, vitesse, coups de trop loin…) et anti-X-ray.
- Liste blanche, demandes d'accès, modérateurs, expulsion et bannissement depuis l'application.
- Protection des pseudos, même avec des comptes non officiels.

**Suivi**
- Console et journal du serveur en direct, commandes Minecraft.
- Statistiques, graphiques de performance, analyse du lag (spark).
- Plantages expliqués, avec le mod en cause quand il y en a un.
- Page de statut publique, alertes du VPS (disque presque plein…).

---

## Publier une nouvelle version

Chaque version se publie **pour Windows et Mac en même temps**, sinon les Mac restent en
arrière.

### 1. Créer la clé de signature (une seule fois)

```powershell
.venv\Scripts\python deployment\windows\sign_release.py keygen
```

La clé est créée dans `%USERPROFILE%\.mdh-release\` et ne va **jamais** dans Git, sur le VPS ou
sur GitHub. Garde-en une copie sur une clé USB : sans elle, les applications n'acceptent plus de
mise à jour automatique. La commande affiche la clé publique à mettre dans `TRUSTED_KEYS`
(`client/mdh_client/system/updates.py`).

Si tu reprends ce projet pour ton propre groupe, crée **ta** clé et remplace celle qui est dans
`TRUSTED_KEYS`. Sinon, les mises à jour que tu signes seront refusées.

### 2. Fabriquer la version Mac (sur GitHub)

Pas besoin d'avoir un Mac : GitHub en prête un.

```powershell
gh workflow run build-macos.yml
gh run watch
gh run download --name mac-X.Y.Z --dir dist\macos
```

On peut aussi la fabriquer sur un vrai Mac avec `bash deployment/macos/build.sh`.

### 3. Fabriquer la version Windows et signer les deux

```powershell
powershell -ExecutionPolicy Bypass -File deployment\windows\build.ps1 -Notes "Ce qui change"
```

Il faut [Inno Setup 6](https://jrsoftware.org/isinfo.php) installé. Le script fabrique
l'installateur, trouve la version Mac dans `dist\macos`, et signe les deux.

### 4. Publier sur le VPS

```powershell
scp dist\installer\MinecraftDistributedHosting-Setup-X.Y.Z.exe* dist\macos\MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip* utilisateur@203.0.113.10:
```

```bash
sudo bash install.sh publish-client ~/MinecraftDistributedHosting-Setup-X.Y.Z.exe ~/MinecraftDistributedHosting-X.Y.Z-macos-arm64.zip
```

Les applications connectées sont prévenues tout de suite. Pour retirer une version ratée :
`sudo bash rollback.sh client` (Windows) ou `sudo bash rollback.sh client mac`.

---

## Sécurité

Les règles que je me suis fixées :

1. **Jamais deux PC n'écrivent le même monde.** Bail avec numéro d'époque, vérifié par le VPS à
   chaque sauvegarde.
2. **Une sauvegarde n'est valide qu'après vérification** de chaque fichier (SHA-256).
3. **Un changement de PC raté ne supprime jamais la dernière bonne sauvegarde.**
4. **Aucun programme n'est lancé depuis la synchronisation.** Les mods viennent uniquement de
   Modrinth, vérifiés, et chaque membre les autorise sur son PC.
5. **Les mises à jour de l'application sont signées hors ligne.** Même quelqu'un qui prendrait le
   contrôle du VPS ne pourrait pas faire installer un faux programme.
6. **L'API n'est jamais exposée directement** : elle écoute sur `127.0.0.1`, Nginx fait le HTTPS.
7. **Aucune commande arbitraire depuis l'API.** Les actions possibles sont une liste fixe.
8. **Les mots de passe sont hachés** (Argon2), jamais stockés en clair.
9. **Aucun secret dans le dépôt.** La configuration du VPS a un modèle : `.env.example`.

Ce qu'il faut savoir :

- Le PC qui héberge voit le trafic des joueurs et peut modifier le monde pendant son tour. C'est
  le principe même de l'hébergement, et les versions précédentes restent restaurables.
- Un mod de Modrinth tourne avec les droits de Minecraft sur le PC hôte. C'est pour ça que
  chacun autorise lui-même les mods sur son PC.
- Les applications ne sont signées ni par Microsoft ni par Apple (pas de certificat payant).

---

## Développement

### Installer l'environnement

Il faut Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e shared -e "server[test]" -e "client[test]"
.\.venv\Scripts\python.exe -m pytest
```

Les tests lancent un vrai serveur central et de vrais agents, avec un faux Minecraft : comptez
une dizaine de minutes pour tout.

### Essayer sur un seul PC

```powershell
powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-local.ps1
```

Ça lance un serveur central local et l'application. Crée un compte, un groupe, un serveur,
accepte l'EULA dans Paramètres, puis **Démarrer**. Dans Minecraft : `localhost:25566`.

Pour simuler un deuxième PC sur la même machine, pendant que le premier tourne :

```powershell
powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-second-pc.ps1
```

Tout est rangé dans `data\` : supprime ce dossier pour repartir de zéro.

### Organisation du code

```
shared/       code commun : modèles, protocole entre le VPS et les PC, manifestes du monde
server/       serveur central : API, WebSocket, comptes, choix de l'hôte, baux, relais Minecraft
client/       application : hébergement, synchronisation du monde, Minecraft, interface (PySide6)
deployment/
  vps/        installation sur le VPS (install.sh, rollback.sh, service, Nginx)
  windows/    installateur Windows et signature des mises à jour
  macos/      fabrication de la version Mac
  scripts/    essais en local
tools/        test de charge
.github/      fabrication de la version Mac sur GitHub Actions
```

### Technologies

- **Python 3.12** partout.
- Serveur central : **FastAPI**, **SQLite**, WebSocket, relais Minecraft fait maison (asyncio).
- Application : **PySide6** (Qt), **PyInstaller**, **Inno Setup** pour l'installateur Windows.
- Serveurs Minecraft : Vanilla, **Paper** ou **Fabric**, Java **Temurin** téléchargé et vérifié
  automatiquement.

---

## Limites connues

- **Minecraft Java seulement.** Pas de Bedrock (téléphone, console) pour l'instant.
- **Un seul PC héberge à la fois.** Le serveur dépend de la connexion Internet de ce PC, surtout
  de son débit montant.
- **Le serveur central ne sait pas quels mods les joueurs ont installés** (aucun serveur
  Minecraft ne le peut). L'anti-triche bloque les effets de la triche, pas les mods eux-mêmes.
- **Les bans par IP de Minecraft ne marchent pas** derrière le relais : bannis par pseudo.
- **Mac Intel non pris en charge**, seulement les puces Apple.

---

## Licence

Pas encore choisie.
