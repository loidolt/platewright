"""Locking plates: stage a sliced file, check it, lock it as a version, put it in Bambuddy's library.

The locked file is kept twice: Platewright's own copy under the data directory (it survives a
deleted library file), and the copy Bambuddy prints, in the library folder for production
plates. Its name, PLATE-<code>_<n>up.gcode.3mf, is what sync falls back to when a print can't
be matched by fingerprint.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from platewright.bambuddy.client import Bambuddy, BambuddyError
from platewright.core.plates import PlateCheck, check_plate
from platewright.core.threemf import SlicedPlate, ThreeMFError, read_sliced
from platewright.store.db import PlateRow, Store

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")
MAX_UPLOAD = 200 * 1024 * 1024


@dataclass
class Staged:
    sha256: str
    filename: str
    sliced: SlicedPlate
    check: PlateCheck


class PlateFiles:
    def __init__(self, data_dir: str | Path, store: Store, bambuddy: Bambuddy):
        self.root = Path(data_dir)
        self.uploads = self.root / "uploads"
        self.locked = self.root / "plates"
        self.store = store
        self.bambuddy = bambuddy

    # -- staging ----------------------------------------------------------------------------

    def stage(self, data: bytes, filename: str) -> str:
        """Keep an uploaded file until it is locked or dropped. Returns its sha256."""
        if len(data) > MAX_UPLOAD:
            raise ThreeMFError("The file is larger than 200 MB.")
        sha = hashlib.sha256(data).hexdigest()
        self.uploads.mkdir(parents=True, exist_ok=True)
        (self.uploads / f"{sha}.gcode.3mf").write_bytes(data)
        (self.uploads / f"{sha}.json").write_text(json.dumps({"filename": Path(filename).name}))
        for old in sorted(self.uploads.glob("*.gcode.3mf"), key=lambda p: p.stat().st_mtime)[:-20]:
            old.unlink(missing_ok=True)  # keep the last 20 uploads
            old.with_name(old.name.replace(".gcode.3mf", ".json")).unlink(missing_ok=True)
        return sha

    def staged(self, sha: str, *, units: int | None = None, allow_untested: bool = False,
               allow_unmapped: bool = False) -> Staged:
        if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
            raise ThreeMFError("Unknown upload.")
        path = self.uploads / f"{sha}.gcode.3mf"
        if not path.exists():
            raise ThreeMFError("That upload is gone; drop the file again.")
        meta = json.loads((self.uploads / f"{sha}.json").read_text())
        settings = self.store.settings()
        data = path.read_bytes()
        sliced = read_sliced(io.BytesIO(data), meta["filename"], settings.palette)
        check = check_plate(sliced, self.store.core_models(), settings, units=units, allow_untested=allow_untested,
                            allow_unmapped=allow_unmapped)
        return Staged(sha, meta["filename"], sliced, check)

    # -- locking ----------------------------------------------------------------------------

    async def lock(self, staged: Staged, code: str, *, description: str | None, by: str) -> tuple[int, str | None]:
        """Lock a staged plate as the next version of ``code`` and put it in Bambuddy's library.
        Returns (version id, upload problem or None). A failed upload leaves the plate locked;
        it can be uploaded again from the plate page."""
        code = code.strip().upper()
        if not CODE_RE.match(code):
            raise ValueError("A plate code is 1 to 8 letters or digits, e.g. N or H1")
        if not staged.check.ok:
            raise ValueError("This plate has problems to fix first")
        old = self.store.plate(code)
        version = (max((v.version for v in self.store.plate_versions(code)), default=0)) + 1
        self.locked.mkdir(parents=True, exist_ok=True)
        dest = self.locked / f"PLATE-{code}_v{version}_{staged.check.units}up.gcode.3mf"
        shutil.copyfile(self.uploads / f"{staged.sha256}.gcode.3mf", dest)
        s = staged.sliced
        vid = self.store.register_plate(code, staged.check, sha256=staged.sha256, printer_model=s.printer_model,
                                        printer_model_id=s.printer_model_id, nozzle=s.nozzle, objects=len(s.objects),
                                        file_path=str(dest.relative_to(self.root)), description=description, by=by)
        problem = await self.publish(code)
        if problem is None and old and old.bambuddy_file_id and old.sha256 != staged.sha256:
            # The new file is in place; only now take the old one out of the production folder.
            moved = await self._move_out(old)
            if moved:
                problem = f"the new version is in Bambuddy, but {moved}"
        return vid, problem

    def library_name(self, p: PlateRow) -> str:
        return f"PLATE-{p.code}_{p.units}up.gcode.3mf"

    async def publish(self, code: str) -> str | None:
        """Put the current version's file in the production folder (reusing a copy already there)."""
        p = self.store.plate(code)
        if p is None:
            return "no such plate"
        path = self.root / (self.store.one("SELECT file_path FROM plate_versions WHERE id = ?", p.version_id)[0] or "")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != p.sha256:
            return "Platewright's copy of the file is missing or changed"
        settings = self.store.settings()
        try:
            folder = await self.bambuddy.folder_id(settings.library_folder)
            for f in await self.bambuddy.library_files(folder):
                if f.get("filename") == self.library_name(p):
                    full = await self.bambuddy.library_file(f["id"])
                    if full.get("file_hash") == p.sha256:
                        self.store.set_version_file(p.version_id, int(f["id"]))
                        return None
            res = await self.bambuddy.upload(self.library_name(p), path.read_bytes(), folder)
            self.store.set_version_file(p.version_id, int(res["id"]))
            logger.info("Plate %s v%s uploaded as library file %s", p.code, p.version, res["id"])
            return None
        except BambuddyError as exc:
            logger.warning("Upload of plate %s failed: %s", code, exc)
            return str(exc)

    async def _move_out(self, p: PlateRow) -> str | None:
        """Move a superseded or retired plate's file out of the production folder, so it isn't printed."""
        try:
            folder = await self.bambuddy.folder_id(self.store.settings().retired_folder)
            await self.bambuddy.move_files([p.bambuddy_file_id], folder)
            return None
        except BambuddyError as exc:
            logger.warning("Couldn't move library file %s out: %s", p.bambuddy_file_id, exc)
            return f"move library file {p.bambuddy_file_id} out of the production folder by hand ({exc})"

    async def retire(self, code: str) -> str | None:
        p = self.store.plate(code)
        if p is None:
            return "no such plate"
        self.store.set_plate_active(code, False)
        return await self._move_out(p) if p and p.bambuddy_file_id else None
