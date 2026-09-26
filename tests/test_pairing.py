import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import INBREEDING_THRESHOLD, RuleEngine, inbreeding_coefficient
from src.service import DomainService


class PairingVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _approve_pairing(self):
        sire = self.service.create(
            self.admin, "animal", {"name": "M-1", "sex": "male"}
        )
        dam = self.service.create(
            self.admin, "animal", {"name": "F-1", "sex": "female"}
        )
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coordinator-1"}
        )
        approved = self.service.transition(
            self.admin,
            pairing["id"],
            "approve",
            {
                "sire_id": sire["id"],
                "dam_id": dam["id"],
                "approvals": ["vet-1"],
            },
        )
        self.assertEqual(approved["status"], "approved")
        return sire, dam, approved

    def _revise_animal_archive(self, animal, changes):
        """Simulates an out-of-band pedigree/archive revision bumping version."""
        data = dict(animal["data"])
        data.update(changes)
        return self.repo.update_entity(
            animal["id"], animal["version"], animal["status"], data
        )

    def test_approval_records_archive_versions_and_pedigree_summaries(self):
        sire, dam, pairing = self._approve_pairing()
        data = pairing["data"]
        self.assertEqual(data["sire_version"], sire["version"])
        self.assertEqual(data["dam_version"], dam["version"])
        self.assertEqual(data["sire_pedigree"]["id"], sire["id"])
        self.assertEqual(data["sire_pedigree"]["sex"], "male")
        self.assertEqual(data["dam_pedigree"]["id"], dam["id"])
        self.assertEqual(data["dam_pedigree"]["sex"], "female")
        self.assertEqual(data["inbreeding_coefficient"], 0.0)
        self.assertEqual(data["approved_by"], "admin")

    def test_complete_rechecks_and_records_verified_versions(self):
        sire, dam, pairing = self._approve_pairing()
        completed = self.service.transition(
            self.admin,
            pairing["id"],
            "complete",
            {"offspring_ids": ["offspring-1"]},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["verified_sire_version"], sire["version"])
        self.assertEqual(completed["data"]["verified_dam_version"], dam["version"])
        self.assertEqual(
            completed["data"]["verified_inbreeding_coefficient"],
            inbreeding_coefficient(
                {"id": sire["id"]}, {"id": dam["id"]}
            ),
        )

    def test_completion_blocked_when_sire_deceased_keeps_approved(self):
        sire, dam, pairing = self._approve_pairing()
        self.service.transition(
            self.admin, sire["id"], "mark_deceased", {"cause": "illness"}
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.admin,
                pairing["id"],
                "complete",
                {"offspring_ids": ["offspring-1"]},
            )
        self.assertIn("sire", str(caught.exception))

        reloaded = self.service.get(pairing["id"])
        self.assertEqual(reloaded["status"], "approved")
        self.assertNotIn("verified_sire_version", reloaded["data"])

    def test_completion_blocked_when_animal_quarantined_keeps_approved(self):
        sire, dam, pairing = self._approve_pairing()
        self.service.transition(
            self.admin, sire["id"], "quarantine_animal", {"reason": "checkup"}
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.admin,
                pairing["id"],
                "complete",
                {"offspring_ids": ["offspring-1"]},
            )
        self.assertIn("quarantined", str(caught.exception))
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_completion_blocked_when_archive_revised_after_approval(self):
        sire, dam, pairing = self._approve_pairing()
        granddam = self.service.create(
            self.admin, "animal", {"name": "F-0", "sex": "female"}
        )
        self._revise_animal_archive(dam, {"dam_id": granddam["id"]})

        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.admin,
                pairing["id"],
                "complete",
                {"offspring_ids": ["offspring-1"]},
            )
        self.assertIn("archive changed", str(caught.exception))
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_completion_blocked_when_pedigree_becomes_related(self):
        sire, dam, pairing = self._approve_pairing()
        # Record the sire as the dam's father: coefficient becomes 0.25.
        self._revise_animal_archive(dam, {"sire_id": sire["id"]})
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.admin,
                pairing["id"],
                "complete",
                {"offspring_ids": ["offspring-1"]},
            )
        message = str(caught.exception)
        self.assertIn("inbreeding", message)
        self.assertIn(str(INBREEDING_THRESHOLD), message)
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_approval_rejects_related_pair_with_identities(self):
        sire = self.service.create(
            self.admin, "animal", {"name": "M-1", "sex": "male"}
        )
        daughter = self.service.create(
            self.admin,
            "animal",
            {"name": "F-1", "sex": "female", "sire_id": sire["id"]},
        )
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coordinator-1"}
        )
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.admin,
                pairing["id"],
                "approve",
                {
                    "sire_id": sire["id"],
                    "dam_id": daughter["id"],
                    "approvals": ["vet-1"],
                },
            )
        self.assertIn("inbreeding", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
