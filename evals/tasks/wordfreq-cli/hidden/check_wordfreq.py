import subprocess, sys, tempfile, os
sys.path.insert(0, os.getcwd())
from wordfreq import top_words
assert top_words("b a B, a! c", 2) == [("a", 2), ("b", 2)], top_words("b a B, a! c", 2)
assert top_words("Привет, мир! Мир", 1) == [("мир", 2)]
assert top_words("", 3) == []
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write("x y x z x y")
out = subprocess.run([sys.executable, "-m", "wordfreq", f.name, "-n", "2"], capture_output=True, text=True, check=True).stdout
assert out.strip().splitlines() == ["x\t3", "y\t2"], out
print("ok")
