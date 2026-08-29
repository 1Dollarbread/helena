# 3D printing (HELENA + Bambu Lab A1)

Three tools, one per stage of the pipeline:

| Tool | What it does | Needs |
|---|---|---|
| `generate_3d_model` | Renders OpenSCAD source (which HELENA writes itself) to an STL | `openscad` on PATH |
| `slice_model` | Headless-slices the STL into a print-ready 3MF | a slicer CLI + a profile bundle |
| `send_to_printer` | Uploads the 3MF over LAN and starts the print | printer in LAN-only + Developer Mode |

Ask HELENA for something like *"model a 40x20x10mm bracket with two 3mm mounting
holes, slice it, and print it"* — it will call the three tools in order,
stopping and asking you before `send_to_printer` actually starts anything.

Everything here is free and keyless — no cloud account, no subscription. The
Bambu Lab tightened third-party cloud access with their Authorization Control
System in 2025; the workaround (and the one used here) is going LAN-only,
which is Bambu's own supported local mode, not a bypass.

## Stage 1 — model generation

Just install OpenSCAD:

```
brew install openscad          # macOS
```

That's it. `generate_3d_model` is a thin wrapper that writes HELENA's OpenSCAD
source to a `.scad` file and runs `openscad -o out.stl in.scad`.

**Scope**: OpenSCAD is constructive solid geometry — primitives (cubes,
cylinders, spheres) combined with unions/differences/extrusions. It's a great
fit for mechanical parts (brackets, enclosures, mounts, gears, hooks, name
tags) and a poor fit for organic/sculptural shapes. Don't expect a detailed
figurine out of this path.

## Stage 2 — slicing

Install a slicer with a CLI. Either works:

- **OrcaSlicer** (open-source Bambu Studio fork, generally has the more
  reliable CLI): https://github.com/SoftFever/OrcaSlicer
- **Bambu Studio**: https://bambulab.com/en/download/studio

Then export a profile bundle once, from the slicer's GUI, for your A1 +
filament of choice:

1. Open the slicer, select your Bambu Lab A1 as the printer and your filament.
2. On the **Printer Settings**, **Filament Settings**, and **Process** tabs,
   use the save/export icon next to the preset dropdown to save each as JSON.
3. Put all three in one folder — `printer.json`, `filament.json`,
   `process.json` (rename on save if needed).

Set in `.env`:

```
HELENA_SLICER_PATH=/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer
HELENA_SLICER_PROFILE_DIR=~/.helena/bambu-a1-profile
```

Slicer CLI flags shift between releases. `slice_model` uses the flags current
as of writing (`--slice 0 --load-settings ... --load-filaments ... --export-3mf
...`); if slicing fails, the tool returns the raw CLI output — check it
against `<slicer> --help` for your installed version and pass corrected flags
via the tool's `extra_args`, or update the command in
`helena_harness/tools/print3d.py` directly.

## Stage 3 — sending to the printer

This is the part that changed most recently. Bambu Lab printers in **Cloud
mode** now block third-party write actions (their Authorization Control
System) — monitoring still works, starting a print doesn't. The supported
way around that is switching the printer to **LAN-only mode with Developer
Mode enabled**:

1. On the A1's touchscreen: **Settings → WLAN → LAN Only Mode** → on.
2. Enabling LAN-only mode also surfaces **Developer Mode** and an **access
   code** on the same screen — turn Developer Mode on and note the code.
3. Note the printer's **IP address** (same screen, or your router's DHCP
   list) and its **serial number** (printer sticker, or Bambu Studio's
   device panel before you switch modes).

Set in `.env`:

```
HELENA_BAMBU_IP=192.168.1.50
HELENA_BAMBU_ACCESS_CODE=12345678
HELENA_BAMBU_SERIAL=00M00A000000000
```

Install the one extra Python dependency this stage needs:

```
pip install -e ".[print3d]"
```

**What you give up in LAN-only mode**: remote monitoring/starting from
outside your network via Bambu Handy, and cloud print history. Camera
LiveView and local control both still work. If you want remote reach back,
put a WireGuard/Tailscale node on the same LAN rather than reverting to
Cloud mode.

**Safety note**: `send_to_printer` always asks for your OK before running,
in every permission mode (including `auto` and `yolo` for `run_command`) —
starting a print consumes real filament and occupies the bed, so this one
tool is deliberately never silent.

### How it talks to the printer

- **File transfer**: implicit FTPS on port 990 (`bblp` / your access code),
  the same channel Bambu Studio itself uses in LAN mode.
- **Print start**: MQTT over TLS on port 8883, publishing a `project_file`
  command to `device/<serial>/request` — again, the same local protocol
  Bambu Studio and the Bambu Handy app use, just driven directly instead of
  through their UI.

No AMS mapping is set by default (`use_ams: false`) since the A1 ships
without one; pass `use_ams: true` if you've added an AMS unit and the
sliced file expects it.
