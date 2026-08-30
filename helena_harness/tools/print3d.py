"""3D printing pipeline: text -> OpenSCAD -> STL -> sliced 3MF -> Bambu A1.

Three tools, one per stage, so each can be inspected/retried on its own:

    generate_3d_model   HELENA writes OpenSCAD source itself; this just renders
                         it to an STL with the `openscad` CLI. Good for
                         mechanical/parametric shapes (brackets, boxes, gears,
                         enclosures) — it is not a mesh-generation model, so
                         organic/sculptural asks should be pointed at
                         generate_3d_model with a simpler primitive-based
                         approximation, or turned down as out of scope.
    slice_model         Headless-slices an STL/3MF with a locally installed
                         OrcaSlicer or Bambu Studio CLI binary, using a
                         profile bundle exported once from that slicer's GUI.
    send_to_printer     Pushes the sliced file to a Bambu Lab A1 over the LAN
                         and starts the print, via the community `bambulabs-api`
                         library (FTPS upload + MQTT print-start). Requires the
                         printer in LAN-only + Developer Mode — see
                         extras/print3d/README.md. This one always asks for
                         confirmation regardless of permission mode, since it
                         has a real-world, physical, consumable effect.

All three are keyless and free — no cloud account, no subscription. The
trade a Developer-Mode/LAN-only printer makes is losing Bambu Handy remote
monitoring and cloud print history in exchange for third-party write access;
see the README for specifics.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult, resolve_path, truncate

MAX_OUTPUT = 20_000
SETUP_HINT = "See extras/print3d/README.md for setup."


# --- stage 1: OpenSCAD -> STL ------------------------------------------------


class GenerateModelTool(Tool):
    name = "generate_3d_model"
    description = """
    Render OpenSCAD source to an STL file, ready to slice and print.

    You (the model) write the `scad_code` yourself — this tool only compiles
    it. OpenSCAD is parametric/constructive: cubes, cylinders, spheres,
    extrusions, and boolean unions/differences/intersections of them. It is
    a great fit for mechanical objects (brackets, boxes, enclosures, mounts,
    gears, hooks, name tags) and a poor fit for organic/sculptural shapes —
    don't try to fake those with hundreds of primitives; say so instead and
    suggest a simpler parametric approximation, or that this isn't the right
    tool for it.

    Requires the `openscad` CLI on PATH (or print3d_openscad_path configured).
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "scad_code": {"type": "string", "description": "Valid OpenSCAD source, e.g. 'difference() { cube([40,20,10]); translate([5,5,-1]) cylinder(h=12, r=3); }'"},
            "name": {"type": "string", "description": "Filename stem for the .scad/.stl pair. Defaults to a generated id."},
        },
        "required": ["scad_code"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return (args.get("name") or "model").strip()

    def preview(self, args: dict[str, Any]) -> str:
        name = args.get("name") or "model"
        return f"Render 3D model → {name}.stl"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        scad_code = args.get("scad_code") or ""
        if not scad_code.strip():
            raise ToolError("`scad_code` is required.")

        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", (args.get("name") or f"model-{uuid.uuid4().hex[:6]}")).strip("-") or "model"
        models_dir = ctx.config.print3d_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        scad_path = models_dir / f"{stem}.scad"
        stl_path = models_dir / f"{stem}.stl"
        scad_path.write_text(scad_code, encoding="utf-8")

        binary = ctx.config.print3d_openscad_path
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-o", str(stl_path), str(scad_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                f"Couldn't find the OpenSCAD CLI ({binary!r}). Install it "
                "(brew install openscad on macOS) or set print3d_openscad_path "
                f"to its full path. {SETUP_HINT}"
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError("OpenSCAD render timed out after 120s — the geometry may be too complex.")

        stderr = stderr_b.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not stl_path.exists() or stl_path.stat().st_size == 0:
            raise ToolError(
                f"OpenSCAD failed to produce an STL (exit {proc.returncode}).\n"
                f"{truncate(stderr or stdout_b.decode('utf-8', 'replace'), MAX_OUTPUT, 'openscad output')}"
            )

        size_kb = stl_path.stat().st_size / 1024
        note = f"\nwarnings:\n{stderr}" if stderr else ""
        return ToolResult(
            ok=True,
            content=f"Rendered {stl_path} ({size_kb:.1f} KB).{note}\nNext: slice_model(stl_path=\"{stl_path}\").",
            display=f"{stem}.stl ({size_kb:.0f} KB)",
            meta={"stl_path": str(stl_path), "scad_path": str(scad_path)},
        )


# --- stage 2: slice -----------------------------------------------------


class SliceModelTool(Tool):
    name = "slice_model"
    description = """
    Headless-slice an STL into a print-ready 3MF using a locally installed
    OrcaSlicer or Bambu Studio CLI, plus a one-time-exported profile bundle
    for the Bambu A1 (printer/filament/process presets).

    Needs print3d_slicer_path (the CLI binary) and print3d_slicer_profile_dir
    (a folder holding printer.json, filament.json, process.json — pulled from
    the slicer's own settings folder, e.g. ~/Library/Application
    Support/OrcaSlicer/user/default/{machine,process,filament}/) configured.
    Raises with setup instructions if they aren't.

    Slicer CLI flags shift between versions, and older/newer config files can
    trip an "unknown config type" error even when the files are valid — this
    always passes --allow-newer-file to work around that known OrcaSlicer CLI
    quirk. If slicing still fails, the raw stdout/stderr is returned — read
    it, and adjust extra_args or the profile dir rather than assuming the
    pipeline is broken.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "stl_path": {"type": "string", "description": "Path to the STL/3MF to slice, e.g. from generate_3d_model's output."},
            "extra_args": {"type": "string", "description": "Optional extra CLI flags appended verbatim, for a slicer version whose flags differ from the defaults."},
        },
        "required": ["stl_path"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return Path(args.get("stl_path") or "?").stem

    def preview(self, args: dict[str, Any]) -> str:
        return f"Slice {Path(args.get('stl_path', '?')).name}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cfg = ctx.config
        if not cfg.print3d_slicer_path:
            raise ToolError(f"print3d_slicer_path is not configured. {SETUP_HINT}")
        if not cfg.print3d_slicer_profile_dir:
            raise ToolError(f"print3d_slicer_profile_dir is not configured. {SETUP_HINT}")

        stl_path = resolve_path(ctx, args.get("stl_path") or "", must_exist=True)
        profile_dir = Path(cfg.print3d_slicer_profile_dir).expanduser()
        profiles = {
            "printer": profile_dir / "printer.json",
            "filament": profile_dir / "filament.json",
            "process": profile_dir / "process.json",
        }
        missing = [name for name, p in profiles.items() if not p.exists()]
        if missing:
            raise ToolError(
                f"Missing profile file(s) in {profile_dir}: {', '.join(missing)}.json. {SETUP_HINT}"
            )

        out_dir = cfg.print3d_dir / "sliced"
        out_dir.mkdir(parents=True, exist_ok=True)

        # OrcaSlicer / Bambu Studio CLI syntax as of writing. `--slice 0`
        # slices all plates, `--export-3mf` writes a ready-to-print 3MF
        # (embedded gcode) rather than a bare .gcode. `--allow-newer-file`
        # works around a known OrcaSlicer CLI bug (upstream issues #8155,
        # #4199) where perfectly valid printer/process/filament JSON — even
        # exported straight from the app's own settings folder — gets
        # rejected with "unknown config type of file ... in load-settings".
        # If your installed version renamed these flags, pass extra_args or
        # run `<slicer> --help` and adjust here.
        cmd = [
            cfg.print3d_slicer_path,
            "--slice", "0",
            "--allow-newer-file",
            "--load-settings", f"{profiles['printer']};{profiles['process']}",
            "--load-filaments", str(profiles["filament"]),
            "--export-3mf", str(out_dir / f"{stl_path.stem}.3mf"),
            str(stl_path),
        ]
        if args.get("extra_args"):
            cmd.extend(args["extra_args"].split())

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ToolError(f"Couldn't find the slicer CLI at {cfg.print3d_slicer_path!r}.") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError("Slicing timed out after 180s.")

        output_3mf = out_dir / f"{stl_path.stem}.3mf"
        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")
        body = truncate("\n".join(p for p in (stdout, stderr) if p.strip()) or "(no output)", MAX_OUTPUT, "slicer output")

        if proc.returncode != 0 or not output_3mf.exists():
            raise ToolError(
                f"Slicing failed (exit {proc.returncode}). Command: {' '.join(cmd)}\n{body}"
            )

        return ToolResult(
            ok=True,
            content=f"Sliced → {output_3mf}\n{body}\nNext: send_to_printer(file_path=\"{output_3mf}\").",
            display=f"{output_3mf.name}",
            meta={"output_path": str(output_3mf)},
        )


# --- stage 3: push to the A1 over LAN and start ------------------------------


def _send_via_bambulabs_api(
    cfg, local_path: Path, remote_name: str, *, plate: int, use_ams: bool, flow_cali: bool
) -> None:
    try:
        import bambulabs_api as bl
    except ImportError as exc:
        raise ToolError(
            "bambulabs-api is not installed. Run: pip install -e \".[print3d]\""
        ) from exc

    # This community library (github.com/BambuTools/bambulabs_api) already
    # handles the implicit-FTPS quirks (TLS SNI on the data channel, passive
    # vs. active mode, and whatever else varies across firmware versions)
    # that a hand-rolled ftplib client kept tripping over — it's used
    # against real A1/A1-mini/P1/X1 hardware by a fairly large user base, so
    # it's a better bet than continuing to debug our own implementation
    # blind against hardware we can't directly test.
    printer = bl.Printer(cfg.bambu_ip, cfg.bambu_access_code, cfg.bambu_serial)
    try:
        printer.connect()
        time.sleep(2)  # give the MQTT/FTP clients a moment to actually establish

        try:
            with local_path.open("rb") as fh:
                printer.upload_file(fh, remote_name)
        except Exception as exc:  # noqa: BLE001 - surface whatever the library raises plainly
            raise ToolError(
                f"Upload to {cfg.bambu_ip} failed ({exc}). Confirm bambu_access_code matches "
                "what's currently shown on the printer's screen right now, and that LAN-only + "
                "Developer Mode are both on."
            ) from exc

        started = printer.start_print(
            filename=remote_name,
            plate_number=plate,
            use_ams=use_ams,
            ams_mapping=[0],
            flow_calibration=flow_cali,
        )
        if not started:
            raise ToolError("The printer rejected the print-start command.")
    finally:
        printer.disconnect()


class SendToPrinterTool(Tool):
    name = "send_to_printer"
    description = """
    Push a sliced 3MF/gcode file to a Bambu Lab A1 over the local network and
    start printing it — no cloud account involved.

    Requires: the printer switched to LAN-only mode with Developer Mode
    enabled (shows an access code on its screen), and bambu_ip,
    bambu_access_code, bambu_serial configured. Refuses with setup
    instructions if any of those are missing.

    This has a real, physical, consumable effect — filament gets used and the
    bed gets occupied for the print's duration — so it always asks for
    confirmation before running, regardless of permission mode.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the sliced .3mf, e.g. from slice_model's output."},
            "plate": {"type": "integer", "description": "Plate number to print. Default 1."},
            "flow_calibration": {"type": "boolean", "description": "Run automatic flow calibration before printing. Default true."},
            "use_ams": {"type": "boolean", "description": "True if this print uses an AMS filament unit. Default false — the A1 ships without one."},
        },
        "required": ["file_path"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return f"printer:{args.get('file_path', '?')}"

    def preview(self, args: dict[str, Any]) -> str:
        return f"Send {Path(args.get('file_path', '?')).name} to the printer and start it"

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return (
            f"printer: {ctx.config.bambu_ip or '(not configured)'}\n"
            f"file: {args.get('file_path', '?')}\n"
            "note: this will actually start printing and consume filament."
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cfg = ctx.config
        missing = [
            name for name, val in (
                ("bambu_ip", cfg.bambu_ip),
                ("bambu_access_code", cfg.bambu_access_code),
                ("bambu_serial", cfg.bambu_serial),
            ) if not val
        ]
        if missing:
            raise ToolError(f"Not configured: {', '.join(missing)}. {SETUP_HINT}")
        if not cfg.bambu_lan_only:
            raise ToolError(
                "bambu_lan_only is set to false — this tool only supports LAN-only + "
                f"Developer Mode printers. {SETUP_HINT}"
            )

        file_path = resolve_path(ctx, args.get("file_path") or "", must_exist=True)
        plate = int(args.get("plate") or 1)
        flow_cali = bool(args.get("flow_calibration", True))
        use_ams = bool(args.get("use_ams", False))
        remote_name = file_path.name

        try:
            await asyncio.to_thread(
                _send_via_bambulabs_api, cfg, file_path, remote_name,
                plate=plate, use_ams=use_ams, flow_cali=flow_cali,
            )
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Sending to the printer failed unexpectedly: {exc}") from exc

        return ToolResult(
            ok=True,
            content=(
                f"Uploaded {remote_name} to {cfg.bambu_ip} and sent the print-start command "
                f"(plate {plate}). Watch the printer's screen or Bambu Studio's device tab for progress."
            ),
            display=f"printing {remote_name}",
            meta={"file": remote_name, "plate": plate},
        )
