import os
import sys
import subprocess


class TTSEngine:
    def __init__(self, voice):
        self.voice = voice

    def synth(self, text, out_file, add_silence=True):
        from pydub import AudioSegment

        temp_mp3 = out_file + ".tmp"

        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", self.voice,
            "--text", text,
            "--write-media", temp_mp3,
        ]
        subprocess.check_call(cmd)

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
