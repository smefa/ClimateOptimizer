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
- 🧳 **[Holiday mode](#holiday-mode)** (optional) — automatically turns the
  heat down while you're away, and works out exactly when to start warming
  the house back up so it's warm right as you get home.
- 📊 **[A dashboard card is included](docs/card.png)**, showing what it's
  doing and why.
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
- **Acting on the weather forecast** — starts heating a little harder before a
  cold front, a rising wind or the sun going in actually arrives, so the house
  doesn't have to cool down first. Only ever adds heat, never takes it away.
- **Electricity price savings** — shift heating away from the most expensive
  hours of the day.
- **Where to send the result** — a Home Assistant entity, and/or a direct
  connection to an Ohm on WiFi / Ohmigo device (see above).
- **Local logging** — a detailed log file on your own machine, for anyone who
  wants to dig into the numbers. Nothing is sent anywhere.

---

## Holiday mode

Going away? Holiday mode turns the heat down while you're gone and works
out exactly when the house needs to start warming back up, so it's warm
right as you get home — not an hour too early or too late.

It's switched on separately from the rest of the integration, as four new
entities:

- **Holiday mode** (switch) — turns the whole feature on/off.
- **Holiday start** and **Holiday end** dates.
- **Holiday setback temperature** — what temperature the house should hold
  while you're away.

How it works:

1. At midnight on the start date, the target drops to the setback
   temperature in one sharp step.
2. TrueTemp works out when the ramp back up needs to start based on how
   fast your specific house has historically warmed up — the same
   measurement the rest of the integration is built on — instead of
   guessing a fixed recovery time. That way the house has time to warm up
   without forcing the heat pump's backup heat to kick in.
3. The house is back at your normal target by 15:00 on the end date. If
   there isn't enough time between the two dates for a gentle ramp, it
   starts ramping immediately at the leave date instead, and a status
   entity ("Holiday status") tells you that happened.
4. The setback never drops below the comfort floor you've configured
   (**"Never let it get colder than"**).

There's also a separate, dedicated dashboard card for holiday mode — in
addition to the main card — showing status, dates, and how far the
ramp-up has progressed.

---

## Want the technical details?

The plain-language version above is the whole story for most people. If
you're curious how the learning actually works under the hood, or you're a
developer, see the [technical reference](docs/TECHNICAL.md).

---

## License

MIT — see [LICENSE](LICENSE).
