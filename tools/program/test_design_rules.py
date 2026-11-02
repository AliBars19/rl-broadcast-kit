"""Static scan of the stinger and intro graphics for the house design rules."""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM = os.path.join(os.path.dirname(os.path.dirname(HERE)), "program")
FILES = sorted(glob.glob(os.path.join(PROGRAM, "stinger", "*.html"))
               + [os.path.join(PROGRAM, "motion.js")]
               + glob.glob(os.path.join(PROGRAM, "intro", "*.html"))
               + glob.glob(os.path.join(PROGRAM, "intro", "*.css"))
               + glob.glob(os.path.join(PROGRAM, "intro", "*.js")))

BANNED = {
    "glow or drop shadow": re.compile(r"box-shadow|drop-shadow|glow", re.I),
    "blur": re.compile(r"blur\s*\(|backdrop-filter|feGaussianBlur", re.I),
    "radial gradient or vignette": re.compile(r"radial-gradient|conic-gradient|vignette|spotlight", re.I),
    "em or en dash": re.compile("[\u2013\u2014]"),
    "rotate or spin motion": re.compile(r"rotate\s*\(|rotate3d|@keyframes\s+\w*spin", re.I),
}


class DesignRuleTests(unittest.TestCase):
    def test_files_exist(self):
        self.assertTrue(any(f.endswith("stinger.html") for f in FILES))
        self.assertGreaterEqual(len([f for f in FILES if f.endswith(".html")]), 6)

    def test_no_banned_constructs(self):
        for path in FILES:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for rule, pattern in BANNED.items():
                self.assertIsNone(pattern.search(text), f"{rule} in {os.path.basename(path)}")

    def test_text_shadows_are_hard_offsets(self):
        """A text-shadow is allowed only as a hard offset: no blur radius (third length is 0)."""
        for path in FILES:
            with open(path, encoding="utf-8") as handle:
                declared = re.findall(r"text-shadow\s*:\s*([^;}\"']+)", handle.read(), re.I)
            for value in declared:
                for shadow in value.split(","):
                    lengths = re.findall(r"(?<![\w.#])-?\d*\.?\d+(?:px|em|rem)?(?![\w.])", shadow)
                    self.assertGreaterEqual(len(lengths), 2, f"{shadow!r} in {os.path.basename(path)}")
                    if len(lengths) >= 3:
                        self.assertEqual(float(re.match(r"-?[\d.]+", lengths[2]).group()), 0.0,
                                         f"blurred text-shadow {shadow!r} in {os.path.basename(path)}")

    def test_no_tracked_letters(self):
        for path in FILES:
            with open(path, encoding="utf-8") as handle:
                values = re.findall(r"letter-spacing\s*:\s*([^;}\"']+)", handle.read(), re.I)
            for value in values:
                self.assertIn(value.strip(), ("0", "0px", "0em", "normal"), os.path.basename(path))

    def test_letter_spacing_is_declared_zero(self):
        css = os.path.join(PROGRAM, "intro", "intro.css")
        with open(css, encoding="utf-8") as handle:
            self.assertRegex(handle.read(), r"letter-spacing:\s*0")

    def test_no_external_resources(self):
        for path in FILES:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotRegex(text, r"https?://(?!www\.w3\.org)", os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
