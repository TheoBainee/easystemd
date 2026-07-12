## 1. Contexte

Sous Linux (Debian 13, mais viser une portabilité raisonnable à toute distro avec systemd
récent), j'ai des binaires installés en standalone qui exposent typiquement deux commandes :

- une commande qui lance un serveur/process long-running (ex: `mon_binaire web`, `mon_binaire serve`, `mon_binaire prod`, etc.)
- une commande qui effectue une mise à jour du binaire lui-même (ex: `mon_binaire upgrade`, `mon_binaire update`, etc.)

Je veux que ce serveur tourne en continu (auto-restart en cas de crash), et que la mise à
jour soit déclenchée automatiquement selon un planning (ex: chaque semaine), avec un arrêt
propre du serveur avant l'upgrade et un redémarrage garanti après — que l'upgrade ait réussi
ou échoué.

Je gère cela aujourd'hui manuellement avec 1 service systemd `--user` (le serveur,
`Restart=always`), 1 service `oneshot` (stop → upgrade → start, via un script wrapper), et
1 timer (`OnCalendar`). Je veux maintenant généraliser cette mécanique à **n'importe quel
binaire**, sans tout réécrire à la main à chaque fois.

## 2. Objectif

Développer un outil CLI Python, nommé **`easystemd`**, qui automatise entièrement la mise en
place de ce pattern "serve + upgrade planifié" pour un ou plusieurs binaires, en générant et
gérant lui-même les units `systemd --user` correspondantes.

L'outil doit être :
- **générique** : aucun couplage avec un binaire précis, tout est paramétrable (binaire,
  sous-commande de service, sous-commande d'upgrade, planning, etc.)
- **multi-instances** : capable de gérer plusieurs binaires différents en parallèle, chacun
  avec sa propre config, ses propres units, ses propres logs
- **idempotent et safe** : ré-exécuter une commande ne doit jamais laisser le système dans un
  état incohérent (units orphelines, service jamais redémarré après un upgrade raté, etc.)

## 3. Contraintes techniques non négociables

- Python 3.11+, aucune dépendance système exotique.
- **Uniquement `systemctl --user`** (jamais de units système / root), sauf pour l'activation
  du *linger* (`loginctl enable-linger`) qui nécessite `sudo` — dans ce cas, l'outil ne doit
  **jamais exécuter `sudo` silencieusement** : il propose la commande exacte à lancer, ou
  demande une confirmation explicite avant de l'exécuter lui-même.
- **PATH restreint des services systemd user** : les services `--user` n'héritent pas
  forcément du `PATH` d'un shell interactif (notamment si `easystemd` est installé via
  `pipx`/`pip --user`, son exécutable est dans `~/.local/bin` qui peut ne pas être dans le
  `PATH` résolu par systemd). **Toutes les `ExecStart=` générées doivent utiliser des chemins
  absolus** (résolus au moment du `add`, via `shutil.which()` puis `Path.resolve()`), aussi
  bien pour le binaire géré que pour l'exécutable `easystemd` lui-même. Ne jamais compter sur
  le `PATH` dans les fichiers unit générés.
- Toute exécution de sous-processus doit être faite avec `subprocess.run(..., capture_output=True, text=True, check=False)` et vérification explicite du `returncode` — pas de `check=True` qui masque le contexte de l'erreur.
- Écriture de fichiers (config, units) toujours atomique : écrire dans un fichier temporaire
  puis `os.replace()`.
- Respecter les XDG Base Directories : config dans `$XDG_CONFIG_HOME` (défaut
  `~/.config/easystemd/`), état/logs dans `$XDG_STATE_HOME` (défaut `~/.local/state/easystemd/`), units dans `~/.config/systemd/user/`.

## 4. Architecture du projet attendue

```
easystemd/
├── pyproject.toml
├── README.md
├── src/easystemd/
│   ├── __init__.py
│   ├── cli.py              # interface CLI (typer ou click, au choix)
│   ├── models.py           # schémas pydantic (AppConfig, etc.)
│   ├── config.py           # lecture/écriture du fichier de config YAML
│   ├── systemd.py          # génération des units + wrappers autour de systemctl
│   ├── templates.py        # rendu Jinja2 des fichiers .service/.timer
│   ├── upgrade_runner.py   # logique exécutée par le service "upgrade" (le cœur du système)
│   ├── doctor.py           # diagnostics d'environnement
│   ├── state.py            # lecture/écriture du last-run.json par app
│   └── templates/
│       ├── serve.service.j2
│       ├── upgrade.service.j2
│       └── upgrade.timer.j2
└── tests/
    ├── test_config.py
    ├── test_systemd.py
    ├── test_cli.py
    ├── test_upgrade_runner.py
    └── test_doctor.py
```

