# ArmorCode SCM Workspace Reconciler

Identifies SCM workspaces/orgs missing from ArmorCode and auto-creates them.

Supports: **GitHub** (PAT) · **Bitbucket** (Cloud Basic Auth + OnPrem Token/Basic) · **Azure DevOps** (Cloud + OnPrem)

---

## Quick Start

### 1. Create config.json

Copy `config.example.json` and fill in your credentials. See [Config Schema](#config-schema) below.

### 2. Build Docker image

```bash
docker build -t armorcode-scm-reconciler .
```

### 3. Run (Dry-Run — default)

Reports what would be created without making any changes:

```bash
docker run --rm \
  -v /path/to/config.json:/config/config.json \
  -v /path/to/output:/output \
  armorcode-scm-reconciler \
  --armorcode-api-token <your_bearer_token>
```

### 4. Run (Auto-Create)

Actually creates missing installations in ArmorCode:

```bash
docker run --rm \
  -v /path/to/config.json:/config/config.json \
  -v /path/to/output:/output \
  armorcode-scm-reconciler \
  --armorcode-api-token <your_bearer_token> \
  --auto-create
```

### 5. Run locally (without Docker)

```bash
pip install -r requirements.txt

# Dry-run
python -W ignore main.py \
  --config /path/to/config.json \
  --armorcode-api-token <your_bearer_token>

# Auto-create
python -W ignore main.py \
  --config /path/to/config.json \
  --armorcode-api-token <your_bearer_token> \
  --auto-create
```

---

## CLI Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--armorcode-api-token` | Yes* | — | ArmorCode API Bearer token |
| `--config` | No | `/config/config.json` | Path to config.json |
| `--auto-create` | No | `false` | Actually create missing installations. Without this flag, runs in dry-run mode (report only) |

*Can also be set via `ARMORCODE_API_TOKEN` environment variable. CLI argument takes priority.

---

## Volume Mounts

| Mount | Container Path | Purpose |
|-------|---------------|---------|
| Config file | `/config/config.json` | Input — SCM credentials and ArmorCode base URL |
| Output dir | `/output` | Data files — date/time-wise JSON diffs (30-day rolling) |

Logs are written to `/tmp/armorcode/log/` inside the container. To persist logs:

```bash
-v /path/to/logs:/tmp/armorcode/log
```

---

## Config Schema

```json
{
  "armorcode": {
    "base_url": "https://app.armorcode.ai"
  },
  "install_config_defaults": {
    "GITHUB": { ... },
    "BITBUCKET": { ... },
    "AZURE_REPOS": { ... }
  },
  "scm": [ ... ]
}
```

### install_config_defaults (optional)

Defaults are baked into the script. This section lets you override them once for all SCM entries of that type.

| SCM | Built-in Defaults |
|-----|------------------|
| GITHUB | `authType: TOKEN`, `subProductMappingConfig: [INACTIVE, DORMANT]`, `githubIssuesEnabled: false` |
| BITBUCKET | `subProductMappingConfig: [INACTIVE, DORMANT]` |
| AZURE_REPOS | `subProductMappingConfig: [INACTIVE, DORMANT]` |

Per-entry `install_config` overrides merge on top: code defaults → `install_config_defaults` → per-entry.

### SCM Entry Fields

Multiple entries for the same SCM type are supported. Their results are **combined** before diffing against ArmorCode. For example, two Bitbucket OnPrem entries with different hosts will have their projects merged into one set.

| Field | GitHub Cloud | GitHub OnPrem | BB Cloud | BB OnPrem (Token) | BB OnPrem (Basic) | ADO Cloud | ADO OnPrem |
|-------|-------------|--------------|----------|-------------------|-------------------|-----------|------------|
| `type` | `GITHUB` | `GITHUB` | `BITBUCKET` | `BITBUCKET` | `BITBUCKET` | `AZURE_REPOS` | `AZURE_REPOS` |
| `hosting_type` | `Cloud` | `OnPrem` | `Cloud` | `OnPrem` | `OnPrem` | `Cloud` | `OnPrem` |
| `host_url` | — | ✅ | — | ✅ | ✅ | — | ✅ |
| `pat` | ✅ | ✅ | — | — | — | — | — |
| `username` | — | — | ✅ | — | ✅ | — | — |
| `password` | — | — | ✅ | — | ✅ | — | — |
| `token` | — | — | — | ✅ | — | ✅ | ✅ |
| `collection` | — | — | — | — | — | — | ⚪ optional |

> **Azure DevOps OnPrem**: `collection` is optional. If omitted, all collections are **auto-discovered** via the server API. Provide it only to restrict fetching to a specific collection.

Bitbucket OnPrem supports both token and basic auth. If both are provided, **token takes priority**.

`host_url` is optional for Cloud types — implied defaults:
- GitHub → `https://github.com`
- Bitbucket → `https://bitbucket.org`
- Azure → `https://dev.azure.com/`

---

## Modes

### Dry-Run (default)

- Fetches orgs/workspaces from both SCM and ArmorCode
- Computes diff (missing in AC, present in AC but absent from SCM)
- Writes all data to JSON files
- Logs what **would** be created
- **Does NOT create** any installations

### Auto-Create (`--auto-create`)

- Same as dry-run, plus:
- Actually creates missing installations via ArmorCode API
- Handles rate limiting (respects `X-Rate-Limit-Retry-After-Seconds` header)
- Skips already-existing installations (409 responses)
- Calls `PUT /installation/<id>/config` after each create

---

## Output

Data is stored in the mounted `/output` directory with 30-day rolling retention.
Each run gets a timestamp folder so multiple runs per day don't overwrite:

```
/output/
└── data/
    └── YYYY-MM-DD/
        └── HH-MM-SS/
            ├── GITHUB/
            │   ├── ac_orgs.json                       # orgs currently in ArmorCode
            │   ├── scm_orgs.json                      # combined orgs from all config entries
            │   ├── scm_sources.json                   # per-entry breakdown (which host returned what)
            │   ├── missing_in_ac.json                 # orgs to be created
            │   ├── present_in_ac_missing_in_scm.json  # in AC but not in SCM
            │   ├── would_create.json                  # dry-run: what would be created
            │   └── created.json                       # auto-create: what was created
            ├── BITBUCKET/
            └── AZURE_REPOS/
```

Logs:
```
/tmp/armorcode/log/
└── run-YYYY-MM-DD.log
```

Folders older than 30 days are automatically cleaned up on each run.
