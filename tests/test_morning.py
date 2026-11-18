from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from creator.morning import Morning
from creator.proposal import generate
from creator.studio import create_app

BRIEF = {"idea": "Idée", "shooting_brief": "Deux plans", "script": "Texte"}
MON = datetime(2026, 11, 16, 8)
TUE = MON.replace(day=17)


class MorningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.job = Morning(self.root, agents=self.root / "agents")
        (self.job.folder / "contexte.txt").write_text("Atelier de papier")
        self.job.save({"enabled": True, "first": "2026-11-16"})
        self.model = self.enterContext(patch("creator.morning.generate", return_value=BRIEF))

    def briefs(self):
        return list(self.root.glob("*.json"))

    def test_time_and_parallel_repeat(self):
        self.job.tick(MON.replace(hour=7, minute=59))
        self.assertFalse(self.briefs())
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.job.tick, [MON, MON]))
        self.assertEqual(len(self.briefs()), 1)
        self.model.assert_called_once_with("", "", context="Atelier de papier", recent_ideas=[])
        self.job.tick(TUE)
        self.assertEqual(len(self.briefs()), 2)

    def test_backlog_and_slot_freed(self):
        for _ in range(3):
            self.job.flow.create_proposal(**BRIEF)
        self.job.tick(MON)
        self.model.assert_not_called()
        self.job.flow.approve_footage(self.briefs()[0].stem, [self.job.settings], actor="test")
        self.job.tick(MON)
        self.model.assert_not_called()
        self.job.tick(TUE)
        self.assertEqual(sum(json.loads(p.read_text())["stage"] == "proposed" for p in self.briefs()), 3)

    def test_disabled_and_crash_recovery(self):
        self.job.save({"enabled": False})
        self.job.tick(MON)
        self.model.assert_not_called()
        self.job.save({"enabled": True, "first": "2026-11-16"})
        self.job.flow.create_proposal(**BRIEF, scheduled_day="2026-11-16")
        self.job.tick(MON)
        self.model.assert_not_called()

    def test_generation_failure_creates_no_partial_project(self):
        self.model.side_effect = ValueError("offline")
        with self.assertRaises(ValueError):
            self.job.tick(MON)
        self.assertFalse(self.briefs())
        self.assertIn("Échec", self.job.status()["message"])
        self.model.side_effect = None
        self.job.tick(TUE)
        self.assertEqual(len(self.briefs()), 1)

    def test_linux_timer_units_and_removal(self):
        self.job.save({"enabled": False})
        with patch("sys.platform", "linux"), patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("creator.morning.provider", return_value="claude"), patch("subprocess.run") as ctl:
            self.job.change(True, now=MON)
            timer, = self.job.units.glob("*.timer")
            service, = self.job.units.glob("*.service")
            self.assertIn("OnCalendar=*-*-* 08:00:00", timer.read_text())
            self.assertIn("CREATOR_MODEL_CLI=claude", service.read_text())
            self.assertIn("creator.morning", service.read_text())
            self.assertEqual(ctl.call_args.args[0][2:4], ["enable", "--now"])
            self.job.change(False)
            self.assertFalse(timer.exists())
            self.assertFalse(service.exists())

    def test_activation_from_studio(self):
        self.job.save({"enabled": False})
        with patch("creator.studio.Morning", return_value=self.job), patch("sys.platform", "darwin"), \
             patch("creator.morning.provider", return_value="claude"), patch("subprocess.run") as ctl, \
             TestClient(create_app(self.root), base_url="http://localhost") as client:
            headers = {"X-Creator-Studio": "1", "Origin": "http://localhost"}
            url = "/api/morning"
            self.assertFalse(client.get(url).json()["enabled"])
            self.assertTrue(client.post(url, data={"enabled": "true"}, headers=headers).json()["enabled"])
            spec = plistlib.loads(self.job.plist.read_bytes())
            self.assertEqual(spec["StartCalendarInterval"], {"Hour": 8, "Minute": 0})
            self.assertEqual(spec["EnvironmentVariables"]["CREATOR_MODEL_CLI"], "claude")
            self.assertEqual(ctl.call_args.args[0][1], "bootstrap")
            self.assertFalse(client.post(url, data={"enabled": "false"}, headers=headers).json()["enabled"])
            self.assertFalse(self.job.plist.exists())
            self.assertEqual(ctl.call_args.args[0][1], "bootout")


class ClaudeTests(unittest.TestCase):
    def test_cli_receives_schema_context_and_returns_fields(self):
        with patch("creator.proposal.shutil.which", return_value="/bin/claude"), \
             patch.dict(os.environ, {"CREATOR_MODEL_CLI": "claude"}), \
             patch("creator.proposal.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"structured_output": BRIEF}))
            self.assertEqual(generate("", "", context="Mon entreprise"), BRIEF)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ["claude", "-p"])
            self.assertEqual(set(json.loads(argv[argv.index("--json-schema")+1])["required"]), set(BRIEF))
            self.assertIn("Mon entreprise", run.call_args.kwargs["input"])
