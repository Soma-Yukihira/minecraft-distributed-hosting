"""User-facing French wording, kept in one place."""

from __future__ import annotations

from mdh_client.minecraft.modrinth import ModrinthError
from mdh_client.networking.api_client import ApiError, ApiUnavailable

STATE_LABELS = {
    "offline": ("Arrêté", "neutral"),
    "electing": ("Recherche d'un PC hôte…", "busy"),
    "assigned": ("Préparation chez l'hôte…", "busy"),
    "starting": ("Démarrage…", "busy"),
    "running": ("En ligne", "ok"),
    "migrating": ("Changement de PC…", "busy"),
    "stopping": ("Arrêt en cours…", "busy"),
    "unreachable": ("Hôte injoignable", "warn"),
    "orphaned": ("Hôte disparu : décision nécessaire", "error"),
}

PHASE_LABELS = {
    "preparing": "Ce PC prépare le serveur (téléchargements, vérifications)…",
    "starting": "Ce PC démarre Minecraft…",
    "running": "Ce PC héberge le serveur",
    "stopping": "Ce PC arrête le serveur proprement…",
    "handing_over": "Ce PC passe le serveur à un autre PC…",
    "saving": "Envoi de la sauvegarde finale au serveur central…",
    "fenced": "Arrêt de sécurité : connexion au serveur central perdue",
    "released": "Ce PC n'héberge plus le serveur",
}

EVENT_LABELS = {
    "group_created": "Groupe créé",
    "member_joined": "Un membre a rejoint le groupe",
    "member_updated": "Droits d'un membre modifiés",
    "member_removed": "Un membre a quitté le groupe",
    "member_leaving": "Départ d'un membre qui héberge : le serveur change d'abord de PC",
    "owner_transferred": "Le groupe a un nouveau propriétaire",
    "invite_created": "Invitation créée",
    "server_created": "Serveur créé",
    "server_updated": "Configuration du serveur modifiée",
    "start_requested": "Démarrage demandé",
    "stop_requested": "Arrêt demandé",
    "offer_sent": "Hébergement proposé à un PC",
    "offer_declined": "Un PC a refusé d'héberger",
    "offer_expired": "Un PC n'a pas répondu",
    "no_eligible_host": "Aucun PC disponible pour héberger",
    "lease_granted": "Un PC a pris l'hébergement",
    "lease_released": "Hébergement terminé proprement",
    "lease_expired": "L'hôte a disparu sans sauvegarde finale",
    "host_unreachable": "L'hôte ne répond plus",
    "host_reachable_again": "L'hôte répond de nouveau",
    "host_downloading": "L'hôte prépare les fichiers",
    "host_starting": "L'hôte démarre Minecraft",
    "host_running": "Serveur en ligne",
    "host_stopping": "L'hôte arrête le serveur",
    "host_crashed": "Minecraft s'est arrêté brutalement",
    "failover_confirmed": "Relance confirmée depuis la dernière sauvegarde",
    "auto_failover": "Relance automatique depuis la dernière sauvegarde",
    "resume_offered": "L'ancien hôte est revenu : reprise proposée",
    "version_committed": "Sauvegarde envoyée au serveur central",
    "version_restored": "Sauvegarde restaurée",
    "version_pinned": "Sauvegarde marquée comme importante",
    "migration_started": "Changement de PC demandé",
    "migration_prefetched": "Le nouveau PC a téléchargé le monde",
    "migration_handover": "Sauvegarde finale envoyée : le nouveau PC prend le relais",
    "migration_completed": "Changement de PC terminé",
    "migration_cancelled": "Changement de PC annulé : le serveur reste où il est",
    "migration_failed": "Le nouveau PC n'a pas pu démarrer : retour à l'élection",
    "import_started": "Import d'un monde en cours",
    "import_abandoned": "Import interrompu : le PC qui importait a disparu",
    "admin_action": "Action d'un administrateur ou modérateur sur un joueur",
    "access_requested": "Un joueur demande à rejoindre le serveur",
    "access_accepted": "Demande d'accès acceptée",
    "access_refused": "Demande d'accès refusée",
    "new_place_requested": "Un joueur se connecte depuis un nouvel endroit",
    "new_place_accepted": "Nouvel endroit accepté",
    "new_place_refused": "Nouvel endroit refusé",
}