Stack recommandée : `typer` (ou `click`) pour le CLI, `pydantic` v2 pour la validation de
config, `PyYAML` pour la sérialisation, `Jinja2` pour les templates. `rich` en option pour
un affichage tabulaire soigné de `list`/`status` (non bloquant si absent).

## 5. Modèle de configuration

Un fichier unique `~/.config/easystemd/config.yaml` contient la liste de toutes les apps
gérées. Format retenu : clé racine `apps:` contenant une **liste** d'objets (extensible pour
futures clés globales : version, settings, etc.). Chaque app est un objet validé par un modèle
pydantic avec les champs suivants :

```yaml
apps:
  - name: mon-binaire
    binary: /usr/local/bin/mon_binaire
    serve_args: web
    upgrade_args: upgrade
    schedule: "Sun 04:00:00"
    # ... autres champs optionnels
```


| Champ | Type | Obligatoire | Défaut | Description |
|---|---|---|---|---|
| `name` | str (slug `[a-z0-9-]+`) | oui | — | identifiant unique, sert de base aux noms de units |
| `binary` | chemin absolu | oui | — | résolu via `shutil.which` si une commande simple est fournie à `add` |
| `serve_args` | str | oui | — | ex: `"web"`, `"prod --port 8080"` |
| `upgrade_args` | str | oui | — | ex: `"upgrade"` |
| `exec_type` | enum `simple\|forking\|exec\|notify` | non | `simple` | correspond à `Type=` dans le service serve |
| `schedule` | str (format `OnCalendar` systemd) | non | `"Sun 04:00:00"` | validé via `systemd-analyze calendar` avant écriture |
| `working_dir` | chemin | non | `$HOME` | |
| `env_file` | chemin | non | `None` | `EnvironmentFile=` |
| `restart_sec` | int | non | `5` | délai avant redémarrage auto du serve en cas de crash |
| `stop_timeout` | int | non | `30` | `TimeoutStopSec=` du service serve |
| `randomized_delay` | int | non | `300` | `RandomizedDelaySec=` du timer |
| `persistent` | bool | non | `true` | `Persistent=` du timer (rattrapage si machine éteinte) |
| `pre_upgrade_hook` | str (commande shell) | non | `None` | exécuté avant l'arrêt du serve |
| `post_upgrade_hook` | str (commande shell) | non | `None` | exécuté après le redémarrage du serve |
| `health_check` | str (commande shell) | non | `None` | code retour 0 = healthy |
| `health_check_retries` | int | non | `5` | |
| `health_check_interval_sec` | int | non | `3` | |

## 6. Interface CLI

```
easystemd add            --name NAME --binary PATH --serve-args STR --upgrade-args STR
                          [--schedule CRON] [--working-dir PATH] [--env-file PATH]
                          [--exec-type simple|forking|exec|notify]
                          [--restart-sec N] [--stop-timeout N] [--randomized-delay N]
                          [--no-persistent]
                          [--pre-upgrade-hook STR] [--post-upgrade-hook STR]
                          [--health-check STR] [--health-check-retries N] [--health-check-interval N]
                          [--dry-run]           # affiche les units générées sans rien écrire/activer
                          [--enable-now/--no-enable-now]  # défaut: enable-now=true

easystemd edit NAME       [mêmes options que add, ne modifie que ce qui est fourni]
                          → régénère les units, daemon-reload, restart si nécessaire

easystemd remove NAME     [--yes]   # stop + disable + suppression units + suppression config

easystemd list            [--json]  # tableau: name, statut serve, prochaine exécution upgrade,
                                     # date/résultat du dernier upgrade

easystemd status NAME     [--json]  # détail complet d'une app (systemctl status combiné +
                                     # contenu de last-run.json)

easystemd upgrade-now NAME [--wait]  # déclenche systemctl --user start <upgrade>.service,
                                      # --wait attend la fin et affiche le résultat

easystemd logs NAME       [--serve|--upgrade] [--follow]  # wrapper autour de journalctl --user -u
                                      # défaut (ni --serve ni --upgrade) : logs du service upgrade
                                      # (priorité au diagnostic d'échec d'upgrade)

easystemd doctor          [--fix]   # cf. section 9

easystemd _run-upgrade NAME   # commande INTERNE, invoquée uniquement par ExecStart= du
                               # service upgrade. Masquée du --help principal.
```

## 7. Cœur du système : `upgrade_runner` (exécuté par `_run-upgrade NAME`)

C'est la partie la plus critique — bien la détailler dans l'implémentation :

1. Charger et valider la config de `NAME`. Si absente/invalide → exit non-zero, rien d'autre.
2. Écrire dans le state file (`.../easystemd/NAME/last-run.json`) un statut `running` +
   `started_at`.
