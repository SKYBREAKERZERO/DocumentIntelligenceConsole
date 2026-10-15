# DocumentIntelligenceConsole v1.05 — Windows local backend and trusted Wi-Fi WebUI

## Deployment boundary

- **Windows primary workstation** runs the desktop administration tool, PostgreSQL, Qdrant, Ollama, BGE-M3 and the persistent FastAPI Backend.
- Open WebUI remains Docker hosted and published only on `127.0.0.1:3000`. Its OpenAI-compatible connection points to `http://host.docker.internal:18080/v1` with the local API token and **Chat Completions** API type, model `document-intelligence-rag`.
- Backend binds `127.0.0.1:18080`. The LAN wizard **never publishes** Backend `18080`, PostgreSQL `5432`, Qdrant `6333/6334` or Ollama `11434`.
- LAN sharing is **OFF by default**. The wizard creates a Windows portproxy bound to the active, unambiguously identified Wi-Fi RFC1918 IPv4 address on port 3000, forwarding to `127.0.0.1:3000`; adds a Wi-Fi-interface-, IP-, profile- and `LocalSubnet`-scoped Windows inbound firewall rule.
- This is a trusted-office-Wi-Fi connectivity feature, not a public Internet service. Company IT authorization and permission to host on the corporate WLAN are required. AP/client isolation, VLANs and company policy can still block access. A successful local port check never guarantees another computer can reach it.
- **HTTP is plaintext. Use only for short connectivity testing. Do not transmit production confidential documents or real user credentials over LAN HTTP.** Configure trusted internal HTTPS (reverse proxy + trusted certificates) before production multi-user usage. No cloud model or external search is required. Separately restrict outbound traffic and disable Open WebUI's OpenAI cloud URL, Web Search and external tools.
- Open WebUI account login does **not** implement knowledge-level authorization. DocumentIntelligence Backend currently uses a shared local API token; all allowed users query shared scopes. Do not enable sharing where per-project permissions are required until RBAC is enforced in Backend.

## 1. First run: start the GUI and Backend

Prepare your local Python 3.12 environment and install project and API dependencies:

```powershell
cd C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.05_CN_FULL
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

Once the GUI is rendered, it **asynchronously** checks `127.0.0.1:18080/v1/models` using the existing local bearer key. If the Backend isn't running and the port is free, it starts `python -m app.server` as a detached Windows user process. The GUI does not reload BGE-M3 itself. Closing the GUI **does not terminate** the Backend. The launcher fails closed on an unverified port occupant; it does not kill or replace unrelated services. BGE-M3 initialization stays within the Backend process and uses offline model cache only.

Settings -> **External UI / Open WebUI** shows Backend status, launch/repair control and health checks. Startup errors are written under `%LOCALAPPDATA%\DocumentIntelligenceConsole\logs\backend_startup.log`.

If you still want interactive troubleshooting, run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_backend.ps1` manually in a terminal. Do not run two Backends on the same port.

### Windows login auto-start checkbox

The GUI can register or remove **the current user's `HKCU\...\Run` value** with its installed launcher. Enabling it starts **the GUI and then Backend after that user logs in**; it does not start the service before login or after logout. It needs no Administrator privilege, does not alter machine-wide startup, and is reversible from the same checkbox. Keep the extracted project directory at a stable path. If you need server-grade 24/7 operation without a logged-in user, install and validate a separate Windows Service under a dedicated least-privilege identity; this release intentionally does not claim to do so.

If the GUI is launched from a packaged EXE, a separately bundled `DocumentIntelligenceBackend.exe` is required for automatic backend launch. **The v1.05 ZIP contains source, not a prebuilt dual-EXE distribution.** Validate EXE and service packaging separately on Windows.

## 2. Open WebUI model connection

On Open WebUI's **Admin -> Connections**, select **OpenAI-compatible, Chat Completions** (not Responses), URL `http://host.docker.internal:18080/v1`, key from `%LOCALAPPDATA%\DocumentIntelligenceConsole\local_api_token.txt`, model ID `document-intelligence-rag`. Keep cloud OpenAI connections disabled. Access from browser localhost `http://127.0.0.1:3000`.

