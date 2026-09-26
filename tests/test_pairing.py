import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PairingVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, **extra):
        data = {"name": name, "sex": sex}
        data.update(extra)
        return self.service.create(self.admin, "animal", data)

    def _approved_pairing(self, sire, dam):
        pairing = self.service.create(self.admin, "pairing", {"proposed_by": "coordinator"})
        return self.service.transition(
            self.admin,
            pairing["id"],
            "approve",
            {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
        )

    def _complete(self, pairing_id):
        return self.service.transition(
            self.admin, pairing_id, "complete", {"offspring_ids": ["offspring-1"]}
        )

    def test_approve_snapshots_versions_and_pedigree(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female", sire_id="F-0", dam_id="F-00")
        pairing = self._approved_pairing(sire, dam)
        data = pairing["data"]
        self.assertEqual(data["sire_version"], sire["version"])
        self.assertEqual(data["dam_version"], dam["version"])
        self.assertEqual(
            data["sire_pedigree"],
            {"id": sire["id"], "sire_id": None, "dam_id": None},
        )
        self.assertEqual(
            data["dam_pedigree"],
            {"id": dam["id"], "sire_id": "F-0", "dam_id": "F-00"},
        )

    def test_complete_records_verified_versions(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        pairing = self._approved_pairing(sire, dam)
        completed = self._complete(pairing["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["verified_sire_version"], sire["version"])
        self.assertEqual(completed["data"]["verified_dam_version"], dam["version"])
        self.assertEqual(completed["data"]["offspring_ids"], ["offspring-1"])

    def test_complete_conflicts_when_parent_no_longer_active(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        pairing = self._approved_pairing(sire, dam)
        self.service.transition(self.admin, dam["id"], "quarantine_animal", {"reason": "illness"})
        with self.assertRaises(ConflictError):
            self._complete(pairing["id"])
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_complete_conflicts_when_parent_profile_changed(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        pairing = self._approved_pairing(sire, dam)
        # 隔离再解除后状态回到 active，但档案版本已变化
        self.service.transition(self.admin, dam["id"], "quarantine_animal", {"reason": "check"})
        self.service.transition(self.admin, dam["id"], "release_quarantine", {})
        self.assertEqual(self.service.get(dam["id"])["status"], "active")
        with self.assertRaises(ConflictError):
            self._complete(pairing["id"])
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    def test_complete_conflicts_when_inbreeding_exceeds_threshold(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        pairing = self._approved_pairing(sire, dam)
        # 批准后母本的父系信息被改为公本本身，亲缘系数升至 0.25
        current = self.service.get(dam["id"])
        changed = dict(current["data"])
        changed["sire_id"] = sire["id"]
        self.repo.update_entity(dam["id"], current["version"], current["status"], changed)
        with self.assertRaises(ConflictError):
            self._complete(pairing["id"])
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")


if __name__ == "__main__":
    unittest.main()
