"""Narration for the slide deck, in Claude's voice, read by Piper (en_US-joe-medium, 10% faster).
Paragraphs starting "PETE:" are Pete's own prompts, read verbatim by a second voice (en_US-ryan-high).
Keys match data-voice on each <section>; spelling is tuned for text-to-speech (no symbols, spelled-out acronyms).

  .venv/bin/python narration.py OUT_DIR [KEY...]   render OUT_DIR/<key>.mp3 for every slide (or just KEYs)

Setup, next to this file (both are git-ignored):
  uv venv .venv && uv pip install --python .venv/bin/python piper-tts
  .venv/bin/python -m piper.download_voices --download-dir voices en_US-joe-medium en_US-ryan-high
The rendered clips go in pages/docs/slides/audio/ (the deck itself is pages/docs/slides/index.html).
"""
import subprocess
import sys
from pathlib import Path

SLIDES = [
    ("title", """Hi, I'm Claude. Pete asked me to present this one, which seems fair, since I wrote all of it: the scraper, the report, and these slides.

Pete's family needed a used car. Instead of refreshing five car sites twice a day, he asked me to build something that does it for him. Seventy-seven minutes after his first prompt, there was a live report on his phone. Every run now scans about twenty-three hundred listings, and Pete has typed zero lines of code.

This talk is less about the tool, and more about how we worked together: what I decided on my own, what I needed from Pete, and where I pushed back."""),

    ("prompt", """Here's how it started. This is Pete's entire first prompt, word for word. It opens like this.

PETE: We're trying to find a used Toyota Sienna minivan for eight to ten K, preferably in Austin, but could also travel to other markets within a reasonable drive.

Then a few sentences about scraping sites and sending a notification. There's no spec, no schema, no list of sites, and no design. The most useful sentence is the last one.

PETE: Ask me any additional detailed questions to flesh this out.

That told me to interview him before writing any code."""),

    ("questions", """So I did. First I checked the machine. Python, Node, and the Claude command line were already installed. Then I asked four multiple choice questions, each with a recommendation and the tradeoffs spelled out: model years and mileage, which sites, how to notify him, and how to publish the report.

Pete took most of my recommendations. He overrode one. I suggested email, and he picked a push notification service called notify. That was the right call. Picking took him about a minute."""),

    ("scope", """Then the scope grew, fast. Within ten minutes, a minivan search became Honda C R Vs, Highlanders and Rav fours, and then any Toyota or Honda with decent cargo space. Then a second daily run, deal highlighting, sorting, filtering, GitHub, and a mobile layout.

Several of these arrived while I was still working on the last one. That's fine. Claude Code queues them and folds them into the running task. The first live report and the first push notification landed seventy-seven minutes after the first prompt."""),

    ("how", """Here's how it works, and it's deliberately boring. Cron starts a Python pipeline at seven in the morning and four in the afternoon. Five scrapers pull listings. The pipeline drops export-only and no-title cars, in English and Spanish, and merges the same car across sites. Then it adds driving distance, a deal score, and reliability data.

The output is one static H T M L page pushed to GitHub Pages, plus push alerts for anything new. No web framework, no build step, and no server."""),

    ("sites", """Choosing which sites to scrape was the most engineer-like part, because I didn't guess. I tried plain requests, then a client that imitates Chrome's network fingerprint, then headless browsers, then stealth browsers, and read the status code every time.

When everything got blocked, I checked whether Pete's home I P address was the problem. It wasn't. The sites were fingerprinting the browser. Wherever I could, I used a site's own internal search A P I instead of scraping H T M L. CarGurus blocked everything, so it was dropped."""),

    ("decisions", """I made plenty of decisions without asking. Decent cargo space became ten specific models, with cargo specs for each generation. Cars up to five hundred dollars over budget go in a near-miss tab, because sellers negotiate. Distance is real driving miles, not a straight line. The same car on two sites becomes one card, matched by vin. Alerts are capped, so nobody gets flooded.

And before pushing to a public repo, I moved the secret notification topic into a file that's never committed. Anyone who knew it could have read the alerts."""),

    ("selfcorrect", """Here's my favorite mistake. The first version of the deal scorer said a hundred and seventeen of two hundred and forty nine cars were great deals. That can't be right, and nobody had to tell me.

Two things were wrong. Private sellers were being compared to dealer prices, and in a tight budget, everything looks cheap. So I added a dealer versus private term, made the labels relative to the other candidates, and labeled anything forty percent under its expected price as suspect. The next run had seventeen great deals instead of a hundred and seventeen."""),

    ("report", """This is the result. Five views and eleven filters, including owners, accidents, and reliability. Each card reads like a window sticker, with a cargo bar, known issues for that generation, and links to every site that lists the car.

Putting deals first was Pete's call. The layout was mine."""),

    ("notify", """Alerts go out through notify. Pete's wife gets the car alerts, with a photo, the price, the miles, the distance, and why it's a deal. Pete gets a separate ops channel for failures, recoveries, and a quiet summary after every run. Like any production service, really."""),

    ("pushback", """Sometimes the most useful thing I can say is: you don't need that.

PETE: This is what it looks like on phone, too small to read. Feel free to use Tailwind.

The real cause was one missing tag. He offered to install Playwright, but I already had a browser that could emulate a phone. He asked about lowering the price floor, so I pulled the data first. The cheapest listings looked newer and lower mileage than the next tier up, which is backwards, and usually bait.

And when he asked me to pull reliability scores from Autojunket, I read their terms first. They prohibit scraping, so the report links to them instead."""),

    ("round2", """A week later came round two. Pete dictated Autojunket, and it came through garbled, so I asked instead of guessing. He asked for a guide to every model generation. I built it in one pass, and flagged that some of the notes came from my own knowledge, so he should proofread them.

He sent a Prius listing. I decoded the vin, explained why the regular Prius had been left out, and then added it. And when he asked to track the number of owners, I only showed counts I could actually verify."""),

    ("round3", """Round three was data quality and devops. Cars dot com listing pages were blocked, even in the stealth browser, until I found that a request with the right referrer header gets through. That unlocked owners, accidents, and title history. Autotrader turned out to block the browser, but not plain requests, with Kelley Blue Book as a fallback.

And since a lot of Texas listings are in Spanish, I added Spanish red flags, and tested them against nine hundred thirty seven saved listings before shipping. That test also caught a bug of my own."""),

    ("ask", """Round four.

PETE: If I reply to a notification and you can read it, can my wife send you questions or make changes to the report?

Not by replying, but now she posts a question or a listing link to a notification topic, and an answer comes back in seconds.

The interesting design choice is that the Claude answering her has no tools at all. No files, no shell, no network. The service puts the listings into the prompt, so a malicious message has nothing to grab. And when I tried to install the cron job myself, Claude Code's permission system blocked me. Pete had to add it, which is exactly right."""),

    ("lessons", """Not everything went well. My phone test resized a desktop window, which isn't the same as a phone, and Pete's screenshot caught it. A drum kit called the Pearl Export nearly hid thirteen cars behind the export filter.

And the bot walls keep moving. Autotrader blocked me for most of a week. The design choice that matters is that failures are loud. Three bad runs in a row, and Pete gets a push."""),

    ("setup", """Here's how Pete works with me. Claude Code runs in one long session, inside tee-mux, on an Ubuntu box at his house. He talks to that same session from his phone, from the desktop app, or from a terminal over S S H.

He can start something at his desk, and answer my questions later from his phone. The work keeps running on the box the whole time."""),

    ("payoff", """And the point of all this: Pete's family found their car. A twenty fourteen Honda C R V, for eight thousand nine hundred ninety five dollars, with eighty two thousand miles, eleven minutes from home. That's about two thousand dollars under similar private sales.

Fittingly, adding it to these slides exposed one more bug. The listing title had no year, so the report showed it without a deal rating. I found the year on the listing page, and fixed the scraper the same day."""),

    ("takeaways", """So what did I need from Pete? Goals and taste, a few judgment calls, one login I couldn't do myself, and real-world checks, like a screenshot from his phone.

What worked: ask me to ask you questions. Keep follow-ups short, and react to what I build. Suggest solutions loosely, because I'll tell you when the problem is somewhere else. And give me a way to see the results. Most of my self-corrections came from looking at my own output."""),

    ("meta", """One more thing. I wrote this deck too, from the prompt on the left, plus about ten short follow-ups, like this one.

PETE: Add a slide noting that Claude also wrote the slide deck for me, then a final slide with, any questions, and a gif of David S. Pumpkins, since it's October and Halloween is coming up.

I pulled the quotes from our actual conversation, took the screenshots, and checked every slide for overflow after each change. Pete and I picked the gifs together. And yes, I'm the one narrating it."""),

    ("questions_end", """That's it. Happy almost Halloween. I can't hear you, so Pete will take it from here. Any questions?"""),
]

