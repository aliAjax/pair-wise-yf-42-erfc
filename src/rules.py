from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


INBREEDING_THRESHOLD = 0.125


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _animal_profile(animal):
    profile = dict(animal["data"])
    profile["id"] = animal["id"]
    return profile


def pedigree_summary(animal):
    return {
        "id": animal["id"],
        "sex": animal["data"].get("sex"),
        "sire_id": animal["data"].get("sire_id"),
        "dam_id": animal["data"].get("dam_id"),
    }


def _pairing_animals(data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    return sire, dam


def _approve_pairing(actor, entity, data, lookup):
    sire, dam = _pairing_animals(data, lookup)
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    coefficient = inbreeding_coefficient(
        _animal_profile(sire), _animal_profile(dam)
    )
    if coefficient > INBREEDING_THRESHOLD:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {
        "approved_by": actor.user_id,
        "sire_version": sire["version"],
        "dam_version": dam["version"],
        "sire_pedigree": pedigree_summary(sire),
        "dam_pedigree": pedigree_summary(dam),
        "inbreeding_coefficient": coefficient,
    }


def _archive_conflicts(role, animal, approved_version):
    conflicts = []
    if approved_version is None:
        conflicts.append(role + " approval archive snapshot is missing")
    elif animal["version"] != approved_version:
        conflicts.append(
            "%s archive changed: version %s at approval, %s now"
            % (role, approved_version, animal["version"])
        )
    if animal["status"] != "active":
        conflicts.append(
            "%s is no longer active (status %s)" % (role, animal["status"])
        )
    return conflicts


def _complete_pairing(actor, entity, data, lookup):
    pairing = entity["data"]
    sire_id = pairing.get("sire_id")
    dam_id = pairing.get("dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id) if sire_id else None
    dam = _find_one(lookup, "animal", "id", dam_id) if dam_id else None

    conflicts = []
    if not sire:
        conflicts.append(
            "sire %s is no longer in the collection" % (sire_id or "unknown")
        )
    if not dam:
        conflicts.append(
            "dam %s is no longer in the collection" % (dam_id or "unknown")
        )
    if sire:
        conflicts.extend(
            _archive_conflicts("sire", sire, pairing.get("sire_version"))
        )
    if dam:
        conflicts.extend(
            _archive_conflicts("dam", dam, pairing.get("dam_version"))
        )

    coefficient = None
    if sire and dam:
        coefficient = inbreeding_coefficient(
            _animal_profile(sire), _animal_profile(dam)
        )
        if coefficient > INBREEDING_THRESHOLD:
            conflicts.append(
                "inbreeding coefficient %s exceeds threshold %s"
                % (coefficient, INBREEDING_THRESHOLD)
            )

    if conflicts:
        raise ConflictError(
            "pairing completion check failed: " + "; ".join(conflicts)
        )

    return {
        "verified_sire_version": sire["version"],
        "verified_dam_version": dam["version"],
        "verified_inbreeding_coefficient": coefficient,
    }


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _approve_pairing,
    ('pairing', 'complete'): _complete_pairing,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
