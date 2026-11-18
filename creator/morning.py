"""One user calendar timer per studio library."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from .proposal import generate, provider
from .workflow import CreatorWorkflow


class Morning:
    def __init__(self, root, *, agents=None):
        self.flow = CreatorWorkflow(root)
        self.folder = self.flow.root / "profil"
        self.folder.mkdir(exist_ok=True)
        self.settings = self.folder / "matin.json"
        key = hashlib.sha256(os.fsencode(self.flow.root)).hexdigest()[:20]
        self.label = f"local.agentic-content.brief.{key}"
        directory = agents if agents else Path.home() / "Library/LaunchAgents"
        self.plist = directory / (self.label + ".plist")
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.units = agents if agents else config_home / "systemd/user"
        self.domain = f"gui/{os.getuid()}"

    @contextmanager
    def lock(self):
        with (self.folder / "brief.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def status(self):
        config = json.loads(self.settings.read_text()) if self.settings.exists() else {"enabled": False}
        return dict(config, supported=sys.platform == "darwin" or (sys.platform.startswith("linux") and bool(shutil.which("systemctl"))))

    def save(self, config):
        temp = self.settings.with_suffix(".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False))
        temp.replace(self.settings)

    def linux_timer(self, spec, enabled):
        service = self.units / (self.label + ".service")
        timer = self.units / (self.label + ".timer")
        ctl = ["systemctl", "--user"]
        if enabled:
            if not shutil.which("systemctl"):
                raise ValueError("La programmation Linux demande systemd dans la session utilisateur.")
            def unit_value(value):
                if any(ch in value for ch in "\r\n"):
                    raise ValueError("Chemin incompatible avec un service systemd.")
                return '"' + value.replace(chr(92), chr(92)*2).replace('"', chr(92)+'"').replace("%", "%%") + '"'
            argv = " ".join(unit_value(v).replace("$", "$$") for v in spec["ProgramArguments"])
            env = " ".join(unit_value(f"{k}={v}") for k, v in spec["EnvironmentVariables"].items())
            self.units.mkdir(exist_ok=True, parents=True)
            service.write_text("[Unit]\nDescription=Brief du matin\n[Service]\nType=oneshot\n"
                               + "WorkingDirectory=" + spec["WorkingDirectory"].replace("%", "%%") + "\n"
                               + f"Environment={env}\nExecStart={argv}\n")
            timer.write_text("[Unit]\nDescription=Brief du matin\n[Timer]\nOnCalendar=*-*-* 08:00:00\n"
                             "Persistent=true\n[Install]\nWantedBy=timers.target\n")
            try:
                subprocess.run([*ctl, "daemon-reload"], check=True)
                subprocess.run([*ctl, "enable", "--now", timer.name], check=True)
            except subprocess.CalledProcessError as exc:
                subprocess.run([*ctl, "disable", "--now", timer.name], check=False)
                service.unlink(missing_ok=True)
                timer.unlink(missing_ok=True)
                raise ValueError("Impossible d’activer le timer systemd de cette session.") from exc
        else:
            subprocess.run([*ctl, "disable", "--now", timer.name], check=True)
            timer.unlink(missing_ok=True)
            service.unlink(missing_ok=True)
            subprocess.run([*ctl, "daemon-reload"], check=True)

    def change(self, enabled, now=None):
        if not self.status()["supported"]:
            raise ValueError("La programmation demande macOS ou Linux avec systemd.")
        with self.lock():
            config = self.status()
            if enabled == config["enabled"]:
                return config
            if enabled:
                model = provider()
                if not (self.folder / "contexte.txt").is_file():
                    raise ValueError("Enregistre ton contexte avant d’activer les briefs du matin.")
                # A calendar trigger handles sleep. RunAtLoad handles login after 08:00.
                spec = {"Label": self.label, "ProgramArguments": [sys.executable, "-m", "creator.morning",
                        str(self.flow.root)], "WorkingDirectory": str(Path(__file__).resolve().parents[1]),
                        "EnvironmentVariables": {"PATH": os.environ["PATH"], "CREATOR_MODEL_CLI": model},
                        "StartCalendarInterval": {"Hour": 8, "Minute": 0}, "RunAtLoad": True}
                if sys.platform.startswith("linux"):
                    self.linux_timer(spec, True)
                else:
                    self.plist.parent.mkdir(parents=True, exist_ok=True)
                    self.plist.write_bytes(plistlib.dumps(spec))
                    try:
                        subprocess.run(["launchctl", "bootstrap", self.domain, str(self.plist)], check=True)
                    except subprocess.CalledProcessError:
                        self.plist.unlink(missing_ok=True)
                        raise ValueError("macOS a refusé le lancement. Ouvre le studio depuis ta session utilisateur.")
                instant = now or datetime.now()
                first = instant.date() if instant.hour < 8 else instant.date() + timedelta(days=1)
                config.update(enabled=True, first=first.isoformat(), model=model, message="Programmé")
            else:
                # Failure leaves the previous settings intact so disable can be retried.
                if sys.platform.startswith("linux"):
                    self.linux_timer({}, False)
                else:
                    subprocess.run(["launchctl", "bootout", f"{self.domain}/{self.label}"], check=True)
                    self.plist.unlink(missing_ok=True)
                config.update(enabled=False, message="Désactivé")
            self.save(config)
            return config

    def tick(self, now=None):
        with self.lock():
            instant = now or datetime.now()
            today = instant.date().isoformat()
            config = self.status()
            if (not config["enabled"] or instant.hour < 8 or today < config["first"]
                    or config.get("day") == today):
                return
            try:
                records = sorted((self.flow.read(p.stem) for p in self.flow.root.glob("*.json")),
                                 key=lambda p: p["updated_at"], reverse=True)
                if any(p.get("scheduled_day") == today for p in records):
                    message = "Brief déjà créé"
                elif sum(p["stage"] == "proposed" for p in records) >= 3:
                    message = "Trois briefs sont déjà en attente"
                else:
                    context = (self.folder / "contexte.txt").read_text().strip()
                    if not context:
                        raise ValueError("Contexte manquant")
                    proposal = generate("", "", context=context,
                                        recent_ideas=[p["proposal"]["idea"] for p in records[:12]])
                    self.flow.create_proposal(**proposal, scheduled_day=today)
                    message = "Nouveau brief disponible"
                config.update(day=today, message=message)
            except Exception:
                config.update(message="Échec de génération. Vérifie ta connexion et ton accès au modèle.")
                raise
            finally:
                config["checked"] = instant.isoformat(timespec="minutes")
                self.save(config)


if __name__ == "__main__":
    Morning(sys.argv[1]).tick()
