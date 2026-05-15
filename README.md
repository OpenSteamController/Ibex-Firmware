# IbexFirmware

Archive of every firmware shipped for the Steam Controller (2026) ("Ibex") and the Steam Puck dock ("Proteus"). Firmware is bundled inside the `bins_hardware_all` zip distributed with every Steam client update; this repo tracks that zip across the stable and publicbeta channels and extracts the `.fw` blobs it contains.

## Layout

- `Controller/IBEX_FW_*.fw` — controller firmware blobs.
- `Puck/PROTEUS_FW_*.fw` — Puck dock firmware blobs.
- `index.json` — full catalog with per-firmware metadata (see below).
- `tools/ibex_firmware.py` — the tracker. Run `backfill` once locally; the GitHub Action runs `check` on a schedule.
- `.github/workflows/track-firmware.yml` — the schedule.

## CORS-friendly access via GitHub Pages

Pages is served from the repo root with `Access-Control-Allow-Origin: *`, so browser code can hit the catalog and any firmware blob directly:

- `https://opensteamcontroller.github.io/IbexFirmware/index.json`
- `https://opensteamcontroller.github.io/IbexFirmware/Controller/IBEX_FW_<version>.fw`
- `https://opensteamcontroller.github.io/IbexFirmware/Puck/PROTEUS_FW_<version>.fw`

`.nojekyll` disables Jekyll so `.fw` files are served verbatim.

## Firmware header

Each `.fw` file has a 32-byte header followed by the payload:

| Offset | Type   | Field                                         |
|-------:|--------|-----------------------------------------------|
| `0x00` | bytes  | magic / build identifier (ignored by tracker) |
| `0x04` | LE u32 | payload size in bytes                         |
| `0x08` | LE u32 | CRC32 of payload (zlib polynomial)            |
| `0x0C` | bytes  | zero padding                                  |
| `0x20` | bytes  | payload (`payload_size` bytes)                |

`file_size == 0x20 + payload_size`. The tracker recomputes the CRC and refuses to catalog a file whose header CRC doesn't match.

## `index.json` schema

```jsonc
{
  "generated_at": "ISO-8601 UTC, when index was last written",
  "controller": {
    "IBEX_FW_<HEX>.fw": {
      "version_hex":  "<HEX>",
      "version_unix": <int>,            // version_hex parsed as a Unix timestamp
      "file_size":    <bytes>,
      "payload_size": <bytes>,
      "crc32":        "<8 hex chars>",  // matches header at 0x08
      "sha256":       "<64 hex chars>", // of the full .fw file
      "first_seen": {
        "publicbeta": { "steam_version": "...", "date": "ISO-8601", "commit": "<SteamTracking sha>" },
        "stable":     { "steam_version": "...", "date": "ISO-8601", "commit": "<SteamTracking sha>" }
      },
      "source_zips": ["bins_hardware_all.zip.<hash>", ...]
    }
  },
  "puck": { /* same shape */ },
  "crc32_index": {
    "<crc32 hex>": {
      "path":        "Controller/IBEX_FW_<HEX>.fw",  // or Puck/PROTEUS_FW_*.fw
      "category":    "controller",                    // or "puck"
      "version_hex": "<HEX>"
    }
  },
  "_zips_seen": {
    "bins_hardware_all.zip.<hash>": {
      "first_seen": { "publicbeta": {...}, "stable": {...} },
      "members":    ["IBEX_FW_*.fw", "PROTEUS_FW_*.fw"]
    }
  },
  "_channels": {
    "stable":     { "last_check_commit": "<sha>", "last_check_at": "ISO-8601" },
    "publicbeta": { "last_check_commit": "<sha>", "last_check_at": "ISO-8601" }
  }
}
```

`crc32_index` is a reverse map keyed by payload CRC32. The Steam Controller in bootloader mode only surfaces its firmware's CRC, so this lets clients resolve "running CRC → which version is it" with a single lookup. `first_seen[channel]` is set to the SteamTracking commit at which the firmware's source zip first appeared on that channel. A channel key is absent if that firmware has never shipped on it. `_zips_seen` is the dedupe key the tracker uses to know which zips it has already downloaded; `_channels[ch].last_check_commit` is the resume pointer for the next CI run.

## Source

Manifest discovery uses [`SteamDatabase/SteamTracking`](https://github.com/SteamDatabase/SteamTracking) — the GitHub Action walks every commit since its last successful run on each of:

- `ClientManifest/steam_client_win64` → `stable`
- `ClientManifest/steam_client_publicbeta_win64` → `publicbeta`

so an intermediate firmware shipped between cron ticks is still captured. Zips themselves are downloaded directly from Valve's CDN at `https://cdn.steamstatic.com/client/<zip>`.
