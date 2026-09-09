"""Settings, loaded from the environment or a .env file.

Naming follows nexus-v2 (``AI_BACKEND`` / ``OLLAMA_*`` / ``VLLM_*``) so the two
projects can share a .env and you don't have to remember which spelling belongs
to which repo.

**Three machines, and which is which matters.**

===================  ==========================  ==============================
host                 what it is                  what reldo uses it for
===================  ==========================  ==============================
192.168.1.64         the Windows PC you play on  runs RuneLite + the plugin,
                                                 which POSTs game state to .74
192.168.1.74         the Windows box, "the bot"  runs ``reldo bot`` itself and
                                                 listens for that game state
192.168.1.78         goose, the 5090             Ollama (chat + embeddings) and
                                                 the XTTS speech server
===================  ==========================  ==============================

The consequence worth internalising: **RuneLite and the bot are on different
machines**, so ``live_host`` must be ``0.0.0.0`` on .74 and the port has to be
open through Windows Firewall. Left on loopback it refuses .64 silently -- the
plugin logs a connection error nobody reads and reldo simply never mentions what
you are doing. ``Set-ReldoLiveFirewall`` in ``scripts/install-windows-services.ps1``
opens it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELDO_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    user_agent: str = Field(
        default="",
        description=(
            "Required. The OSRS wiki blocks default agents outright, so an unset "
            "value means every request 403s. Include a contact URL."
        ),
    )
    index_path: Path = Field(default=Path("data/index.npz"))
    requests_per_second: float = Field(default=4.0)

    # -- local model serving -------------------------------------------------
    # Everything runs on your own hardware; there is no hosted API and no key.

    # goose (192.168.1.78) is the 5090 box. Addressed by IP rather than hostname
    # to match nexus-v2's .env and to not depend on local DNS resolving "goose".
    ai_backend: str = Field(default="ollama", description="'ollama' or 'vllm'")

    ollama_api_url: str = Field(default="http://192.168.1.78:11434")
    # Mistral Small 3.2 at Q6_K: ~20 GB against qwen3:32b's 28.6 GB, which leaves
    # real headroom on a 32 GB card. Q6 rather than Q8 on purpose -- Q4_K_M is
    # already ~1% off FP16 and Q6 closes most of that, while Q8 costs 5 GB more
    # for a difference you cannot measure on a task where every fact comes from
    # the wiki text in context rather than from the weights.
    #
    # Tool calling is the thing to check when changing this, not size. Measured
    # on this box: qwen3:32b YES, mistral-small3.2:24b YES, qwen2.5:7b-instruct
    # YES; gemma3:27b and dolphin3:8b are rejected outright by Ollama (HTTP 400),
    # and hermes3:8b and qwen3-coder:30b silently emit their tool call as *text*,
    # producing an agent that never searches and never errors. A community GGUF
    # repack is exactly where a chat template gets broken, so run `reldo doctor`
    # after any change here.
    ollama_model: str = Field(
        default="hf.co/unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF:Q6_K"
    )

    # vLLM on goose was not listening when this was written (8000/8001/8002/8080/
    # 8081 all refused); Ollama on 11434 is the working path. Kept so `AI_BACKEND
    # =vllm` works the moment a server is up.
    vllm_api_url: str = Field(default="http://192.168.1.78:8002/v1")
    vllm_model: str = Field(default="Qwen2.5-32B-Instruct-Q6_K")

    temperature: float = Field(default=0.3)
    max_tokens: int = Field(default=2048)

    # -- embeddings ----------------------------------------------------------
    # 'ollama' runs on the GPU: nomic-embed-text is 768-dim and indexes 35k
    # articles in ~1.6 min at ~370 docs/s. 'local' is CPU-only ONNX (fastembed,
    # 384-dim, ~13 min) and exists so the project still builds without a GPU box.
    # The two produce incompatible vectors -- changing this needs a rebuild.
    embed_backend: str = Field(default="ollama", description="'ollama' or 'local'")
    ollama_embed_model: str = Field(default="nomic-embed-text")
    local_embed_model: str = Field(default="BAAI/bge-small-en-v1.5")

    # -- tracing (den-den-mushi) ---------------------------------------------
    # Point chat traffic at the black-snail proxy and every prompt, response,
    # and tool call lands in the local hub, searchable by agent tag. Empty means
    # off: reldo talks straight to the model server, exactly as before.
    #
    # Set this to the proxy root WITHOUT /v1 (e.g. http://127.0.0.1:8443); the
    # suffix is appended the same way it is for ollama_api_url. denden's own
    # `default_upstream` decides which box the call actually reaches, so it must
    # agree with ollama_api_url below or you will trace calls to the wrong GPU.
    trace_proxy_url: str = Field(default="")
    # Where finished exchanges go, as opposed to trace_proxy_url which captures
    # the chat calls underneath them. Different altitudes and both useful: a
    # proxy sees seventeen completions per answer and no answers.
    #
    # The file is on by default because it is the sink that cannot fail to be
    # readable later -- no auth, no schema, nothing that has to be up at the
    # moment something went wrong. data/ is gitignored, so this never leaves the
    # machine that wrote it.
    trace_path: str = Field(default="data/exchanges.jsonl")
    # Something that accepts POST with a JSON body, e.g. den-den-mushi's ingest.
    trace_ingest_url: str = Field(default="")
    # Tag on every traced call. denden files untagged traffic under "untagged",
    # which is useless the moment a second project points at the same proxy.
    trace_agent: str = Field(default="reldo")
    # Optional: selects a *named* upstream from denden's config instead of its
    # default. Leave empty unless you actually run more than one model box --
    # a name denden doesn't know is a hard 502 on every call, and "default" is
    # NOT a valid name (verified: it means "header absent", not "the default
    # upstream", and sending it literally fails).
    trace_upstream: str = Field(default="")
    # Hub API, used only by `reldo doctor` to confirm traces are landing. This
    # is a different port from the proxy (denden serves the UI on 8765 and
    # proxies on 8443); both defaults match denden's own.
    trace_hub_url: str = Field(default="http://127.0.0.1:8765")

    # Where Discord user -> RuneScape name links are kept. One string per user;
    # a JSON file is the right size of solution.
    accounts_path: Path = Field(default=Path("data/accounts.json"))
    # XP snapshots over time. Written from the hiscores lookup that already
    # happens on every question, so that path costs no extra requests.
    progress_path: Path = Field(default=Path("data/progress.json"))
    # ...but that path alone samples by conversation rather than by time, which
    # made "what have you trained lately" a question about how often you talked
    # to the bot. This polls every linked account on a timer as well. Seconds;
    # 0 disables it and returns to sampling only when somebody asks.
    progress_poll_seconds: float = Field(default=1800.0)

    # -- live game state (RuneLite plugin -> reldo) ---------------------------
    # Off unless enabled. The plugin POSTs here; nothing reaches into the game in
    # the other direction.
    live_enabled: bool = Field(default=False)
    # 0.0.0.0, not loopback: RuneLite runs on .64 and the bot on .74, so the
    # receiver has to accept another machine. Loopback refuses it silently --
    # a connection error in the client log nobody reads, and nothing at all on
    # this side. Narrow it to 127.0.0.1 only if you move them onto one box.
    #
    # This value makes live_token mandatory, and deliberately: 0.0.0.0 is every
    # interface, not merely the LAN one you had in mind.
    live_host: str = Field(default="0.0.0.0")
    live_port: int = Field(default=8099)
    # Must match the plugin's token. Empty disables the check entirely, so it is
    # fine on loopback and refused off it -- `reldo bot` will not start with
    # live_host on 0.0.0.0 and this empty. See reldo.live.serve.
    live_token: str = Field(default="")
    # Where the receiver is, for a process that is NOT the one running it. The
    # three settings above configure the socket this machine opens; this is the
    # address of somebody else's. `reldo coach` runs on the machine you play at
    # while the receiver runs next to the bot, so it is the one command here
    # that has to be told where the rest of the system lives.
    live_url: str = Field(default="", description="e.g. http://192.168.1.74:8099")

    # -- Gielinomics platform -------------------------------------------------
    # The C# side of this repo: TimescaleDB with the price bars and hiscore
    # snapshots the ingest workers have been accumulating. Set this and the GE
    # and hiscores clients read from it instead of from the wiki and Jagex,
    # which is what makes "has the whip been rising" answerable at all -- the
    # upstream APIs keep no history to answer it from.
    #
    # Empty means every client stays pointed upstream, so this file behaves
    # exactly as it did before the platform existed. That is the default on
    # purpose: `reldo` remains usable standalone.
    gielinomics_url: str = Field(default="", description="e.g. http://api:8080")
    # Only needed for the writes: POST /api/players/{name}/track is
    # authenticated because tracking an account adds polling load.
    gielinomics_token: str = Field(default="")
    # Ask the wiki when the platform cannot answer. On by default -- a database
    # that is down should cost you the history, not the price. Turn it off to
    # make a misconfigured URL fail loudly instead of silently going slow.
    gielinomics_fallback: bool = Field(default=True)
    # Enrol accounts the platform has never seen when somebody asks about them.
    # The first answer still comes from Jagex; the point is that next week's can
    # come with history attached. Needs gielinomics_token.
    gielinomics_track: bool = Field(default=True)

    # -- Wise Old Man ---------------------------------------------------------
    # History that predates this bot: weeks and months of gains, plus EHP/EHB,
    # which progress.py cannot know until it has watched for that long.
    wom_enabled: bool = Field(default=True)

    # -- speech (Discord only) -----------------------------------------------
    # A local XTTS server: POST /speak returns a filename, GET /audio/<name>
    # returns the WAV. Empty URL disables every speech path.
    xtts_url: str = Field(default="")
    xtts_voice: str = Field(default="tsunade", description="performer_id; GET /health lists them")

    # -- persona (Discord only) ----------------------------------------------
    # The library, the CLI and the agent stay neutral; this only ever reaches the
    # Discord front end. No characters ship with this repository -- 'plain' is
    # the only persona defined -- so this is a hook rather than a setting with
    # alternatives, and reldo.persona explains why the hook is kept.
    persona: str = Field(default="plain", description="'plain' is the only one defined")
    # Comma-separated channel ids the persona is allowed in. Per-channel rather
    # than global, so a voice added later cannot follow the bot into every
    # general channel of every server it joins.
    #
    # "123,456"            -> the default persona in both
    # "123:plain,456:plain" -> a different persona per room
    persona_channel_ids: str = Field(default="")

    # Only needed for `reldo bot`.
    discord_token: str = Field(default="")
    # Sync slash commands to one guild instead of globally. Strongly recommended:
    # a global sync takes up to an hour to propagate, during which a missing
    # /wiki is indistinguishable from a broken bot. Right-click your server ->
    # Copy Server ID (needs Developer Mode on in Discord's Advanced settings).
    discord_guild_id: int | None = Field(default=None)

    @property
    def persona_channels(self) -> dict[int, str]:
        """Channel id -> character name. Empty name means the default character.

        Accepts bare ids and ``id:name`` pairs in the same list. Unparseable
        entries are dropped rather than raising: one typo in a comma-separated
        list should not stop the bot booting.
        """
        out: dict[int, str] = {}
        for part in self.persona_channel_ids.split(","):
            part = part.strip()
            if not part:
                continue
            channel, _, name = part.partition(":")
            if channel.strip().isdigit():
                out[int(channel.strip())] = name.strip().lower() or self.persona
        return out

    @property
    def chat_base_url(self) -> str:
        """OpenAI-compatible chat root: the trace proxy if set, else the backend.

        Only *chat* is diverted. Embeddings keep using ``ollama_api_url``
        directly and deliberately so -- a wiki rebuild is tens of thousands of
        /api/embed batches, and recording each one buys no diagnostic value
        while burying the handful of traces you actually want to read.
        """
        if self.trace_proxy_url:
            return _with_v1(self.trace_proxy_url)
        if self.ai_backend == "vllm":
            return self.vllm_api_url
        # Ollama exposes the compat layer under /v1, but OLLAMA_API_URL is
        # conventionally the bare host, so append it here rather than making
        # every caller remember.
        return _with_v1(self.ollama_api_url)

    @property
    def chat_model(self) -> str:
        return self.vllm_model if self.ai_backend == "vllm" else self.ollama_model

    @property
    def trace_headers(self) -> dict[str, str]:
        """Routing/tagging headers for den-den-mushi. Empty when tracing is off.

        The proxy strips both before forwarding, so they never reach the model
        server -- but sending them when no proxy is configured would still leak
        two meaningless headers at Ollama, hence the guard.
        """
        if not self.trace_proxy_url:
            return {}
        headers = {"X-DenDen-Agent": self.trace_agent or "reldo"}
        if self.trace_upstream:
            headers["X-DenDen-Upstream"] = self.trace_upstream
        return headers

    def require_user_agent(self) -> str:
        if not self.user_agent.strip():
            raise SystemExit(
                "RELDO_USER_AGENT is not set. The OSRS wiki blocks default agents, so "
                "this is required, not advisory. Set something like:\n"
                '  RELDO_USER_AGENT="reldo/0.1 (github.com/you/reldo)"'
            )
        return self.user_agent


def _with_v1(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def load() -> Settings:
    return Settings()
