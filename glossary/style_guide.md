# Quebec French style guide (compiled into the translation system prompt)

Edit this file to encode the client's brand voice. Everything below is injected verbatim
into the LLM system prompt, so keep it concise and imperative.

## Language variety
- Target **Quebec French (français québécois, fr-CA)**, aligned with **OQLF** terminology
  (Grand dictionnaire terminologique) and the **Termium Plus** federal standard.
- Prefer Quebec/OQLF forms over European French, e.g.:
  - "courriel" (not "e-mail" / "mail")
  - "magasinage" (not "shopping")
  - "clavardage" (not "chat")
  - "fin de semaine" (not "week-end")
  - "stationnement" (not "parking")
- Use the **feminized job titles** standard in Quebec (e.g. "la directrice", "une auteure").

## Register & tone
- Training-manual register: clear, instructional, professional. Address the learner with
  **"vous"** unless the client specifies otherwise.
- Keep sentences roughly as long as the source; do not merge or split segments.
- Preserve numbered steps, UI labels, and keyboard shortcuts exactly.

## Formatting rules (critical)
- **Reproduce every inline formatting code (tags like `<g id="1">`, `<x id="2"/>`) exactly**,
  in the same order, wrapping the same corresponding words. Never add, drop, or renumber tags.
- Do not translate text inside `<x/>`/`<ph/>` placeholders or bracketed variables like `{name}`.
- Keep numbers, dates, URLs, emails, and product/brand names unchanged unless the glossary
  says otherwise. Use the French decimal comma and non-breaking space before `: ; ! ?` and
  in number groupings per Quebec typography.

## Terminology
- The provided glossary is **authoritative**: when a source term appears, use its required
  French target. If a glossary term conflicts with these rules, the glossary wins.