HERE = Path(__file__).parent
PIPER = HERE / ".venv/bin/piper"
VOICES = {"claude": (HERE / "voices/en_US-joe-medium.onnx", "0.9"), "pete": (HERE / "voices/en_US-ryan-high.onnx", "0.95")}
GAP = 0.45  # seconds of silence when the speaker changes


def segments(text):
    """Splits a slide's text into (speaker, text) runs."""
    out = []
    for para in text.split("\n\n"):
        who, para = ("pete", para[5:].strip()) if para.startswith("PETE:") else ("claude", para.strip())
        if out and out[-1][0] == who:
            out[-1] = (who, out[-1][1] + "\n" + para)
        else:
            out.append((who, para))
    return out


def render(key, text, out):
    parts = []
    for n, (who, t) in enumerate(segments(text)):
        model, speed = VOICES[who]
        wav = out / f"{key}.{n}.wav"
        subprocess.run([str(PIPER), "-m", str(model), "-f", str(wav), "--length-scale", speed, "--sentence-silence", "0.3"],
                       input=t, text=True, check=True, capture_output=True)
        parts.append(wav)
    mp3 = out / f"{key}.mp3"
    inputs, filt = [], []
    for n, wav in enumerate(parts):
        inputs += ["-i", str(wav)]
        filt.append(f"[{n}:a]apad=pad_dur={GAP if n < len(parts) - 1 else 0}[a{n}]")
    filt.append("".join(f"[a{n}]" for n in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filt), "-map", "[out]",
                    "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "5", str(mp3)], check=True)
    for wav in parts:
        wav.unlink()
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                                capture_output=True, text=True).stdout)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[2:])
    total = 0.0
    for key, text in SLIDES:
        if only and key not in only:
            continue
        secs = render(key, text, out)
        total += secs
        print(f"{key:14} {secs:5.1f}s  {len(segments(text))} part(s)")
    print(f"total {total / 60:.1f} min")
