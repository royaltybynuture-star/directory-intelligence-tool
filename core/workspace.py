"""Per-run workspace: manages workspace/<profile>/<run_id>/ directories and artifact IO.

New runs always land under workspace/<profile_name>/<run_id>/. Standalone extracts
(no profile) use workspace/standalone/<run_id>/.

Backward compat: legacy flat workspace/<run_id>/ directories are still readable.
Resolution order for run_dir(run_id, profile_name):
  1. workspace/<profile_name>/<run_id>    (namespaced — preferred)
  2. workspace/<run_id>                   (legacy flat)
  3. Any workspace/<other_profile>/<run_id> (global search, for explicit IDs only)
"""
import json
import re
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent / "workspace"
STANDALONE_PROFILE = "standalone"

_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}$")


def _looks_like_run_id(name: str) -> bool:
    return bool(_RUN_ID_RE.match(name))


def _find_run_dir(run_id: str, profile_name: str | None = None) -> Path | None:
    """Locate an existing run dir. Returns None if not found anywhere."""
    if profile_name:
        namespaced = WORKSPACE_ROOT / profile_name / run_id
        if namespaced.exists():
            return namespaced
    flat = WORKSPACE_ROOT / run_id
    if flat.exists():
        return flat
    # Global search across all profile subdirs (used for explicit run IDs so that
    # switching the active profile doesn't break --from-find <explicit_id>).
    if WORKSPACE_ROOT.exists():
        for item in WORKSPACE_ROOT.iterdir():
            if item.is_dir() and not item.name.startswith(".") and not _looks_like_run_id(item.name):
                candidate = item / run_id
                if candidate.exists():
                    return candidate
    return None


def run_dir(run_id: str, profile_name: str | None = None) -> Path:
    """Return the Path for this run's workspace directory.

    Finds existing dirs via fallback order; returns the namespaced path for
    runs that don't exist yet (so callers can mkdir without extra logic).
    """
    found = _find_run_dir(run_id, profile_name)
    if found:
        return found
    namespace = profile_name or STANDALONE_PROFILE
    return WORKSPACE_ROOT / namespace / run_id


def new_run(profile_name: str | None = None) -> str:
    """Create a new run directory and return the run_id.

    Writes to workspace/<profile_name>/<run_id>/. Pass None for standalone runs.
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir(run_id, profile_name).mkdir(parents=True, exist_ok=True)
    return run_id


def _list_legacy_runs() -> list[str]:
    """Run IDs stored in legacy flat workspace/<run_id>/ layout."""
    if not WORKSPACE_ROOT.exists():
        return []
    return sorted(
        [p.name for p in WORKSPACE_ROOT.iterdir()
         if p.is_dir() and _looks_like_run_id(p.name)],
        reverse=True,
    )


def list_runs(profile_name: str | None = None) -> list[str]:
    """Return run IDs sorted most-recent-first.

    With profile_name: scoped to that profile's subdirectory (plus legacy flat
    runs as a fallback when no namespaced runs exist yet).
    Without: all legacy flat runs (for backward compat / diagnostics).
    """
    if not WORKSPACE_ROOT.exists():
        return []
    if profile_name:
        scope = WORKSPACE_ROOT / profile_name
        if scope.exists():
            runs = sorted(
                [p.name for p in scope.iterdir()
                 if p.is_dir() and not p.name.startswith(".")],
                reverse=True,
            )
            if runs:
                return runs
        # Profile dir is empty or missing — include legacy flat runs.
        return _list_legacy_runs()
    return _list_legacy_runs()


def latest_run(profile_name: str | None = None) -> str | None:
    runs = list_runs(profile_name)
    return runs[0] if runs else None


def resolve_run_id(run_id: str | None, profile_name: str | None = None) -> str:
    """Resolve a run_id string to a concrete, verified run_id.

    - "latest" or None: returns the most recent run for profile_name (or globally).
    - Explicit run_id: finds the run globally (profile switch doesn't break lookup).
    """
    if run_id and run_id != "latest":
        found = _find_run_dir(run_id, profile_name)
        if not found:
            raise FileNotFoundError(f"Run not found: {run_id}")
        return run_id
    latest = latest_run(profile_name)
    if not latest:
        scope = f"profile '{profile_name}'" if profile_name else "workspace"
        raise FileNotFoundError(
            f"No runs found for {scope}. Run `python dit.py find` first."
        )
    return latest


def save_json(run_id: str, filename: str, data, profile_name: str | None = None) -> Path:
    path = run_dir(run_id, profile_name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def save_text(run_id: str, filename: str, text: str, profile_name: str | None = None) -> Path:
    path = run_dir(run_id, profile_name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def load_json(run_id: str, filename: str, profile_name: str | None = None):
    path = run_dir(run_id, profile_name) / filename
    if not path.exists():
        raise FileNotFoundError(f"No {filename} in run {run_id}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(run_id: str, filename: str, profile_name: str | None = None) -> str:
    path = run_dir(run_id, profile_name) / filename
    if not path.exists():
        raise FileNotFoundError(f"No {filename} in run {run_id}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
