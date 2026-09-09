"""A window instead of a terminal.

The coach worked and lived in a shell, which is a strange place for something
whose whole point is that it interrupts you while you are playing a game. This
serves one page on loopback: her answers, her unprompted remarks, and your live
stats, in a layout that borrows from the interface already on the other monitor.

**The design is the game's, not a chat app's.** The accent is purple because
that is what the game announces a rare drop in, and "a purple" is what players
call one -- so it is the colour already reserved for *something worth looking up
just happened*, which is precisely when she speaks. Level-ups stay gold, because
the game's level-up text is gold and recolouring it would be altering a
quotation. Every glyph carries the hard one-pixel black shadow that is the real
signature of RuneScape text, far more than any typeface would be. The skills grid is the in-game Stats tab, driven by the XP your plugin
is already posting, because the thing she is reasoning from should be the thing
you can see.

Everything is inline. No CDN, no build step, no fonts to fetch: this runs on a
machine that is playing a game, and a UI that needs the network to render is a UI
that is blank exactly when the network is the thing that broke.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aiohttp import web

log = logging.getLogger(__name__)

DEFAULT_PORT = 8100

# Audio is held for one page-load's worth of playback and then dropped. Keeping
# it longer would mean a growing pile of WAVs for a page nobody has open.
MAX_CLIPS = 24

UNLOCK = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reldo</title>
<style>
  html, body { height: 100%; margin: 0; background: #1a1222; color: #d9cfe8;
    font-family: "Lucida Sans Unicode", "Lucida Grande", Verdana, sans-serif;
    -webkit-font-smoothing: none; text-shadow: 1px 1px 0 #000;
    display: grid; place-items: center; }
  form { background: #2e2438; border: 2px solid #1a1222; border-top-color: #4c3a63;
    border-left-color: #4c3a63; padding: 22px; width: min(420px, 90vw); }
  h1 { margin: 0 0 4px; font-size: 19px; color: #c15bff; letter-spacing: .04em; }
  p { margin: 0 0 16px; font-size: 13px; opacity: .75; line-height: 1.5; }
  .err { color: #ff6a6a; opacity: 1; }
  input { width: 100%; background: #0b0710; border: 2px solid #0d0912;
    border-bottom-color: #1a1222; border-right-color: #1a1222; color: #f2ecff;
    font: inherit; text-shadow: inherit; padding: 9px 10px; margin-bottom: 12px; }
  button { background: #4c3a63; border: 2px solid #1a1222; border-top-color: #6b5390;
    border-left-color: #6b5390; color: #d9cfe8; font: inherit; text-shadow: inherit;
    padding: 9px 20px; cursor: pointer; width: 100%; }
  button:hover { background: #5d4878; }
  :focus-visible { outline: 2px solid #c15bff; outline-offset: 1px; }
</style>
</head>
<body>
<form method="post" action="/unlock">
  <h1>Reldo</h1>
  <p class="err"><!--ERR--></p>
  <p>Paste the shared token. It is RELDO_LIVE_TOKEN on the box running the bot,
     the same one the RuneLite plugin uses.</p>
  <input name="token" type="password" autofocus autocomplete="off"
         placeholder="Shared token" aria-label="Shared token">
  <button>Unlock</button>
</form>
</body>
</html>
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reldo</title>
<style>
  /* Purple, grounded in the one thing the game already uses it for: a rare
     drop is announced in purple, and "getting a purple" is what players call
     it. So the accent is the colour the game reaches for when something worth
     looking up has just happened -- which is exactly when she speaks.

     Gold stays for level-ups because the game's level-up text is gold and
     changing it would be changing a quotation. Everything else is retuned from
     the interface browns to the same interface at Zamorak's end of the
     spectrum: still beveled, still flat, never soft. */
  :root {
    --stone:        #2e2438;
    --stone-dark:   #1a1222;
    --stone-light:  #4c3a63;
    --parchment:    #d9cfe8;
    --ink:          #1a1222;
    --chat:         #0b0710;
    --purple:       #c15bff;
    --gold:         #ffb000;
    --cyan:         #7fd6ff;
    --green:        #57e389;
    --said:         #f2ecff;
    --you:          #9fd8ff;
  }

  * { box-sizing: border-box; }

  html, body {
    height: 100%;
    margin: 0;
    background: var(--stone-dark);
    color: var(--parchment);
    /* Not a webfont: nothing here may need the network to render. The
       recognisable part of RuneScape text is not the face anyway -- it is the
       hard black shadow under every glyph and the absence of antialiasing. */
    font-family: "Lucida Sans Unicode", "Lucida Grande", Verdana, sans-serif;
    font-size: 15px;
    -webkit-font-smoothing: none;
    text-shadow: 1px 1px 0 #000;
  }

  /* The frame. Two flat bevels rather than a gradient or a shadow, because the
     client's panels are beveled and never soft. */
  .panel {
    background: var(--stone);
    border: 2px solid var(--stone-dark);
    border-top-color: var(--stone-light);
    border-left-color: var(--stone-light);
  }

  #app {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 320px;
    grid-template-rows: auto minmax(0, 1fr) auto;
    gap: 10px;
    height: 100%;
    padding: 10px;
  }

  header {
    grid-column: 1 / -1;
    display: flex;
    align-items: baseline;
    gap: 14px;
    padding: 8px 12px;
  }
  header h1 { margin: 0; font-size: 19px; color: var(--purple); letter-spacing: .04em; }
  header .who { color: var(--parchment); opacity: .75; }
  header .state { margin-left: auto; font-size: 13px; }
  .dot { display: inline-block; width: 8px; height: 8px; margin-right: 6px; background: #5a2040; }
  .dot.live { background: var(--green); }

  /* The chatbox: true black, because the game's is. */
  #log {
    background: var(--chat);
    border: 2px solid var(--stone-dark);
    border-top-color: var(--stone-light);
    border-left-color: var(--stone-light);
    overflow-y: auto;
    padding: 10px 12px;
    line-height: 1.5;
  }
  .line { margin: 0 0 7px; white-space: pre-wrap; word-wrap: break-word; }
  .line .from { color: var(--purple); }
  .line.you .from { color: var(--you); }
  .line.you { color: var(--you); }
  .line.said { color: var(--said); }
  /* Unprompted remarks arrive looking like the game talking to you, because
     that is what they are: something the client noticed, not a reply. */
  .line.unprompted { color: var(--gold); }
  .line.system { color: var(--cyan); }
  .line.error { color: #ff6a6a; }

  /* Signature: the Stats tab. Real XP from the plugin, laid out the way the
     game lays it out -- three columns, level over total, sorted as in-game. */
  aside { padding: 10px; overflow-y: auto; }
  aside h2 {
    margin: 0 0 8px; font-size: 13px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--parchment); opacity: .7;
  }
  #skills { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
  .skill {
    background: var(--stone-dark);
    border: 1px solid #120c1a;
    padding: 4px 6px;
    font-size: 12px;
    line-height: 1.25;
  }
  .skill b { color: var(--purple); font-weight: normal; }
  .skill .name { display: block; opacity: .65; font-size: 11px; }
  .skill.moving { border-color: var(--green); }
  .skill.moving b { color: var(--green); }

  #session { margin-top: 12px; font-size: 12px; line-height: 1.6; opacity: .85; }
  #session .rate { color: var(--green); }

  form { grid-column: 1 / -1; display: flex; gap: 8px; padding: 8px; }
  input[type=text] {
    flex: 1; min-width: 0;
    background: var(--chat);
    border: 2px solid var(--stone-dark);
    border-top-color: #0d0912; border-left-color: #0d0912;
    color: var(--said);
    font: inherit; text-shadow: inherit;
    padding: 8px 10px;
  }
  input[type=text]::placeholder { color: #6d5f80; }
  button {
    background: var(--stone-light);
    border: 2px solid var(--stone-dark);
    border-top-color: #6b5390; border-left-color: #6b5390;
    color: var(--parchment); font: inherit; text-shadow: inherit;
    padding: 8px 18px; cursor: pointer;
  }
  button:hover:not(:disabled) { background: #5d4878; }
  button:disabled { opacity: .5; cursor: default; }
  :focus-visible { outline: 2px solid var(--purple); outline-offset: 1px; }

  .thinking::after {
    content: "";
    animation: dots 1.2s steps(4, end) infinite;
  }
  @keyframes dots { 0% { content: ""; } 25% { content: "."; } 50% { content: ".."; } 75% { content: "..."; } }
  @media (prefers-reduced-motion: reduce) { .thinking::after { animation: none; content: "..."; } }

  @media (max-width: 760px) {
    #app { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0,1fr) auto auto; }
    aside { max-height: 220px; }
  }
</style>
</head>
<body>
<div id="app">
  <header class="panel">
    <h1>Reldo</h1>
    <span class="who" id="who">—</span>
    <span class="state"><span class="dot" id="dot"></span><span id="status">connecting</span></span>
  </header>

  <div id="log" role="log" aria-live="polite"></div>

  <aside class="panel">
    <h2>Stats</h2>
    <div id="skills"></div>
    <div id="session"></div>
  </aside>

  <form id="ask" class="panel">
    <input type="text" id="q" autocomplete="off" placeholder="Ask her something" aria-label="Ask her something">
    <button id="send">Ask</button>
  </form>
</div>

<script>
const log = document.getElementById('log');
const skillsEl = document.getElementById('skills');
const sessionEl = document.getElementById('session');
const form = document.getElementById('ask');
const q = document.getElementById('q');
const send = document.getElementById('send');
const dot = document.getElementById('dot');
const status = document.getElementById('status');
const who = document.getElementById('who');

// In-game order, so the grid reads the way the Stats tab does rather than
// alphabetically. A skill the server does not send is simply absent.
const ORDER = ["Attack","Hitpoints","Mining","Strength","Agility","Smithing",
  "Defence","Herblore","Fishing","Ranged","Thieving","Cooking","Prayer",
  "Crafting","Firemaking","Magic","Fletching","Woodcutting","Runecraft",
  "Slayer","Farming","Construction","Hunter","Sailing"];

let previous = {};

function line(text, cls, from) {
  const p = document.createElement('p');
  p.className = 'line ' + (cls || '');
  if (from) {
    const b = document.createElement('span');
    b.className = 'from';
    b.textContent = from + ': ';
    p.appendChild(b);
  }
  p.appendChild(document.createTextNode(text));
  log.appendChild(p);
  log.scrollTop = log.scrollHeight;
  return p;
}

let playing = null;

function play(id) {
  if (!id) return;
  // Two rules, both from listening to it go wrong.
  //
  // Wait for canplaythrough rather than calling play() straight away: the
  // element will happily start on the first decoded frames and catch up, and
  // what that sounds like at the front of a sentence is breathing before the
  // voice arrives. Measured synthesis starts at -12 dB in the first 50 ms, so
  // there is no quiet lead-in to blame -- the gap is decode, not audio.
  //
  // And only one clip at a time: a proactive remark can land while an answer is
  // still speaking, and two of her at once is not a feature.
  const audio = new Audio(auth('/api/audio/' + id));
  audio.preload = 'auto';
  audio.addEventListener('canplaythrough', () => {
    if (playing && playing !== audio) { playing.pause(); }
    playing = audio;
    audio.play().catch(() => {
      // Autoplay refused until the page has been interacted with. Not worth an
      // error line: asking anything at all satisfies it.
    });
  }, {once: true});
  audio.addEventListener('ended', () => { if (playing === audio) playing = null; });
  audio.load();
}

function stats(live) {
  const skills = live.skills || {};
  if (Object.keys(skills).length) {
    skillsEl.replaceChildren();
    for (const name of ORDER) {
      if (!(name in skills)) continue;
      const xp = skills[name];
      const div = document.createElement('div');
      div.className = 'skill' + (previous[name] !== undefined && xp > previous[name] ? ' moving' : '');
      div.innerHTML = '<span class="name"></span><b></b>';
      div.querySelector('.name').textContent = name;
      div.querySelector('b').textContent = (live.levels && live.levels[name]) ?? '';
      div.title = name + ' — ' + xp.toLocaleString() + ' xp';
      skillsEl.appendChild(div);
    }
    previous = skills;
  }
  const s = live.session;
  if (s) {
    const gains = Object.entries(s.gains || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
    sessionEl.replaceChildren();
    const head = document.createElement('div');
    head.textContent = 'This session: ' + s.minutes + ' min';
    sessionEl.appendChild(head);
    for (const [name, amount] of gains) {
      const row = document.createElement('div');
      const rate = (s.rates || {})[name];
      row.innerHTML = '<span class="rate"></span>';
      row.querySelector('.rate').textContent =
        name + ' +' + amount.toLocaleString() + (rate ? '  (' + rate.toLocaleString() + '/hr)' : '');
      sessionEl.appendChild(row);
    }
  }
}

const TOKEN = new URLSearchParams(location.search).get('t');
if (TOKEN) {
  // The server has set a cookie by now, so the secret does not need to stay in
  // the address bar, in history, or in whatever gets pasted into chat next.
  history.replaceState(null, '', location.pathname);
}
const auth = (path) => TOKEN ? path + (path.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN) : path;
const events = new EventSource(auth('/api/events'));
events.onopen = () => { dot.classList.add('live'); status.textContent = 'watching'; };
events.onerror = () => { dot.classList.remove('live'); status.textContent = 'receiver unreachable'; };
events.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.kind === 'state') {
    who.textContent = msg.player + (msg.fresh ? '' : ' — not logged in');
    stats(msg);
  } else if (msg.kind === 'remark') {
    line(msg.text, 'unprompted', msg.persona);
    play(msg.audio);
  } else if (msg.kind === 'note') {
    line(msg.text, 'system');
  }
};

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = q.value.trim();
  if (!question) return;
  q.value = '';
  send.disabled = true;
  line(question, 'you', 'You');
  const waiting = line('reading the wiki', 'system thinking');
  try {
    const res = await fetch(auth('/api/ask'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question}),
    });
    const data = await res.json();
    waiting.remove();
    if (data.error) {
      line(data.error, 'error');
    } else {
      line(data.text, 'said', data.persona);
      play(data.audio);
    }
  } catch (err) {
    waiting.remove();
    line('Could not reach reldo: ' + err.message, 'error');
  } finally {
    send.disabled = false;
    q.focus();
  }
});
q.focus();
</script>
</body>
</html>
"""


