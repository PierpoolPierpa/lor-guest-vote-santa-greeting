# LOR Guest Vote & Santa Greeting

A free guest-interaction system for [Light-O-Rama](https://www1.lightorama.com/) Christmas light shows: visitors vote for their favorite song/animation from their own phone, and (optional) get a personalized greeting from Santa. Built entirely on the Python standard library — no paid services required to run the core voting feature.

Bilingual interface (Italian/English) out of the box.

## What it does

- **Song voting**: guests scan a QR code (or type an address) on your guest WiFi, see a welcome page, then vote for the next song/sequence to play. Talks to LOR through its official REST API to queue up the winner automatically. Three queue modes to choose from (turn-based, persistent, or capped queue) — if you have a use case that needs a different one, open a Discussion and make the case; if we agree it's generally useful we'll build it, and since it's open source you're also welcome to add it yourself.
- **Santa's greeting** *(optional, off by default, still under active development)*: a parent types their child's name and gets a personalized greeting with lights and voice.
- **Charity page** *(optional, off by default)*: a free-form page (text/image/audio) reachable from the guest menu, with an optional "Donate now" button (bring your own Stripe/PayPal link) and a thank-you page.
- **ManagerShow**: a desktop control panel (Tkinter GUI) to configure everything above, manage the song catalog, schedule automatic start/stop, and view voting history/charts.

## Requirements

- Windows + Python 3.x (developed and tested on 3.14)
- [Light-O-Rama Suite](https://www1.lightorama.com/) with its REST API enabled (Control Panel → Settings → Integration) — **tested against LOR Suite 6.2.18**. It should keep working on nearby versions since the endpoints used are the ones documented in the official manual, but this hasn't been verified on every version. If you try it on a different version, please report back (see below) — that feedback is genuinely useful to the next person.
- A guest WiFi network guests can reach from their phones, separate from your main network is recommended.

## Getting started

1. Copy `01_AUTOMAZIONE\CONFIGURAZIONE_SHOW.ini.example` to `01_AUTOMAZIONE\CONFIGURAZIONE_SHOW.ini` and fill in your own values (see the comments in the file). **This file is git-ignored on purpose — it holds your admin password and network settings, never commit it.**
2. Add your songs with `ManagerShow.pyw` (Songs tab → Add).
3. Open the Guide button inside ManagerShow for detailed, in-app documentation of every feature (Scheduling, Welcome page, Charity page, Santa's greeting, etc.) — it's more complete than this README and stays in sync with the code.

## Is this safe to run?

Fair question, worth answering directly instead of just asserting it.

`VotoShow.py` runs a small web server that only listens on your local guest WiFi (not the public internet, unless you explicitly configure your router to forward the port — which we don't recommend and this project doesn't need). A guest's phone only ever sees web pages and can submit a vote or a name through a form — it has **no access to your PC's files, no ability to run commands, nothing beyond what visiting any ordinary website gives someone**. Submitted data is sanitized before use, requests are rate-limited per IP, and the results/admin page is password-protected.

It's built only on Python's standard library (no third-party dependencies pulling in unknown code), and — since this is all open source — you don't have to take our word for it: every line of `VotoShow.py` is right there to read. If you're security-conscious, that's the real answer: go look for yourself, nothing is hidden.

## Status / known limitations

- Santa's greeting collects a name from the guest, but doesn't play anything automatically yet — the on/off switch and the guest-facing pages are done, and a library of 618 pre-generated audio greetings (common Italian names, via a separate offline pipeline) is ready to use, but it isn't wired into the live show flow yet. Off by default and won't affect you unless you turn it on. **We'd genuinely like help from anyone who's tackled something similar in LOR — see below.**
- Currently distributed as Python source (no standalone .exe yet) — you'll need Python installed.
- **Local guest WiFi only, by design, for now.** Guests need to be on the same network as the show PC. Making the voting page reachable from outside (its own domain, guests voting from anywhere) is a real possibility for later — some commercial tools in this space (e.g. MIIP) already do it, using a lightweight local client that only makes *outbound* calls to their cloud service, never accepting inbound connections. That's the safe pattern we'd follow if/when this gets built — no port forwarding, no exposing LOR's local API to the internet. Anyone happy with local-only voting (as it works today) won't need to change anything when this lands; it'll be opt-in.
- Tested on a single real-world setup so far (see Requirements above) — **compatibility reports from other LOR versions/setups are the single most useful thing right now**, especially before everyone starts installing lights for the season. Please open an Issue and tell us your LOR Suite version and whether voting/queueing worked.

## HELP WANTED: Santa's greeting, the LOR side

We're early on this and hitting real walls — if you've solved something similar in LOR, please open a [Discussion](https://github.com/PierpoolPierpa/lor-guest-vote-santa-greeting/discussions) and tell us **specifically what you tried and what worked**, not just "have you considered X" — that's the part we're stuck on. Three concrete open questions:

1. **The names/audio we already generated are Italian-only.** Making this useful outside Italy means redoing that name/audio library per language. If you know a good way to handle multi-language name libraries efficiently (instead of just brute-forcing it per language from scratch), we're listening.
2. **Our current plan is to have all 618 greetings ready as pre-built LOR sequences (audio + timed animation), not hand-programmed one by one in SuperStar or the Sequence Editor.** This is the part we're struggling with most. **If you know a way to script or batch-generate LOR sequences instead of programming each one by hand in SS/SE, that's exactly the kind of tip we need.** As a fallback for names outside the pre-built list, we'd eventually like to generate the sequence (audio + animation) in real time on the fly — if you've done real-time LOR sequence generation before, that's useful too, even if it's a partial answer.
3. **Our current best idea for playback**: play the greeting *on top of* whatever sequence is already running, instead of queueing it to play after — duck the volume of the running sequence, play the greeting audio, show Santa's face animation on a couple of dedicated props (we're using 2 matrices + 1 tree; pick your own), while the rest of the running sequence's channels keep going normally underneath. This avoids waiting for the current sequence to end and gives us more time to work with. **If you've built audio ducking plus a partial-channel overlay on top of a running sequence in LOR before, practical steps are very welcome.**

## Protecting the admin page (results/reset) without HTTPS

The results and reset pages are password-protected (see `Is this safe to run?` above), but that password travels in plain text over your WiFi — no HTTPS. We looked into this in depth and, after weighing it, **currently don't think adding an HTTPS component is worth the extra complexity**, because a much simpler network-level fix already covers it for most setups. Here's the reasoning, so you can judge for yourself and set things up accordingly:

- **If your router supports a separate Guest WiFi** (common even on cheap consumer routers, not just fancy ones): put the show PC on it, and check results / do a reset **only from that same PC** (the "Open results" button in ManagerShow uses `localhost`, so it never touches the WiFi at all). A guest with only the guest-network password cannot decrypt traffic on a different network with a different password — the admin password is never exposed to them, even if they tried to sniff it. If your lights are far from the house and your home WiFi barely reaches out there, an outdoor WiFi antenna with Multi-SSID support (broadcasting two separate networks, each on its own VLAN) is still worth adding even though the security side is already covered — dedicate its second network to yourself, and you get a strong, dedicated signal right by the lights for testing channels with the LOR app, instead of fighting a weak, laggy connection. This project's own outdoor setup uses a TP-Link CPE210 — an inexpensive, weatherproof unit that's worked well for us; not an endorsement or sponsorship, just what we happen to use, and plenty of other brands offer the same Multi-SSID/VLAN feature if you'd rather shop around.
- **If you only have one flat WiFi network** (no guest network option at all): the free fix is discipline — only ever open the results/reset pages from the show PC itself, never from a phone on that WiFi. If your router has a "Client/AP Isolation" toggle, turn it on too — it won't protect the admin password, but it stops guests from poking at each other's devices. Here, the same outdoor Multi-SSID antenna mentioned above does double duty: it's what actually closes the security gap (two genuinely separate networks from one device, no VLANs or managed switches required elsewhere), *and* gives you that same strong dedicated signal for testing near the lights. A cheap second router (~15-20€, even an old spare one) set up as a second independent network works too, if you don't need the outdoor range.

We're open to being wrong about this — if you think HTTPS is worth building anyway (e.g. your setup doesn't fit the above), open a [Discussion](https://github.com/PierpoolPierpa/lor-guest-vote-santa-greeting/discussions) and make the case. Happy to reconsider with real scenarios in hand.

## License

TBD.
