# Production assets: voice, avatar footage, brand, lexicon

Checklists for the material only you can supply. Each section says what the
pipeline does with the asset, so it is clear why a spec matters rather than
just what it is.

---

## 1. Voice recording (ElevenLabs)

**Target: ~30 minutes of clean speech — 15–20 min RU, 10–15 min EN.**

Quality beats quantity here. Ten clean minutes clone better than forty minutes
with room echo, because the model learns the room along with the voice.

**Technical**

- WAV, 48 kHz, 24-bit, mono. No MP3 at any point in the chain.
- No music, no compression, no EQ, no noise reduction, no reverb. Send it dry —
  ElevenLabs does its own processing and stacking yours on top degrades it.
- One microphone, one position, one session per language. Changing mic distance
  mid-session teaches the model two different voices.
- Treated room or the quietest space available. Soft furnishings beat bare
  walls. Watch for fridge hum, air conditioning, and street noise.
- Peaks around −6 dBFS, noise floor below −60 dBFS. Never clip.
- Leave 1–2 s of silence at the start so the noise floor is measurable.

**Content — this is the part that usually gets skipped and shouldn't**

Read at the pace the channel actually uses: **130–155 wpm**, not the slower
104 wpm of the existing scripts. The clone inherits your rhythm, so recording
slowly bakes the problem in at the source.

Cover:

- Neutral narration — several minutes of ordinary explanatory prose.
- Emotional range — dry/deadpan, curious, emphatic, amused. The Beluga format
  lives on deadpan, so give it plenty.
- Question intonation, list intonation, and mid-sentence pauses.
- **Every tractate name and Hebrew/Aramaic term** from
  `pipeline/data/lexicon_ru.json` and `lexicon_en.json`, read the way you want
  them said. Read each once in isolation and once inside a sentence.
- Numbers, folio references ("Chagigah 17a", "Хагига 17а"), and dates.
- The closing Yahalom phrase, several takes with different energy.

**After recording**

The voice ids go on the server, never in chat:

```
/opt/tzoar/deploy/secrets/elevenlabs.env   (chmod 600)
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID_RU=
ELEVENLABS_VOICE_ID_EN=
```

Then recalibrate the RU/EN pacing factor. `pacing.estimate_beat_duration()`
currently assumes Russian runs 8% slower than English at equal word counts —
a working figure, not a measured one. Synthesise the same 200-word passage in
both voices, compare against `estimate_duration()`, and correct the constant.

---

## 2. Avatar footage (SadTalker)

**Target: 15–20 minutes.**

- 1080p minimum, 4K preferred. 25 or 50 fps — match `RenderPolicy.fps` (25).
- **Locked-off camera on a tripod.** No zoom, no handheld, no pan. SadTalker
  derives head pose from the source; a moving camera reads as a moving head.
- Even, soft, frontal lighting. No hard shadows across the face, no backlight,
  nothing that changes during the session.
- Green screen, evenly lit and separated from you so it doesn't spill.
- Waist-up framing, face occupying a consistent portion of frame.
- **Separate microphone** — camera audio is a sync reference only.

Content: neutral speech, the same emotional range as the voice session,
deliberate pauses, natural gestures, and unhurried head turns. Include several
seconds of stillness looking straight at the lens — that frame becomes the
default source still.

Also capture **one high-resolution still**: neutral expression, mouth closed,
eyes on lens, same lighting. This is what `--source` gets.

Note on `--still`: the pipeline passes it by default, which suppresses large
head motion and looks better for a corner inset. Drop it if the avatar becomes
the full frame.

---

## 3. Brand

Everything visual is in `pipeline/render/deck.py`. Replace the `THEME` dict and
nothing else in that module hard-codes appearance.

Needed:

- **Palette** — background, claim-slide background, foreground text, muted text,
  accent. Both channels; say whether they share a palette or differ.
- **Typefaces** — display face for narration, and a text face for the source
  line. Supply the actual font files; they must be installed on the server or
  rsvg will silently substitute. Include Cyrillic and Latin coverage — the same
  deck code renders both channels.
- **Logos** — TAMHA and Yahalom, SVG preferred, on light and dark.
- **Intro card and end CTA** — as SVG, or as a spec I can build to.
- **Description template** — `gate.build_description()` currently generates a
  minimal RU/EN description with the source list. Tell me what else belongs
  there, and note that `ALLOWED_LINK_HOSTS` in `gate.py` must be extended
  before any new link domain will pass the gate.

The claim slide is deliberately styled apart from the rest — different
background, accent rule, sources on screen. Keep that distinction in whatever
you supply: it is the one slide a viewer may want to verify.

---

## 4. Pronunciation lexicon

Starter files ship at `pipeline/data/lexicon_{ru,en}.json`: 37 Bavli tractates
plus common terms, ~55 entries each. Loaded automatically per language by
`Lexicon.for_language()`.

**They are unverified guesses and need your ear.** The workflow:

1. Synthesise the term list in both voices once the voices exist.
2. Fix whatever sounds wrong by editing the JSON value — the key stays as the
   spelling that appears in text, the value is what gets spoken.
3. Add anything missing: names, place names, recurring company names.

Two properties worth knowing when editing: matching is whole-word and
case-insensitive, so `Bava` inside `Bava Metzia` is replaced while `Bavarian`
is left alone; and multi-word keys work, matched longest-first. Keys beginning
with `_` are treated as comments.

RU values are Cyrillic respellings, EN values are hyphenated syllables with the
stressed syllable capitalised. That split is deliberate — the Russian voice
reads Cyrillic far more reliably than transliterated Latin.