def build_app(
    *, answer, live_snapshot, events_queue, clips: dict, token: str = ""
) -> web.Application:
    """The page, the ask endpoint, the event stream and the audio.

    Args:
        answer: ``async (question) -> dict`` with text, persona and audio id.
        live_snapshot: ``async () -> dict`` current live state for the stats grid.
        events_queue: an ``asyncio.Queue`` the coach pushes remarks onto.
        clips: id -> WAV bytes, shared with whatever produced the audio.
    """

    COOKIE = "reldo_token"

    def allowed(request: web.Request) -> bool:
        # Query string OR cookie. The query string is how the token arrives the
        # first time, because EventSource cannot set a header and a UI whose
        # live stream is the one unauthenticated route is not an authenticated
        # UI. The cookie is how it stops having to: a 32-character secret that
        # has to survive being retyped into an address bar is a secret that gets
        # mistyped, which is what happened -- one dropped character, and a 401
        # that looks identical to the service being down.
        if not token:
            return True
        return token in (request.query.get("t"), request.cookies.get(COOKIE))

    async def unlock(request: web.Request) -> web.Response:
        """Take the token from a form field instead of the address bar.

        Editing a 32-character secret into a URL failed twice in a row, both
        times by dropping the last character, and a truncated token 401s
        identically to the service being down. A field you can paste into whole,
        that says plainly when it is wrong, is the difference.
        """
        data = await request.post()
        given = str(data.get("token") or "").strip()
        if token and given != token:
            return web.Response(
                text=UNLOCK.replace("<!--ERR-->", "That token is not right."),
                status=401,
                content_type="text/html",
            )
        response = web.HTTPFound("/")
        response.set_cookie(COOKIE, token, httponly=True, samesite="Lax", path="/")
        raise response

    async def page(request: web.Request) -> web.Response:
        if not allowed(request):
            wrong = "That token is not right." if request.query.get("t") else ""
            return web.Response(
                text=UNLOCK.replace("<!--ERR-->", wrong),
                status=401,
                content_type="text/html",
            )
        response = web.Response(text=PAGE, content_type="text/html")
        if token and request.query.get("t") == token:
            # Session cookie, so it lasts as long as the browser is open and
            # leaves nothing behind on a shared machine.
            response.set_cookie(
                COOKIE, token, httponly=True, samesite="Lax", path="/"
            )
        return response

    async def ask(request: web.Request) -> web.Response:
        if not allowed(request):
            return web.json_response({"error": "bad token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "malformed request"}, status=400)
        question = str((payload or {}).get("question") or "").strip()
        if not question:
            return web.json_response({"error": "Ask her something first."}, status=400)
        try:
            return web.json_response(await answer(question))
        except Exception as exc:  # noqa: BLE001 - surfaced to the page, not swallowed
            log.exception("Answering failed")
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"})

    async def audio(request: web.Request) -> web.Response:
        if not allowed(request):
            raise web.HTTPUnauthorized()
        clip = clips.get(request.match_info["clip"])
        if clip is None:
            raise web.HTTPNotFound()
        return web.Response(body=clip, content_type="audio/wav")

    async def events(request: web.Request) -> web.StreamResponse:
        if not allowed(request):
            raise web.HTTPUnauthorized()
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                # Without this a proxy will hold the stream until it has enough
                # bytes to be worth forwarding, which for one JSON line a poll
                # is never.
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        # Immediately, so the page is not blank for a whole poll interval.
        await _send(response, {"kind": "state", **(await live_snapshot())})
        while True:
            try:
                message = await asyncio.wait_for(events_queue.get(), timeout=15.0)
            except TimeoutError:
                # A comment frame keeps the connection from being reaped and
                # tells the page nothing, which is correct: nothing happened.
                await response.write(b": keepalive\n\n")
                continue
            except (ConnectionResetError, asyncio.CancelledError):
                return response
            try:
                await _send(response, message)
            except (ConnectionResetError, RuntimeError):
                return response

    app = web.Application()
    app.add_routes([
        web.get("/", page),
        web.post("/unlock", unlock),
        web.post("/api/ask", ask),
        web.get("/api/events", events),
        web.get("/api/audio/{clip}", audio),
    ])
    return app


async def _send(response: web.StreamResponse, message: dict) -> None:
    import json

    await response.write(f"data: {json.dumps(message)}\n\n".encode())


def keep(clips: dict, audio: bytes | None) -> str | None:
    """Hold a clip for the page to fetch, dropping the oldest past the cap."""
    if not audio:
        return None
    clip_id = uuid.uuid4().hex
    clips[clip_id] = audio
    while len(clips) > MAX_CLIPS:
        clips.pop(next(iter(clips)))
    return clip_id


def levels_for(skills: dict[str, int]) -> dict[str, int]:
    """Levels beside the XP, so the grid can show what the game shows."""
    from .skills import level_at_xp

    return {name: level_at_xp(xp) for name, xp in skills.items()}


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


async def serve(
    app: web.Application,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str = "",
):
    """Start the UI. Loopback by default: this is a window, not a service.

    Raises:
        ValueError: asked to listen off loopback with no token. The same refusal
            :func:`reldo.live.serve` makes, and for a stronger reason -- that one
            accepts state, this one answers questions, reads your stats aloud and
            will happily do it for anyone who can reach the port.
    """
    if host not in LOOPBACK and not token.strip():
        raise ValueError(
            f"Refusing to serve the UI on {host!r} with no token. That is every "
            "interface on this machine. Set RELDO_LIVE_TOKEN (it is already the "
            "shared secret for the receiver) or use --host 127.0.0.1."
        )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Reldo is at http://%s:%d", host, port)
    return runner