ROLE_LABELS = {"owner": "Propriétaire", "admin": "Administrateur", "moderator": "Modérateur",
               "member": "Membre"}

#: Why a change of host stopped (``detail`` of a migration).
MIGRATION_DETAILS = {
    "cancelled_by_user": "annulé par un membre",
    "target_offline": "le PC choisi s'est déconnecté",
    "target_declined": "le PC choisi a refusé",
    "prefetch_failed": "le PC choisi n'a pas pu télécharger le monde",
    "prefetch_timeout": "téléchargement du monde trop long",
    "source_unreachable": "le PC hôte ne répond plus",
    "source_lost": "le PC hôte a disparu",
    "source_stopped": "le PC hôte s'est arrêté",
    "stop_requested": "arrêt du serveur demandé",
    "offer_expired": "le PC choisi n'a pas répondu",
    "target_failed": "le PC choisi n'a pas pu démarrer le serveur",
}

#: Why the control plane did not pick a PC (``no_eligible_host`` event).
INELIGIBILITY_LABELS = {
    "offline": "hors ligne",
    "hosting_disabled_by_user": "hébergement désactivé dans ses paramètres",
    "no_host_permission": "pas autorisé à héberger dans ce groupe",
    "disabled_for_server": "désactivé pour ce serveur",
    "not_enough_ram": "pas assez de RAM libre",
    "not_enough_disk": "pas assez d'espace disque",
    "network_unavailable": "réseau indisponible",
    "excluded": "a refusé ou n'a pas répondu à la proposition",
}

ERROR_CODES = {
    "invalid_credentials": "Nom d'utilisateur ou mot de passe incorrect.",
    "rate_limited": "Trop de tentatives d'affilée. Réessaie un peu plus tard.",
    "invite_required": "Une invitation est nécessaire pour créer un compte.",
    "invalid_invite": "Ce code d'invitation est invalide, expiré ou déjà utilisé.",
    "username_taken": "Ce nom d'utilisateur est déjà pris.",
    "already_member": "Tu fais déjà partie de ce groupe.",
    "forbidden": "Tu n'as pas les droits pour cette action.",
    "needs_failover_confirmation": "L'hôte précédent a disparu : un administrateur doit confirmer la "
                                   "relance depuis la dernière sauvegarde.",
    "not_isolated_yet": "L'ancien hôte pourrait encore écrire le monde. Réessaie dans quelques secondes.",
    "member_is_hosting": "Ce membre héberge le serveur : arrête-le d'abord.",
    "limit_reached": "Limite atteinte.",
    "unavailable": "Le serveur central est injoignable. Vérifie ta connexion.",
    "certificate_mismatch": ("Le serveur central ne présente pas le certificat attendu : connexion refusée "
                             "par sécurité. Si son certificat a été changé, demande la nouvelle adresse "
                             "complète (avec la partie après #) à l'administrateur."),
    "token_reused": "Ta session a été révoquée par sécurité. Reconnecte-toi.",
    "not_running": "Le serveur doit être en ligne pour changer de PC.",
    "migration_in_progress": "Un changement de PC est déjà en cours.",
    "already_hosting": "Ce PC héberge déjà le serveur.",
    "target_not_eligible": "Ce PC ne peut pas héberger en ce moment",
    "no_eligible_host": "Aucun autre PC ne peut héberger en ce moment.",
    "target_offline": "Le PC choisi n'est pas connecté.",
    "too_late": "Trop tard : le PC actuel a déjà passé la main.",
    "no_migration": "Aucun changement de PC en cours.",
    "too_many_players": "La liste des joueurs autorisés est pleine (200 au plus).",
}


