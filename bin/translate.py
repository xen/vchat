import os
import sys
from pathlib import Path

import polib
from openai import OpenAI
from tqdm import tqdm

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

# Make sure to install: pip install polib openai tqdm
# Also ensure openai>=1.0.0 so that .chat.completions.create() works.

client = OpenAI()


def translate_text(text, source_lang, target_lang):
    """
    Translate 'text' from source_lang to target_lang using OpenAI's chat-based model.
    If the message contains the word 'Lidgen', instruct the model not to translate the brand name.
    """

    if not text.strip():
        return ""

    # Build a base prompt
    # We'll instruct it to keep meaning/context, but not to translate "Lidgen" if it appears.
    brand_note = ""
    if "Lidgen" in text:
        brand_note = "IMPORTANT: Do NOT translate the brand name 'Lidgen'. Keep it exactly as is.\n\n"

    prompt = (
        f"Translate the following text from {source_lang} to {target_lang}, "
        "preserving meaning, context, and style.\n\n"
        f"{brand_note}"
        f"{text}\n"
    )

    # Call the API
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    # In openai>=1.0.0, .content is an attribute, not a dict key
    return response.choices[0].message.content.strip()


def main():
    """
    Usage:
      python translate_folder.py <translations_root_folder>

    Example:
      python translate_folder.py lidgen/translations
    """
    if len(sys.argv) < 2:
        print("Usage: python translate_folder.py <translations_root_folder>")
        sys.exit(1)

    translations_folder = sys.argv[1]

    # Walk through every file in the given folder
    # expecting a structure like: <folder>/<lang>/LC_MESSAGES/messages.po
    for root, dirs, files in os.walk(translations_folder):
        for filename in files:
            if filename.endswith(".po"):
                # Example root: "lidgen/translations/de/LC_MESSAGES"
                # We want the language code from the subfolder name (e.g. "de").
                # Usually that is one level above "LC_MESSAGES", so let's parse carefully:
                parts = root.split(os.sep)

                # We expect something like [..., "translations", "<lang>", "LC_MESSAGES"] in 'parts'
                # So if the last part is "LC_MESSAGES", the one before is the language code.
                if len(parts) >= 2 and parts[-1] == "LC_MESSAGES":
                    lang_code = parts[-2]
                else:
                    # If the structure doesn't match, skip
                    continue

                if lang_code == "en":
                    print("Skipping English translation...")
                    continue

                po_path = os.path.join(root, filename)

                # Load .po file
                po = polib.pofile(po_path)

                print(f"\nProcessing: {po_path} (language: {lang_code})")

                # Translate only empty msgstr
                for entry in tqdm(po, desc=f"Translating .po file for {lang_code}"):
                    if not entry.msgstr.strip() or entry.fuzzy:
                        translated = translate_text(entry.msgid, "English", lang_code)
                        entry.msgstr = translated

                # Save the updated file (overwrites the original .po)
                po.save(po_path)
                print(f"Translation complete for {po_path}!")


if __name__ == "__main__":
    main()
