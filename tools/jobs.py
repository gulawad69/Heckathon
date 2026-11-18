"""Tiny background-job runner for the workbench (run evals / tests / API server / k6 from the UI). Jobs are subprocesses with
log files under /tmp/pharma_jobs; nothing here changes what is measured - it only launches the same commands as the README."""
from __future__ import annotations

import os, shlex, signal, subprocess, time
from pathlib import Path

LOGDIR = Path("/tmp/pharma_jobs"); LOGDIR.mkdir(exist_ok=True)
PROFILE = Path("profiles/local_cpu.env")


def profile_env() -> dict:
    env = {}
    for line in PROFILE.read_text().splitlines():
        if line.startswith("export ") and "=" in line:
            k, v = line[7:].split("=", 1); env[k.strip()] = v.strip()
    return env


class Jobs:
    def __init__(self):
        self.p: dict[str, subprocess.Popen] = {}
        self.started: dict[str, float] = {}
        self.cmd: dict[str, str] = {}

    def start(self, name: str, cmd: str, local_profile: bool = True) -> None:
        if self.running(name): return
        env = {**os.environ}
        if local_profile: env = {**profile_env(), **env} if False else {**env, **profile_env()}
        env.setdefault("MCP_MANIFEST_KEY", "dev-only-local-key")
        log = open(LOGDIR / f"{name}.log", "w")
        self.p[name] = subprocess.Popen(["bash", "-c", cmd], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.started[name], self.cmd[name] = time.time(), cmd

    def running(self, name: str) -> bool:
        return name in self.p and self.p[name].poll() is None

    def code(self, name: str):
        return self.p[name].poll() if name in self.p else None

    def stop(self, name: str) -> None:
        if self.running(name):
            os.killpg(os.getpgid(self.p[name].pid), signal.SIGTERM)

    def tail(self, name: str, n: int = 25) -> str:
        f = LOGDIR / f"{name}.log"
        if not f.exists(): return ""
        lines = [l for l in f.read_text(errors="replace").splitlines() if "Hub" not in l and "Loading weights" not in l]
        return "\n".join(lines[-n:])