Read-only endpoints with a Bearer token include `/v1/models`, `/v1/chat/completions`, `/api/v1/retrieval`, `/api/v1/health`. RAG and QA/BUG/version/engineering relationship retrieval uses the pre-existing evidence governance layer; this feature does not directly expose Ollama as the RAG model.

## 3. One-click Wi-Fi sharing wizard

**Prerequisites:**

1. Obtain permission to operate a service on your company WLAN. Check that Open WebUI and Backend work locally and that local accounts/visibility are configured.
2. **Resolve existing data-service exposure first.** The current user's Qdrant was previously mapped on `0.0.0.0:6333/6334`. The wizard refuses to enable LAN sharing if any of `18080`, `5432`, `6333`, `6334`, `11434` listen on a non-loopback address. Use the existing `scripts/qdrant_loopback_rebind.ps1` in dry-run mode, confirm `qdrant_storage` volume, take backups and only then apply its controlled rebind. Never delete `qdrant_storage`.
3. Enable HTTPS for real document traffic. The wizard does **not** install or configure TLS certificates. Its plain HTTP flow is for test only.

In the desktop **Settings -> External UI / Open WebUI -> Current Wi-Fi LAN sharing**, click **One-click configure current Wi-Fi**. Review the warning and explicitly approve the Windows UAC prompt. The operation:

- Identifies one active Wi-Fi adapter and RFC1918 IPv4. If ambiguous, or only public IPv4, it refuses to proceed.
- Checks WebUI localhost 3000 works and all private ports are loopback-only.
- Creates `netsh interface portproxy` entry (`Wi-Fi IPv4:3000` -> `127.0.0.1:3000`) and a firewall rule limited to that Wi-Fi interface, address, profile, and local subnet.
- Does not edit or replace Docker containers, their settings, SQL, Qdrant data or models.
- Stores only nonsecret LAN state in `%ProgramData%\DocumentIntelligenceConsole\lan_share.json`, last error status in `lan_last_result.json`.

Click **Refresh** after UAC completes to see the URL or failure reason. If the Wi-Fi IP changes, run the wizard again; **no background change silently publishes a new address**.

To undo it: click **Disable LAN sharing**, approve UAC; the script deletes only its own portproxy entry/firewall rule and keeps all Docker volumes untouched. It does not remove manually created unrelated rules.

### Safe read-only diagnostics

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\configure_lan_webui.ps1 -Action Status
netsh interface portproxy show v4tov4
docker port open-webui
docker port qdrant
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 3000,18080,5432,6333,6334,11434 } | Select-Object LocalAddress,LocalPort,OwningProcess
```

A colleague on the approved Wi-Fi can then test **the actual Wi-Fi IPv4:3000** from their own browser. If blocked by corporate device isolation or a VLAN ACL, ask IT; do **not** broaden firewall rules or publish backend/model/database ports.

### Existing manually configured LAN portproxy

If you already added a `portproxy` manually for `Wi-Fi IPv4:3000`, the wizard refuses to overwrite an unmanaged listener. Review `netsh interface portproxy show v4tov4` and your existing firewall rule first; clean those up deliberately before adopting tool-managed sharing. The wizard's Disable operation only removes entries it itself recorded.

## 4. Multi-user availability and limits

Backend is a single shared local model runtime. Multiple WebUI accounts can connect but throughput remains GPU/VRAM/CPU dependent. Test two or three users concurrently and measure first-token latency, request queueing, failures, SQL/Qdrant retrieval and overall model throughput. The workstation must remain powered on and **logged in** when using login autostart. Neither this release nor a firewall portproxy guarantees 24/7 uptime, HTTPS encryption, company authorization, or per-project data permissions.

## 5. Verification

Selected automated Python regression tests and source compilation are run in Linux CI-like environment; Windows PowerShell 5.1/UAC, actual GUI, corporate Wi-Fi/AP policies and Open WebUI full end-to-end tests must be verified on the user's Windows machine. See README and CHANGELOG for the delivery status.
