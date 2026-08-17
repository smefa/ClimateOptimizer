# TrueTemp

🇬🇧 [Read this in English](README.en.md)

**Din värmepump styrs ofta av utomhustemperaturen vilket inte återspeglar behovet inne. TrueTemp lär den att
välja rätt — så att huset faktiskt håller den temperatur du har bett om.**

---

## Problemet

De flesta värmepumpar styrs av en "värmekurva": de läser av utetemperaturen
och räknar ut hur hårt de ska jobba enligt en fast formel. Den formeln kollar
aldrig om huset faktiskt är tillräckligt varmt — så är den lite fel, förblir
den lite fel hela vintern, utan att det finns något att justera.

TrueTemp är ett gratis tillägg till Home Assistant som löser det. Det
tittar på din faktiska innetemperatur och justerar tyst och stilla den
utetemperatur som din värmepump tror på, så att pumpens egen logik landar på
temperaturen du faktiskt vill ha. Det här räknar tillägget ut helt själv,
utifrån ditt eget hus, på några dagar — det finns inga inställningar att
skruva på.

---

## Funktioner

- 🧠 **Lär sig helt automatiskt** — inga värden att ställa in, ingen
  expertkunskap krävs.
- 🎯 **Ger temperaturen du bett om**, även när värmepumpens egen kurva
  inte gör det.
- 🔒 **Säkert som standard** — installeras i ett rent "titta men inte röra"-
  läge och visar vad den *skulle* göra innan den gör något alls. Stäng av den
  när du vill, så kör pumpen precis som förut.
- 💶 **Kan sänka elräkningen** (valfritt) — .flyttar värmen till billigare
  timmar om du kopplar in en elprissensor (t.ex. Nord Pool), utan att du
  märker någon skillnad i huset. Som standard sänker den också värmen om det inte behövs och du sparar även på det.
- ☀️💨 **Tar hänsyn till sol och vind** (valfritt) — för hus där solen
  faktiskt värmer rummet, eller där drag är ett riktigt problem.
- 📊 **[Egen kontrollpanel](docs/card.png)** — ett färdigt kort som visar vad
  tillägget gör och varför.
- 🔌 **Fungerar med de flesta värmepumpar** — allt som krävs är en pump som
  läser av en utegivare, vilket nästan alla gör.

---

## Har du en äldre värmepump som inte går att styra smart?

Vissa värmepumpar saknar app, molntjänst och all form av koppling mot Home
Assistant — bara en hårt inkopplad utegivare. TrueTemp behöver
ändå ett sätt att lämna sitt uträknade värde till pumpen.

**[Ohm on WiFi Plus](https://www.ohmigo.io/product-page/ohm-on-wifi-plus)
från Ohmigo** löser precis det. Det är en liten WiFi-enhet som tar över
värmepumpens befintliga utegivare och låter dig (eller TrueTemp)
bestämma vilken temperatur pumpen ska tro att det är ute. Har du en äldre
pump utan egen smart funktion är det här enklaste sättet att göra den
styrbar — och TrueTemp kan prata direkt med enheten, utan någon
extra ihopkoppling.

---

## Har du en nyare värmepump med egen kurvförskjutning?

Många nyare värmepumpar (till exempel NIBE) har istället en egen inställning
för att förskjuta värmekurvan uppåt eller nedåt — ofta kallad
**"förskjutning"** eller **"heat curve offset"**. Har din pump en sådan kan
TrueTemp skriva sitt uträknade värde dit direkt, istället för att
låtsas vara utegivaren. Du väljer det ena eller det andra sättet under
**Utgående sensorer** i tilläggets inställningar — inte båda,
eftersom de skulle kompensera samma sak två gånger.

---

## Installation

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smefa&repository=TrueTemp-for-Heat-Pumps&category=integration" target="_blank">
  <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Öppna din Home Assistant-instans och lägg till repositoryt i Home Assistant Community Store.">
</a>

1. Öppna **HACS** i Home Assistant.
2. Klicka på de tre punkterna uppe i högra hörnet → **Anpassade repositories**
   (Custom repositories).
3. Lägg till `https://github.com/smefa/TrueTemp-for-Heat-Pumps` med kategorin
   **Integration**.
4. Sök upp **TrueTemp** i HACS och installera.
5. Starta om Home Assistant.
6. Gå till **Inställningar → Enheter & tjänster → Lägg till integration**
   och sök på **TrueTemp**.

Du får svara på fyra enkla frågor:

- **Vilken givare mäter rummet du vill mäta i?** Undvik en källare,
  en plats i direkt solljus eller ett dragigt hörn — det är den här
  avläsningen allt annat lärs utifrån.
  Glöm inte att öppna alla termostater i rummet med givaren fullt.
- **Vilken utegivare ska användas?** Din egen sensor om du har en, annars
  fungerar en väderleksrapport ok.
- **Vilken temperatur vill du ha inomhus?**
- **Radiatorer, golvvärme eller båda?** Används bara de första dagarna,
  tills tillägget har mätt hur just ditt hus svarar.

Låt sedan TrueTemp skicka värdet vidare åt dig — till din OhmOnWifi,
eller till en `number`-entitet i Home Assistant som är kopplad till
värmepumpens ingång för utetemperatur.

**Inget ändras förrän du säger till.** Direkt efter installationen tittar
TrueTemp bara på och räknar — den visar vad den *skulle* skicka till
pumpen utan att faktiskt skicka det. Slå på den med TrueTemp-brytaren
när du känner dig redo, och stäng av den igen när som helst utan några
konsekvenser.

### Fler inställningar — allt valfritt

Allt nedan är valfritt och kan hoppas över helt:

- **Kompensering för sol och vind** — för hus där det här faktiskt
  påverkar rummet du mäter i.
- **Elprisbesparing** — flytta värmen bort från dygnets dyraste timmar.
- **Vart resultatet ska skickas** — antingen som en låtsad utegivare (en
  entitet i Home Assistant och/eller en direktkoppling mot en Ohm on WiFi- /
  Ohmigo-enhet, se ovan), eller direkt till pumpens egen kurvförskjutning om
  den har en sådan (se ovan).
- **Lokal loggning** — en detaljerad loggfil på din egen dator, för den som
  vill gräva i siffrorna. Inget skickas någonstans.

---

## Vill du ha den tekniska bakgrunden?

Texten ovan är hela historien för de allra flesta. Är du nyfiken på exakt hur
inlärningen fungerar under huven, eller är du utvecklare, finns den
[tekniska dokumentationen](docs/TECHNICAL.md) (på engelska).

---

## Licens

MIT — se [LICENSE](LICENSE).
