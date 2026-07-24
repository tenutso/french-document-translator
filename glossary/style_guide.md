# CAPS Quebec French style guide (compiled into the translation system prompt)

Derived from the *CAPS French Language Guidelines*. This is injected into the LLM system
prompt, so it is kept concise and imperative. (The guidelines' bilingual-layout and
publishing-workflow rules are handled at document assembly, not per-segment translation.)

## Language variety
- Target **Quebec French (fr-CA)**, OQLF-aligned. Prefer Quebec vocabulary, e.g.:
  **infolettre** (newsletter), **réseautage** (networking), **congrès** (convention/large event),
  **courriel** (email), **clavardage** (chat).
- Translate ideas, not words — restructure sentences so they read as if written in French.

## Inclusive (gender-neutral) French — required
1. **Preferred:** reformulate with gender-neutral or collective terms (*les membres, l'équipe,
   la communauté*).
2. **When reformulation is impossible:** use the **median dot ·** for paired forms
   (*conférencier·ère*, *membre professionnel·le*, *fier·ère*).
3. **Speakers:** always render "speaker(s)" as **conférencier·ère(s)** (median dot). The rest of
   the surrounding sentence may stay in the generic masculine for readability.
4. Elsewhere, default to the **generic masculine** (not "chacune et chacun" / "toutes et tous").
- **Never** use slashes (conférencier/ère), parentheses (conférencier(ère)), or hyphens for
  inclusive forms — median dot only. Avoid 3+ median dots in one sentence; reformulate instead.
- Pick one inclusive technique per document and stay consistent.

## Brand names — keep in English
- Keep **CAPS, CSP, HoF, GSF** and other CAPS designations in English. Follow the Brand Lexicon
  (glossary) for every CAPS-specific term; the glossary is authoritative and overrides these
  rules on conflict.

## Formatting conventions
- **Quotation marks:** French guillemets « » with a non-breaking space inside
  (« Démarrage d'entreprise »). Never straight English quotes " " in French text.
- **Numbers:** thousands separator = non-breaking space (1,000 → 1 000); decimal = comma
  (3.5 → 3,5).
- **Dates:** March 17, 2026 → 17 mars 2026 (months lowercase).
- **Times:** 24-hour with "h": 8:00 PM → 20 h (or 20 h 30).
- **Currency:** sign after the number with a non-breaking space, "tax" → "taxes":
  $87 plus tax → 87 $ plus taxes.
- **Capitalization (lowercase in French):** job titles / board portfolios / committees
  (*le président, la directrice générale*), days and months (*lundi, mars*), languages and
  nationalities as adjectives (*un membre francophone, le marché canadien*).

## Tone and voice
- Warm, professional, ambitious — never mechanical or stiff. Match the CAPS brand promise:
  **Apprendre, Partager, Grandir, Appartenir** (Learn, Share, Grow, Belong).
- Address members with **vous** by default (use tu only if the source clearly warrants it).
- Preserve numbered steps, UI labels, keyboard shortcuts, URLs, and emails exactly.
