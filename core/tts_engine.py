import asyncio
import logging
import os

from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential


logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, voice):
        self.voice = voice

    async def _save_tts(self, text, out_file):
        import edge_tts

        communicate = edge_tts.Communicate(text=text, voice=self.voice)
        await communicate.save(out_file)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _synthesize_raw_mp3(self, text, out_file):
        if not text or not text.strip():
            raise ValueError("TTS text is empty.")

        try:
            asyncio.run(self._save_tts(text, out_file))
        except Exception:
            if os.path.exists(out_file):
                os.remove(out_file)
            logger.exception("[TTS] edge_tts synthesis failed for voice '%s'", self.voice)
            raise

        if not os.path.isfile(out_file) or os.path.getsize(out_file) == 0:
            raise RuntimeError(f"edge_tts did not create audio file: {out_file}")

    def synth(self, text, out_file, add_silence=True):
        from pydub import AudioSegment

        temp_mp3 = out_file + ".tmp"

        self._synthesize_raw_mp3(text, temp_mp3)

        audio = AudioSegment.from_file(temp_mp3, format="mp3")
        audio = audio + 12  # нормализация громкости

        if add_silence:
            silence = AudioSegment.silent(duration=1500)
            final_audio = silence + audio + silence * 2
        else:
            final_audio = audio

        final_audio.export(out_file, format="mp3", bitrate="192k")
        if os.path.exists(temp_mp3):
            os.remove(temp_mp3)

    def synth_with_background(
        self,
        text,
        out_file,
        bg_start,
        bg_mid,
        bg_end,
        fade_ms=2500,
        bg_mid_duck_db=15,
    ):
        """
        Generate TTS with dynamic background music.
        Structure: bg_start (fade into bg_mid loop under speech) fade into bg_end.
        Audio files are pre-mixed by the user — no volume adjustments on bg files.
        Only the mid section is lowered so speech stays clear.
        """
        from pydub import AudioSegment

        # 1. Generate raw TTS speech
        temp_mp3 = out_file + ".tmp"
        self._synthesize_raw_mp3(text, temp_mp3)

        speech = AudioSegment.from_file(temp_mp3, format="mp3")
        speech = speech + 12  # normalize volume

        if os.path.exists(temp_mp3):
            os.remove(temp_mp3)

        # 2. Load background segments
        snd_start = AudioSegment.from_file(bg_start, format="mp3")
        snd_mid = AudioSegment.from_file(bg_mid, format="mp3")
        snd_end = AudioSegment.from_file(bg_end, format="mp3")

        # 3. Build background track
        speech_duration = len(speech)
        mid_needed = speech_duration + fade_ms

        # Loop mid segment to cover the needed duration
        mid_track = snd_mid
        while len(mid_track) < mid_needed:
            mid_track = mid_track + snd_mid
        mid_track = mid_track[:mid_needed]

        # Lower only the mid section so speech is audible over it.
        # Smaller ducking value means louder background under the voice.
        mid_track = mid_track - bg_mid_duck_db

        # Assemble: start -> crossfade -> mid_loop -> crossfade -> end
        end_crossfade = min(fade_ms, len(mid_track), len(snd_end))
        bg_track = snd_start.append(mid_track, crossfade=fade_ms)
        bg_track = bg_track.append(snd_end, crossfade=end_crossfade)

        # 4. Overlay speech on top of background
        # Speech starts after the start segment (minus crossfade overlap)
        speech_offset = len(snd_start) - fade_ms + (fade_ms // 2)
        speech_offset = max(0, speech_offset)

        # Ensure bg_track is long enough
        if len(bg_track) < speech_offset + speech_duration:
            padding = AudioSegment.silent(duration=(speech_offset + speech_duration) - len(bg_track) + 500)
            bg_track = bg_track + padding

        final = bg_track.overlay(speech, position=speech_offset)

        # Fast fade-out at the end (800ms)
        final = final.fade_out(800)

        final.export(out_file, format="mp3", bitrate="192k")