def health_label(tps: float | None, mspt: float | None) -> str:
    """Game smoothness in words (20 ticks per second is the normal pace of Minecraft)."""
    if tps is None:
        return ""
    if tps >= 19.5:
        words = "fluide"
    elif tps >= 17:
        words = "léger ralentissement"
    elif tps >= 12:
        words = "le serveur ralentit"
    else:
        words = "le serveur rame fortement"
    detail = f" · {mspt:.0f} ms par tick" if mspt is not None else ""
    return f"Fluidité : {tps:.1f}/20 ({words}){detail}"


def state_label(state: str | None) -> tuple[str, str]:
    return STATE_LABELS.get(state or "", ("Inconnu", "neutral"))


def alert_text(alert: dict) -> str:
    """One sentence for an alert of the VPS (docs/API.md section 2 bis)."""
    d = alert.get("details") or {}
    gb = 1024**3
    if alert["code"] == "vps_disk_low":
        return (f"Le disque du VPS est presque plein : {d.get('free_bytes', 0) / gb:.1f} Go libres sur "
                f"{d.get('total_bytes', 0) / gb:.0f} Go. Les sauvegardes du monde risquent d'échouer.")
    if alert["code"] == "vps_memory_low":
        return f"Le VPS manque de mémoire : {d.get('available_mb')} Mo disponibles."
    if alert["code"] == "vps_restarted":
        return ("Le serveur central a redémarré après un arrêt imprévu (plantage ou coupure). "
                "Détails sur le VPS : sudo bash install.sh status")
    if alert["code"] == "group_quota_high":
        used, quota = d.get("used_bytes", 0), d.get("quota_bytes", 1)
        return (f"Le groupe « {d.get('group_name', '?')} » utilise {used * 100 // max(quota, 1)} % de son "
                f"espace sur le VPS ({used / gb:.1f} Go sur {quota / gb:.0f} Go). Les plus vieilles "
                "sauvegardes non marquées « importantes » sont supprimées automatiquement ; sinon, "
                "l'administrateur du VPS peut augmenter le quota.")
    return f"Alerte du serveur central : {alert['code']}"


def error_text(error: BaseException) -> str:
    if isinstance(error, ApiUnavailable):
        return ERROR_CODES.get(error.code, ERROR_CODES["unavailable"])
    if isinstance(error, ApiError) and error.code == "rate_limited":
        wait = int(error.details.get("retry_after_s") or 60)
        when = (f"{wait} secondes" if wait < 90 else
                f"{-(-wait // 60)} minutes" if wait < 5400 else f"{-(-wait // 3600)} heures")
        return f"Trop de tentatives d'affilée. Réessaie dans {when}."
    if isinstance(error, ApiError) and error.code == "server_too_old":
        return (f"Le serveur central (version {error.details.get('server_version') or 'inconnue'}) est "
                f"trop ancien pour cette application, qui demande au moins la {error.details.get('required')}. "
                "L'administrateur du VPS doit le mettre à jour (install.sh app).")
    if isinstance(error, ApiError) and error.code == "mods_incompatible":
        problems = (error.details or {}).get("problems") or []
        return "Ces mods ne peuvent pas fonctionner sur ce serveur : " + " ".join(problems)
    if isinstance(error, ModrinthError):
        return str(error)
    if isinstance(error, ApiError):
        text = ERROR_CODES.get(error.code, error.message)
        reasons = error.details.get("reasons") if error.details else None
        if reasons:
            text += " : " + ", ".join(INELIGIBILITY_LABELS.get(r, r) for r in reasons) + "."
        return text
    if isinstance(error, ValueError):
        return str(error)
    return f"Erreur inattendue : {error}"


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type.replace("_", " "))