3. Si `pre_upgrade_hook` défini : l'exécuter. **S'il échoue, abandonner immédiatement** (le
   service serve n'a pas encore été touché, donc pas besoin de le redémarrer) — logger
   l'échec, exit non-zero.
4. `systemctl --user stop <name>-serve.service`, avec attente active de l'arrêt effectif
   (poll `systemctl --user is-active` jusqu'à `stop_timeout`).
5. Bloc `try/finally` :
   - `try` : exécuter `binary upgrade_args`, capturer stdout/stderr (tronqués à ~4000
     caractères dans le state file — le détail complet reste de toute façon dans le journal
     via `journalctl`, pas besoin de dupliquer), exit code, durée.
   - `finally` : **toujours** `systemctl --user start <name>-serve.service`, quoi qu'il
     arrive dans le bloc `try` (y compris en cas d'exception Python).
6. Si `health_check` défini : boucle de `health_check_retries` tentatives espacées de
   `health_check_interval_sec`, exécute la commande, `healthy=True` dès qu'un code 0 est
   obtenu, sinon `healthy=False` après épuisement des tentatives.
7. Si `post_upgrade_hook` défini : l'exécuter avec des variables d'environnement injectées :
   `EASYSTEMD_NAME`, `EASYSTEMD_UPGRADE_EXIT_CODE`, `EASYSTEMD_HEALTHY` (`true`/`false`/`skipped`).
8. Mettre à jour le state file final : `finished_at`, `duration_s`, `upgrade_exit_code`,
   `healthy`, `success` (bool composite).
9. Code de sortie du process = non-zero si l'upgrade a échoué OU si le health check a échoué
   après tous les retries. Ainsi `systemctl status`/`journalctl` reflètent correctement
   l'échec, et `easystemd list` peut s'appuyer dessus.

Ne PAS implémenter de rollback automatique du binaire — c'est hors périmètre (on ne peut pas
supposer que tout binaire sait se downgrader). Se contenter de bien faire remonter l'échec.

## 8. Templates systemd (base à adapter, pas figée)

`serve.service.j2` :
```ini
[Unit]
Description=easystemd - {{ name }} (serve)
After=network-online.target
Wants=network-online.target

[Service]
Type={{ exec_type }}
WorkingDirectory={{ working_dir }}
{% if env_file %}EnvironmentFile={{ env_file }}{% endif %}
ExecStart={{ binary }} {{ serve_args }}
Restart=always
RestartSec={{ restart_sec }}
TimeoutStopSec={{ stop_timeout }}

[Install]
WantedBy=default.target
```

`upgrade.service.j2` :
```ini
[Unit]
Description=easystemd - {{ name }} (upgrade)
After=easystemd-{{ name }}-serve.service

[Service]
Type=oneshot
ExecStart={{ easystemd_exe }} _run-upgrade {{ name }}
```

`upgrade.timer.j2` :
```ini
[Unit]
Description=easystemd - {{ name }} (upgrade timer)

[Timer]
OnCalendar={{ schedule }}
Persistent={{ persistent | lower }}
RandomizedDelaySec={{ randomized_delay }}

[Install]
WantedBy=timers.target
```

Convention de nommage des units : `easystemd-{name}-serve.service`,
`easystemd-{name}-upgrade.service`, `easystemd-{name}-upgrade.timer` — pour éviter toute
collision entre apps gérées et rester facilement identifiable via `systemctl --user list-units 'easystemd-*'`.

## 9. `easystemd doctor`

Doit vérifier, sans jamais rien modifier sauf si `--fix` est passé et confirmé :
- linger activé pour l'utilisateur courant (`loginctl show-user $USER -p Linger`) → sinon
  affiche la commande `sudo loginctl enable-linger $USER` à lancer (ou la propose en
  interactif avec `--fix`, jamais silencieusement)
- bus systemd user joignable (`systemctl --user status` ne renvoie pas une erreur de connexion)
- pour chaque app : binaire toujours présent et exécutable à son chemin résolu, units
  présentes sur disque et cohérentes avec la config actuelle (détecter une config modifiée
  sans régénération), timer actif avec une prochaine exécution planifiée
- validité des units sur disque via `systemd-analyze verify` (détecte une unit corrompue ou
  devenue invalide après une mise à jour systemd)

De plus, `add`/`edit` appellent automatiquement `systemd-analyze verify` **après** génération
des units et **avant** activation : en cas d'échec, les units écrites sont rollbackées
(supprimées) et un message clair est affiché — l'utilisateur ne se retrouve jamais avec des
units invalides activées.

## 10. Gestion des erreurs et cas limites à couvrir explicitement

- Nom d'app déjà utilisé lors d'un `add` → erreur claire, suggérer `edit`.
- Binaire introuvable / non exécutable → refuser `add`, message clair.
- `schedule` invalide → valider avec `systemd-analyze calendar "<expr>"` (subprocess, code
  retour) avant d'écrire quoi que ce soit ; message d'erreur si invalide.
- `systemctl --user` indisponible (pas de session dbus, environnement conteneurisé sans
  systemd, etc.) → message explicite, ne pas planter avec une stacktrace brute par défaut
  (stacktrace uniquement si `--verbose`/`--debug`).
- `remove` sur une app inconnue → erreur claire, pas de crash.
- `remove` doit toujours demander confirmation sauf `--yes`.
- Double exécution de `add` avec le même nom → refuser, orienter vers `edit` (pas de
  option `--force` : `add` crée uniquement, `edit` modifie une app existante et échoue
  si le nom est inconnu).

## 11. Tests attendus (pytest)

- Tous les appels à `subprocess.run` doivent être mockables (monkeypatch), **aucun test ne
  doit invoquer un vrai `systemctl`/`loginctl` de la machine de dev/CI**.
- `Path.home()` / `$HOME` doivent être surchargeables dans les tests (fixture `tmp_home`) pour
  ne jamais toucher la vraie config de l'utilisateur qui lance les tests.
- Couvrir : validation des modèles pydantic (valeurs par défaut, rejet de schedule invalide,
  rejet de nom invalide), rendu correct des templates Jinja2 à partir d'une config donnée,
  comportement du CLI (`add`, `edit`, `remove`, `list`, `status` — via `CliRunner` de
  click/typer), et surtout le `upgrade_runner` : vérifier que le service serve est bien
  redémarré même quand l'exécution de la commande d'upgrade lève une exception ou renvoie un
  code non-zero (c'est LE comportement critique à tester en priorité).
- Prévoir un marker `@pytest.mark.integration` pour des tests optionnels qui, eux, appellent
  un vrai systemd user (skip par défaut, à lancer manuellement sur une machine Debian réelle).

## 12. Documentation attendue (README.md)

- Présentation courte du problème résolu et du principe (serve toujours up, upgrade planifié
  avec arrêt/redémarrage garanti).
- Installation (`pipx install .` recommandé, alternative `pip install --user .`).
- Quickstart avec un exemple complet basé sur un binaire fictif `mon_binaire` reprenant
  exactement le cas d'usage de la section 13 ci-dessous.
- Référence de toutes les commandes CLI avec leurs options.
- Explication de l'arborescence de fichiers générés (config, units, state/logs).
- Section "Troubleshooting" centrée sur `easystemd doctor` et le piège classique du linger.
- Note explicite sur le piège du `PATH` restreint dans les services systemd user (cf. section 3).

## 13. Exemple d'utilisation bout-en-bout attendu

```bash
easystemd add \
  --name mon-binaire \
  --binary /usr/local/bin/mon_binaire \
  --serve-args "web" \
  --upgrade-args "upgrade" \
  --schedule "Sun 04:00" \
  --randomized-delay 300

easystemd list
easystemd status mon-binaire
easystemd upgrade-now mon-binaire --wait
easystemd logs mon-binaire --serve --follow
easystemd doctor
easystemd remove mon-binaire --yes
```

## 14. Périmètre : MVP vs bonus vs hors périmètre

**MVP (indispensable) :** `add`/`edit`/`remove`/`list`/`status`/`upgrade-now`/`logs`/`doctor`,
génération des 3 units par app, `upgrade_runner` complet avec garantie stop→upgrade→start
même en cas d'échec, **hooks pre/post-upgrade et `health_check` avec retries** (tels que
détaillés section 7), résolution en chemins absolus, gestion du linger (détection +
instruction, pas d'exécution silencieuse), validation `systemd-analyze verify` dans
`add`/`edit`/`doctor`, sortie `--json` sur `list`/`status`, tests unitaires sur le
`upgrade_runner` et la génération de templates.

**Bonus (si le temps le permet) :** affichage `rich` soigné, `doctor --fix` interactif
(confirmation guidée pour le linger notamment).

**Hors périmètre (ne pas implémenter) :** rollback automatique du binaire, notifications
externes (email/webhook), support des units systemd *system* (root), support d'autres init
systems que systemd.

## 15. Checklist de "definition of done"

- [ ] `easystemd add` sur un binaire de test génère bien 3 units valides (`systemd-analyze verify` passe)
- [ ] Le serve redémarre automatiquement après un crash simulé (`Restart=always` fonctionnel)
- [ ] `upgrade-now` sur un upgrade qui échoue (mock/exit code non-zero) redémarre quand même le serve
- [ ] `doctor` détecte correctement un linger désactivé
- [ ] Tous les chemins dans les units générées sont absolus (aucune dépendance au `PATH`)
- [ ] `pytest` passe intégralement sans toucher au vrai systemd de la machine
- [ ] README permet à quelqu'un qui découvre l'outil de l'utiliser sans aide externe