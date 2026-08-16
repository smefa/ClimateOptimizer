# TrueTemp

🇸🇪 [Läs det här på svenska](README.md)

**Your heat pump is guessing what the temperature is outside. TrueTemp
teaches it to guess right — so your home actually reaches the temperature you
asked for.**

---

## The problem

Most heat pumps run on a "weather curve": they look at the outdoor
temperature and calculate how hard to work from a fixed formula. That formula
never checks whether your house is actually warm enough — so if it's a little
off, it stays a little off, all winter, and there's nothing to adjust.

TrueTemp is a free add-on for Home Assistant that fixes this. It
watches your indoor temperature and quietly corrects the outdoor-temperature
number your heat pump sees, so the pump's own logic ends up landing on the
temperature you actually want. It figures this out on its own, from your
house, over a few days — there are no settings to fiddle with.

---

## Features

- 🧠 **Learns on its own** — no numbers to tune, no expert knowledge needed.
- 🎯 **Hits the temperature you asked for**, even when the pump's built-in
  curve doesn't.
- 🔒 **Safe by default** — installs in "watch only" mode and shows you what it
  *would* do before it touches anything. Turn it off any time and the pump
  runs exactly as it did before.
- 💶 **Can save you money** (optional) — shifts heating to cheaper hours if you
  connect an electricity price sensor (e.g. Nord Pool), without you noticing
  the difference.
- ☀️💨 **Accounts for sun and wind** (optional) — for homes where sunlight
  genuinely warms the room, or where draughts are a real issue.
- 📊 **A dashboard card is included**, showing what it's doing and why.
- 🔌 **Works with most heat pumps** — all it needs is a pump that reads an
  outdoor-temperature sensor, which almost all of them do.

---

## Got an older heat pump that can't be smart-controlled?

Some heat pumps have no app, no cloud service, no way to talk to Home
Assistant at all — just a wired outdoor sensor. TrueTemp still needs
some way to hand its corrected number to the pump.

**[Ohm on WiFi Plus](https://www.ohmigo.io/product-page/ohm-on-wifi-plus)
from Ohmigo** solves exactly this. It's a small WiFi device that takes over
your heat pump's existing outdoor-temperature sensor and lets you (or
TrueTemp) decide what temperature the pump should believe it is
outside. If you have an older pump with no smart features of its own, it's
the easiest way to make it controllable — and TrueTemp can talk to it
directly, with nothing else to configure.

---

## Installation

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smefa&repository=TrueTemp-for-Heat-Pumps&category=integration" target="_blank">
  <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.">
</a>

1. Open **HACS** in Home Assistant.
2. Click the three dots in the top-right corner → **Custom repositories**.
3. Add `https://github.com/smefa/TrueTemp-for-Heat-Pumps` with category **Integration**.
4. Search for **TrueTemp** in HACS and install it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for
   **TrueTemp**.

You'll be asked four simple questions:

- **Which sensor measures the room you actually live in?** Avoid a
  basement, a spot in direct sunlight, or somewhere draughty — this is the
  reading everything else is learned from.
- **Which outdoor sensor should it use?** Your own sensor if you have one,
  otherwise a weather service works fine.
- **What temperature do you want indoors?**
- **Radiators, underfloor heating, or both?** This is only used for the
  first few days, until it has measured how your specific house responds.

Finally, connect the new `..._compensated_outdoor_temperature` sensor to your
heat pump's outdoor-temperature input — or let TrueTemp send it there
automatically (see below).

**Nothing is touched until you say so.** After installation, TrueTemp
only watches and calculates — it shows you what it *would* send to your pump
without actually sending it. Turn it on with the TrueTemp switch once
you're happy, and turn it off again at any time with no side effects.

### More settings — all optional

Everything below is optional and can be skipped:

- **Sun and wind compensation** — for homes where these genuinely affect the
  room you're measuring in.
- **Electricity price savings** — shift heating away from the most expensive
  hours of the day.
- **Where to send the result** — a Home Assistant entity, and/or a direct
  connection to an Ohm on WiFi / Ohmigo device (see above).
- **Local logging** — a detailed log file on your own machine, for anyone who
  wants to dig into the numbers. Nothing is sent anywhere.

---

## Want the technical details?

The plain-language version above is the whole story for most people. If
you're curious how the learning actually works under the hood, or you're a
developer, see the [technical reference](docs/TECHNICAL.md).

---

## License

MIT — see [LICENSE](LICENSE).
