# LOR Guest Vote & Santa Greeting

A free guest-interaction system for [Light-O-Rama](https://www1.lightorama.com/) Christmas light shows: visitors vote for their favorite song/animation from their own phone, and (optional) get a personalized greeting from Santa. Built entirely on the Python standard library — no paid services required to run the core voting feature.

Bilingual interface (Italian/English) out of the box.

## What it does

- **Song voting**: guests scan a QR code (or type an address) on your guest WiFi, see a welcome page, then vote for the next song/sequence to play. Talks to LOR through its official REST API to queue up the winner automatically.
- **Santa's greeting** *(optional, off by default, still under active development)*: a parent types their child's name and gets a personalized greeting with lights and voice.
- **Charity page** *(optional, off by default)*: a free-form page (text/image/audio) reachable from the guest menu, with an optional "Donate now" button (bring your own Stripe/PayPal link) and a thank-you page.
- **ManagerShow**: a desktop control panel (Tkinter GUI) to configure everything above, manage the song catalog, schedule automatic start/stop, and view voting history/charts.

## Requirements

- Windows + Python 3.x (developed and tested on 3.14)
- [Light-O-Rama Suite](https://www1.lightorama.com/) with its REST API enabled (Control Panel → Settings → Integration) — **tested against LOR Suite 6.2.0.18**. It should keep working on nearby versions since the endpoints used are the ones documented in the official manual, but this hasn't been verified on every version. If you try it on a different version, please report back (see below) — that feedback is genuinely useful to the next person.
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

- Santa's greeting feature is UI/plumbing only right now — it collects a name but doesn't generate the animation/audio yet. It's off by default and won't affect you unless you turn it on.
- Currently distributed as Python source (no standalone .exe yet) — you'll need Python installed.
- **Local guest WiFi only, by design, for now.** Guests need to be on the same network as the show PC. Making the voting page reachable from outside (its own domain, guests voting from anywhere) is a real possibility for later — some commercial tools in this space (e.g. MIIP) already do it, using a lightweight local client that only makes *outbound* calls to their cloud service, never accepting inbound connections. That's the safe pattern we'd follow if/when this gets built — no port forwarding, no exposing LOR's local API to the internet. Anyone happy with local-only voting (as it works today) won't need to change anything when this lands; it'll be opt-in.
- Tested on a single real-world setup so far (see Requirements above) — **compatibility reports from other LOR versions/setups are the single most useful thing right now**, especially before everyone starts installing lights for the season. Please open an Issue and tell us your LOR Suite version and whether voting/queueing worked.

## License

TBD.
